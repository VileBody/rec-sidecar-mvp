#!/usr/bin/env python3
"""Minimal browser roleplay loop for seller/client chat."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Literal

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel


REPO_ROOT = Path(__file__).resolve().parents[1]
UI_HTML = Path(__file__).resolve().parent / "static" / "index.html"
DEFAULT_PORT = 8101

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from llm_service.app.config import DEFAULT_CEREBRAS_API_BASE, Settings
from llm_service.app.orchestrator import LlmOrchestrator
from llm_service.app.providers import (
    ProviderError,
    cerebras_stage_response_format,
    cerebras_structured_response_format,
    parse_json_suggestion,
)
from llm_service.app.schemas import LiveRequest, StageRequest
from llm_service.app.stage_assets import (
    STAGE_AGENDA_BY_TAG,
    clamp_stage_forward,
    normalize_stage,
    parse_stage_detection,
    stage_detection_system_prompt,
)
from live_client_voice_agent import (
    DEFAULT_CLIENT_ACTOR_MODEL,
    DEFAULT_INPUT,
    ScriptPersona,
    build_client_system_prompt,
    client_reference_arc,
    dialogue_tail,
    event_facts,
    generate_client_reply,
    load_env_file,
    parse_reference_script,
    sanitize_client_reply,
)

logger = logging.getLogger("uvicorn.error")

SELLER_SYSTEM_PROMPT = """Ты пишешь одну следующую реплику продавца для high-check B2C sales разговора на русском.

Твоя задача - дать только текст продавца, который можно сразу скопировать и отправить или зачитать клиенту.

Правила:
- Если диалог только начинается, дай opener: короткий экологичный заход, рамку разговора и permission-based начало без раннего оффера.
- Если stage уже известен, придерживайся его agenda и следующего шага.
- Для cold / hostile клиента предпочитай прямую конкретику, а не теплые вводные мостики.
- Пиши естественно, конкретно и по делу, без шаблонной воды.
- Не пиши мета-инструкции вроде "спроси", "уточни", "скажи клиенту".
- По умолчанию не начинай с "понимаю", "слышу", "вы правы", "давайте сразу к сути", если можно сказать точнее и предметнее.
- Не используй markdown, списки, заголовки, кавычки вокруг ответа или пояснения.
- Ровно одно предложение.
"""

SEMANTIC_TRIGGER_SYSTEM_PROMPT = """Ты работаешь как быстрый ZAI-trigger для high-check B2C sales разговора.

У тебя есть только partial-реплика клиента. Твоя задача - решить, уже ли понятен основной смысл этой реплики настолько, что продавцу можно начинать готовить ответ прямо сейчас, не дожидаясь конца всей фразы.

Верни JSON по схеме:
- action = "suggest", если из partial уже понятен главный смысл, сомнение, возражение, запрос или направление ответа;
- action = "skip", если клиент еще только разгоняется, смысл пока плавает, или слишком рано делать вывод.

Правила:
- Будь практичным, но не сверхконсервативным.
- Если клиент уже явно выражает сомнение, боль, цель, отказ, запрос на конкретику или критерий выбора, обычно это "suggest".
- text всегда должен быть пустой строкой.
"""

DEFAULT_STAGE_TAG = "S2.1"
REACTION_INTERVAL_SECS = 2.2
CHUNK_REFRESH_INTERVAL_SECS = 5.0
SEMANTIC_TRIGGER_DEBOUNCE_SECS = 0.25
SEMANTIC_TRIGGER_TIMEOUT_SECS = 2.5
FINAL_STAGE_TIMEOUT_SECS = 4.0
VERTEX_STAGE_THINKING_LEVEL = "minimal"
VERTEX_SELLER_THINKING_LEVEL = "minimal"
DEFAULT_CLIENT_WPM = 145
DEFAULT_SELLER_WPM = 155
MIN_PARTIAL_REACTION_CHARS = 36
MIN_PARTIAL_GROWTH_CHARS = 18
MIN_SEMANTIC_TRIGGER_CHARS = 18
MIN_SEMANTIC_TRIGGER_GROWTH_CHARS = 6

ReplyMode = Literal[
    "zai_stage_reactive",
    "gemini_chunk_refresh",
    "zai_semantic_trigger",
]


class ResetRequest(BaseModel):
    script: int = 2
    persona_mode: Literal["neutral", "cold", "hostile"] = "hostile"
    reply_mode: ReplyMode = "zai_stage_reactive"
    client_wpm: int = DEFAULT_CLIENT_WPM
    seller_wpm: int = DEFAULT_SELLER_WPM


@dataclass
class Message:
    id: str
    role: Literal["seller", "client"]
    text: str
    created_at: float

    @property
    def speaker(self) -> str:
        return "Продавец" if self.role == "seller" else "Клиент"

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "role": self.role,
            "speaker": self.speaker,
            "text": self.text,
            "createdAt": self.created_at,
        }

    def history_role(self) -> str:
        return "Seller" if self.role == "seller" else "Client"


@dataclass
class ActiveTurn:
    id: str
    seller_text: str
    client_message_id: str
    started_at: float
    client_done: asyncio.Event
    last_reaction_text: str = ""
    last_reply_refresh_text: str = ""
    last_reply_refresh_at: float = 0.0
    last_semantic_check_text: str = ""
    final_client_latency_ms: int | None = None
    first_client_delta_ms: int | None = None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--env-file",
        type=Path,
        action="append",
        default=[Path(".env"), Path(".env.iac")],
    )
    return parser.parse_args(argv)


def env_var(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def list_reference_scripts(path: Path) -> list[dict[str, object]]:
    text = path.read_text(encoding="utf-8")
    return [
        {"number": int(match.group(1)), "title": match.group(2).strip()}
        for match in re.finditer(r"(?m)^## Скрипт\s+(\d+)\.\s*(.+)$", text)
    ]


def build_client_args(config: ResetRequest) -> argparse.Namespace:
    return argparse.Namespace(
        input=DEFAULT_INPUT,
        script=config.script,
        persona_mode=config.persona_mode,
        client_actor_model=os.getenv("CEREBRAS_MODEL", DEFAULT_CLIENT_ACTOR_MODEL),
        client_actor_temperature=0.85,
        client_actor_max_tokens=220,
        cerebras_api_key=env_var("CEREBRAS_API_KEY"),
        cerebras_api_base=env_var("CEREBRAS_API_BASE") or DEFAULT_CEREBRAS_API_BASE,
        cerebras_reasoning_effort=env_var("CEREBRAS_REASONING_EFFORT") or "none",
    )


def sanitize_seller_line(text: str) -> str:
    value = text.strip().strip("\"'`«»“”")
    value = re.sub(
        r"^(?:продавец|seller|coach|тренер|ответ|реплика)\s*[:：-]\s*",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(r"\s+", " ", value)
    return value.strip().strip("\"'`«»“”").strip()


def compact_text(text: str) -> str:
    return " ".join(text.strip().split())


def seller_style_hint(persona_mode: str) -> str:
    return {
        "neutral": "Тон спокойный и деловой, без давления.",
        "cold": "Тон собранный, короткий и предметный; не заигрывай и не утепляй без причины.",
        "hostile": "Тон спокойный, но жестко конкретный; не уговаривай и не смягчай лишнего.",
    }[persona_mode]


def clamp_wpm(value: int) -> int:
    return max(80, min(240, int(value)))


def speech_fragments(text: str) -> list[str]:
    fragments = re.findall(r"\S+\s*", text)
    return fragments or ([text] if text else [])


def reply_mode_options() -> list[dict[str, str]]:
    return [
        {
            "value": "zai_stage_reactive",
            "label": "ZAI staged",
            "description": "Текущий базовый режим: ZAI следит за stage на таймере, Gemini пишет следующую реплику.",
        },
        {
            "value": "gemini_chunk_refresh",
            "label": "Gemini chunk refresh",
            "description": "Без ZAI: каждые ~5 секунд клиентской речи заново собираем реплику и прерываем старую.",
        },
        {
            "value": "zai_semantic_trigger",
            "label": "ZAI semantic trigger",
            "description": "ZAI слушает partial клиента и как только смысл реплики уже понятен, сразу триггерит Gemini-ответ.",
        },
    ]


def reply_mode_label(mode: str) -> str:
    for option in reply_mode_options():
        if option["value"] == mode:
            return option["label"]
    return mode


def stage_preview(stage: str, *, provider: str, model: str, confidence: float | None) -> dict[str, object]:
    agenda = STAGE_AGENDA_BY_TAG[stage]
    return {
        "stage": agenda.stage,
        "title": agenda.title,
        "agenda": agenda.agenda,
        "emotion": agenda.emotion,
        "step": agenda.step,
        "provider": provider,
        "model": model,
        "confidence": confidence,
        "scorecard": None,
    }


class RoleplaySession:
    def __init__(self, initial_config: ResetRequest):
        self.initial_config = initial_config
        self.lock = asyncio.Lock()
        self.settings = Settings.from_env()
        self.http_client = httpx.AsyncClient(
            timeout=self.settings.timeout_secs,
            proxy=self.settings.outbound_proxy,
        )
        self.orchestrator = LlmOrchestrator(self.settings, self.http_client)
        self.script_options = list_reference_scripts(DEFAULT_INPUT)
        self.reply_mode_options = reply_mode_options()
        self.scorecard_task: asyncio.Task[None] | None = None
        self.turn_task: asyncio.Task[None] | None = None
        self.bootstrap_task: asyncio.Task[None] | None = None
        self.reply_task: asyncio.Task[None] | None = None
        self.semantic_trigger_task: asyncio.Task[None] | None = None
        self.reply_generation_id: str | None = None
        self.state_condition = asyncio.Condition()
        self.state_version = 0
        self.turn_version = 0
        self.active_turn: ActiveTurn | None = None
        self._apply_reset(initial_config)

    async def aclose(self) -> None:
        if self.bootstrap_task is not None:
            self.bootstrap_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self.bootstrap_task
            self.bootstrap_task = None
        await self._cancel_semantic_trigger_task()
        await self._cancel_reply_task()
        await self._cancel_turn_task()
        await self._cancel_scorecard_task()
        await self.orchestrator.aclose()

    async def bootstrap(self) -> None:
        async with self.lock:
            self.status = "seller_thinking"
            self.error = None
        self.bootstrap_task = asyncio.create_task(self._bootstrap_initial_reply())

    def _apply_reset(self, config: ResetRequest) -> None:
        normalized = config.model_copy(deep=True)
        normalized.client_wpm = clamp_wpm(normalized.client_wpm)
        normalized.seller_wpm = clamp_wpm(normalized.seller_wpm)
        self.config = normalized
        self.run_id = f"fresh-{uuid.uuid4().hex[:10]}"
        self.messages: list[Message] = []
        self.pending_seller: dict[str, object] | None = None
        self.stage = stage_preview(
            DEFAULT_STAGE_TAG,
            provider="preset",
            model="preset",
            confidence=None,
        )
        self.stage_status = "idle"
        self.last_route: dict[str, object] | None = None
        self.status = "booting"
        self.error: str | None = None
        self.reference = parse_reference_script(DEFAULT_INPUT, config.script)
        self.client_args = build_client_args(config)
        self.active_turn = None
        self.turn_version += 1

    def snapshot(self) -> dict[str, object]:
        return {
            "runId": self.run_id,
            "status": self.status,
            "error": self.error,
            "config": self.config.model_dump(),
            "scriptOptions": self.script_options,
            "replyModeOptions": self.reply_mode_options,
            "messages": [message.as_dict() for message in self.messages],
            "pendingSeller": self.pending_seller,
            "stage": self.stage,
            "stageStatus": self.stage_status,
            "lastRoute": self.last_route,
            "eventFacts": event_facts(),
            "canAdvance": bool(
                self.pending_seller
                and self.pending_seller.get("text")
                and not self.pending_seller.get("streaming")
                and self.active_turn is None
            ),
            "turnActive": self.active_turn is not None,
            "clientStreaming": bool(
                self.active_turn is not None and not self.active_turn.client_done.is_set()
            ),
            "replyStreaming": bool(self.pending_seller and self.pending_seller.get("streaming")),
            "activeClientMessageId": self.active_turn.client_message_id if self.active_turn else None,
        }

    async def snapshot_now(self) -> dict[str, object]:
        async with self.lock:
            return self.snapshot()

    async def current_snapshot_with_version(self) -> tuple[int, dict[str, object]]:
        async with self.lock:
            return self.state_version, self.snapshot()

    async def wait_for_snapshot(
        self,
        after_version: int,
        *,
        timeout_secs: float = 15.0,
    ) -> tuple[int, dict[str, object]] | None:
        try:
            async with self.state_condition:
                await asyncio.wait_for(
                    self.state_condition.wait_for(lambda: self.state_version > after_version),
                    timeout=timeout_secs,
                )
                version = self.state_version
        except TimeoutError:
            return None

        async with self.lock:
            return version, self.snapshot()

    async def _publish_state(self) -> None:
        async with self.state_condition:
            self.state_version += 1
            self.state_condition.notify_all()

    async def reset(self, config: ResetRequest) -> dict[str, object]:
        if self.bootstrap_task is not None:
            self.bootstrap_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self.bootstrap_task
            self.bootstrap_task = None
        await self._cancel_semantic_trigger_task()
        await self._cancel_reply_task()
        await self._cancel_turn_task()
        await self._cancel_scorecard_task()
        async with self.lock:
            self._apply_reset(config)
        await self._publish_state()
        try:
            await self._generate_seller_line_current(initial=True, trigger_reason="opener")
            async with self.lock:
                self.status = "ready"
            await self._publish_state()
        except Exception as exc:
            async with self.lock:
                self.status = "error"
                self.error = str(exc)
            await self._publish_state()
        return await self.snapshot_now()

    async def _bootstrap_initial_reply(self) -> None:
        try:
            await self._generate_seller_line_current(initial=True, trigger_reason="opener")
            async with self.lock:
                self.status = "ready"
            await self._publish_state()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self.lock:
                self.status = "error"
                self.error = str(exc)
            await self._publish_state()
        finally:
            self.bootstrap_task = None

    async def advance_turn(self) -> dict[str, object]:
        async with self.lock:
            if self.active_turn is not None:
                raise RuntimeError("Текущий ход клиента еще не завершен.")
            if not self.pending_seller or not self.pending_seller.get("text"):
                raise RuntimeError("Сначала нужна текущая реплика продавца.")

            seller_text = str(self.pending_seller["text"]).strip()
            self._append_message("seller", seller_text)
            client_message = self._append_message("client", "")
            self.pending_seller = None
            self.status = "client_reply"
            self.error = None
            turn = ActiveTurn(
                id=f"turn-{uuid.uuid4().hex[:8]}",
                seller_text=seller_text,
                client_message_id=client_message.id,
                started_at=time.monotonic(),
                client_done=asyncio.Event(),
            )
            self.active_turn = turn
            self.last_route = {
                "analysisKind": "streaming",
                "replyTriggerReason": "turn_started",
                "replyProvider": "vertex",
                "replyModel": self.settings.vertex_model,
                "replyMode": self.config.reply_mode,
            }
            self.turn_task = asyncio.create_task(self._run_turn(turn.id))
            snapshot = self.snapshot()
        await self._publish_state()
        return snapshot

    async def regenerate_seller(self) -> dict[str, object]:
        async with self.lock:
            if self.active_turn is not None:
                raise RuntimeError("Нельзя перегенерить реплику, пока клиент еще отвечает.")
        try:
            await self._generate_seller_line_current(
                initial=not self.messages,
                trigger_reason="manual_regenerate",
            )
            async with self.lock:
                self.status = "ready"
                snapshot = self.snapshot()
            await self._publish_state()
            return snapshot
        except Exception as exc:
            async with self.lock:
                self.status = "error"
                self.error = str(exc)
            await self._publish_state()
            raise

    async def _cancel_turn_task(self) -> None:
        task = self.turn_task
        self.turn_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

    async def _cancel_scorecard_task(self) -> None:
        task = self.scorecard_task
        self.scorecard_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

    async def _cancel_reply_task(self) -> None:
        task = self.reply_task
        self.reply_task = None
        self.reply_generation_id = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

    async def _cancel_semantic_trigger_task(self) -> None:
        task = self.semantic_trigger_task
        self.semantic_trigger_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

    async def _run_turn(self, turn_id: str) -> None:
        client_task = asyncio.create_task(self._stream_client_reply(turn_id))
        reaction_task = asyncio.create_task(self._reaction_loop(turn_id))
        try:
            await client_task
            await reaction_task
        except asyncio.CancelledError:
            client_task.cancel()
            reaction_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await client_task
            with suppress(asyncio.CancelledError, Exception):
                await reaction_task
            raise
        except Exception as exc:
            client_task.cancel()
            reaction_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await client_task
            with suppress(asyncio.CancelledError, Exception):
                await reaction_task
            async with self.lock:
                if self.active_turn and self.active_turn.id == turn_id:
                    self.status = "error"
                    self.error = str(exc)
        finally:
            await self._cancel_semantic_trigger_task()
            async with self.lock:
                if self.active_turn and self.active_turn.id == turn_id:
                    self.active_turn = None
                if self.turn_task is asyncio.current_task():
                    self.turn_task = None
                if self.status != "error":
                    self.status = "ready"
            await self._publish_state()

    async def _stream_client_reply(self, turn_id: str) -> None:
        async with self.lock:
            turn = self._require_active_turn_locked(turn_id)
            history_messages = self._messages_snapshot_locked(skip_message_id=turn.client_message_id)
            seller_text = turn.seller_text

        started_at = time.monotonic()
        parts: list[str] = []
        first_delta_ms: int | None = None
        try:
            if self.orchestrator.cerebras.configured():
                async for delta in self.orchestrator.cerebras.stream_text(
                    model=self.client_args.client_actor_model,
                    system_prompt=build_client_system_prompt(self.config.persona_mode),
                    user_content=self._client_user_content(
                        messages=history_messages,
                        seller_text=seller_text,
                    ),
                    temperature=self.client_args.client_actor_temperature,
                    prompt_cache_key=f"fresh-start-client-stream-v1-{self.run_id}",
                ):
                    if not delta:
                        continue
                    async for fragment in self._paced_fragments(delta, self.config.client_wpm):
                        parts.append(fragment)
                        partial_text = self._streaming_text("".join(parts))
                        if partial_text and first_delta_ms is None:
                            first_delta_ms = int((time.monotonic() - started_at) * 1000)
                        await self._update_client_stream(turn_id, partial_text, first_delta_ms)
                final_text = sanitize_client_reply("".join(parts))
            else:
                final_text = await asyncio.to_thread(
                    generate_client_reply,
                    args=self.client_args,
                    reference=self.reference,
                    history=self._history_pairs_from(history_messages),
                    seller_transcript=seller_text,
                )
                if not final_text:
                    raise RuntimeError("Клиент не вернул реплику.")
                async for fragment in self._paced_fragments(final_text, self.config.client_wpm):
                    parts.append(fragment)
                    partial_text = self._streaming_text("".join(parts))
                    if partial_text and first_delta_ms is None:
                        first_delta_ms = int((time.monotonic() - started_at) * 1000)
                    await self._update_client_stream(turn_id, partial_text, first_delta_ms)
                final_text = sanitize_client_reply("".join(parts))

            if not final_text:
                raise RuntimeError("Клиент не вернул реплику.")

            async with self.lock:
                turn = self._require_active_turn_locked(turn_id)
                turn.final_client_latency_ms = int((time.monotonic() - started_at) * 1000)
                if turn.first_client_delta_ms is None and first_delta_ms is not None:
                    turn.first_client_delta_ms = first_delta_ms
                message = self._find_message_locked(turn.client_message_id)
                if message is not None:
                    message.text = final_text
                route = dict(self.last_route or {})
                if turn.first_client_delta_ms is not None:
                    route["clientFirstDeltaMs"] = turn.first_client_delta_ms
                route["clientLatencyMs"] = turn.final_client_latency_ms
                route["clientChars"] = len(final_text)
                self.last_route = route
            await self._publish_state()
        finally:
            async with self.lock:
                if self.active_turn and self.active_turn.id == turn_id:
                    self.active_turn.client_done.set()
            await self._publish_state()

    async def _update_client_stream(
        self,
        turn_id: str,
        partial_text: str,
        first_delta_ms: int | None,
    ) -> None:
        async with self.lock:
            turn = self._require_active_turn_locked(turn_id)
            message = self._find_message_locked(turn.client_message_id)
            if message is not None:
                message.text = partial_text
            if first_delta_ms is not None and turn.first_client_delta_ms is None:
                turn.first_client_delta_ms = first_delta_ms
            route = dict(self.last_route or {})
            if first_delta_ms is not None:
                route["clientFirstDeltaMs"] = first_delta_ms
            route["clientChars"] = len(partial_text)
            self.last_route = route
        await self._publish_state()
        if self.config.reply_mode == "zai_semantic_trigger":
            self._schedule_semantic_trigger(turn_id)

    async def _reaction_loop(self, turn_id: str) -> None:
        mode = self.config.reply_mode
        if mode == "zai_stage_reactive":
            while True:
                async with self.lock:
                    turn = self._require_active_turn_locked(turn_id)
                    client_done = turn.client_done
                try:
                    await asyncio.wait_for(client_done.wait(), timeout=REACTION_INTERVAL_SECS)
                    break
                except TimeoutError:
                    await self._analyze_partial_staged(turn_id, final=False)
            await self._analyze_partial_staged(turn_id, final=True)
            return

        if mode == "zai_semantic_trigger":
            async with self.lock:
                turn = self._require_active_turn_locked(turn_id)
                client_done = turn.client_done
            await client_done.wait()
            task = self.semantic_trigger_task
            if task is not None and not task.done():
                with suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
            await self._cancel_semantic_trigger_task()
            await self._finalize_semantic_trigger(turn_id)
            return

        interval = CHUNK_REFRESH_INTERVAL_SECS
        while True:
            async with self.lock:
                turn = self._require_active_turn_locked(turn_id)
                client_done = turn.client_done
            try:
                await asyncio.wait_for(client_done.wait(), timeout=interval)
                break
            except TimeoutError:
                await self._analyze_chunk_refresh(turn_id, final=False)
        await self._analyze_chunk_refresh(turn_id, final=True)

    def _schedule_semantic_trigger(self, turn_id: str) -> None:
        task = self.semantic_trigger_task
        if task is not None and not task.done():
            return
        self.semantic_trigger_task = asyncio.create_task(
            self._debounced_semantic_trigger(turn_id)
        )

    async def _debounced_semantic_trigger(self, turn_id: str) -> None:
        try:
            await asyncio.sleep(SEMANTIC_TRIGGER_DEBOUNCE_SECS)
            await self._analyze_semantic_trigger(turn_id)
        except asyncio.CancelledError:
            raise
        finally:
            if self.semantic_trigger_task is asyncio.current_task():
                self.semantic_trigger_task = None

    async def _analyze_partial_staged(self, turn_id: str, *, final: bool) -> None:
        async with self.lock:
            turn = self._require_active_turn_locked(turn_id)
            messages = self._messages_snapshot_locked()
            client_message = self._find_message_in(messages, turn.client_message_id)
            client_text = compact_text(client_message.text if client_message else "")
            current_stage = normalize_stage(str((self.stage or {}).get("stage") or "")) or DEFAULT_STAGE_TAG
            existing_pending_text = ""
            if self.pending_seller and self.pending_seller.get("text"):
                existing_pending_text = str(self.pending_seller["text"]).strip()

            if not final:
                if len(client_text) < MIN_PARTIAL_REACTION_CHARS:
                    return
                growth = len(client_text) - len(turn.last_reaction_text)
                if growth < MIN_PARTIAL_GROWTH_CHARS and not re.search(r"[.?!…]$", client_text):
                    return
            turn.last_reaction_text = client_text
            self.stage_status = "reacting"
        await self._publish_state()

        stage_started = time.monotonic()
        stage_context = self._stage_context_from(messages)
        stage_data, route_stage, request = await self._detect_stage_fast(
            context=stage_context,
            current_stage=current_stage,
        )
        stage_elapsed_ms = int((time.monotonic() - stage_started) * 1000)

        stage_changed = stage_data["stage"] != current_stage
        if stage_changed or not existing_pending_text:
            reply_gate = {
                "action": "suggest",
                "provider": "shortcut",
                "model": "shortcut",
            }
            gate_elapsed_ms = 0
        else:
            gate_started = time.monotonic()
            gate_context = self._live_gate_context_from(messages)
            reply_gate = await self._reply_refresh_gate(
                context=gate_context,
                current_text=existing_pending_text,
                force=False,
            )
            gate_elapsed_ms = int((time.monotonic() - gate_started) * 1000)

        needs_reply = (
            final
            or stage_changed
            or not existing_pending_text
            or reply_gate["action"] == "suggest"
        )
        trigger_reason = (
            "final_client_turn"
            if final and needs_reply
            else "final_keep_current_reply"
            if final
            else "stage_changed"
            if stage_changed
            else "zai_suggest"
            if reply_gate["action"] == "suggest"
            else "keep_current_reply"
        )

        async with self.lock:
            if not (self.active_turn and self.active_turn.id == turn_id):
                return
            previous_stage = str((self.stage or {}).get("stage") or current_stage)
            preserved_scorecard = None
            if self.stage and str(self.stage.get("stage")) == stage_data["stage"]:
                preserved_scorecard = self.stage.get("scorecard")
            self.stage = stage_data
            if preserved_scorecard is not None:
                self.stage["scorecard"] = preserved_scorecard
            self.last_route = {
                **route_stage,
                "replyGateLatencyMs": gate_elapsed_ms,
                "replyGateAction": reply_gate["action"],
                "replyGateProvider": reply_gate["provider"],
                "replyGateModel": reply_gate["model"],
                "replyTriggeredGemini": needs_reply,
                "replyTriggerReason": trigger_reason,
                "analysisKind": "final" if final else "partial",
                "partialChars": len(client_text),
                "previousStage": previous_stage,
                "currentStage": stage_data["stage"],
                "stageChanged": stage_changed,
                "stageLatencyMs": stage_elapsed_ms,
                "replyProvider": "vertex",
                "replyModel": self.settings.vertex_model,
            }
            if stage_changed or final or self.stage.get("scorecard") is None:
                self.stage_status = "scoring"
                self._start_scorecard_refresh(request=request, stage=stage_data["stage"])
            else:
                self.stage_status = "ready"
        await self._publish_state()

        if not needs_reply:
            return

        seller_started = time.monotonic()
        await self._start_reply_generation(
            initial=False,
            messages=messages,
            stage_data=stage_data,
            kind="reply",
            trigger_reason=trigger_reason,
            await_completion=True,
        )
        seller_elapsed_ms = int((time.monotonic() - seller_started) * 1000)
        async with self.lock:
            if self.last_route is not None:
                self.last_route["sellerLatencyMs"] = seller_elapsed_ms
        await self._publish_state()

    async def _analyze_chunk_refresh(self, turn_id: str, *, final: bool) -> None:
        async with self.lock:
            turn = self._require_active_turn_locked(turn_id)
            messages = self._messages_snapshot_locked()
            client_message = self._find_message_in(messages, turn.client_message_id)
            client_text = compact_text(client_message.text if client_message else "")
            current_stage = normalize_stage(str((self.stage or {}).get("stage") or "")) or DEFAULT_STAGE_TAG
            now = time.monotonic()

            if not final:
                if not client_text:
                    return
                if (
                    turn.last_reply_refresh_at <= 0
                    and now - turn.started_at < CHUNK_REFRESH_INTERVAL_SECS
                ):
                    return
                if (
                    turn.last_reply_refresh_at > 0
                    and now - turn.last_reply_refresh_at < CHUNK_REFRESH_INTERVAL_SECS
                ):
                    return
                if client_text == turn.last_reply_refresh_text:
                    return

            turn.last_reply_refresh_text = client_text
            turn.last_reply_refresh_at = now
            self.stage_status = "reacting"
        await self._publish_state()

        stage_data = None
        route_stage: dict[str, object] = {}
        request: StageRequest | None = None
        stage_elapsed_ms = 0
        stage_changed = False
        if final:
            stage_started = time.monotonic()
            stage_context = self._stage_context_from(messages)
            stage_data, route_stage, request = await self._detect_stage_vertex_only(
                context=stage_context,
                current_stage=current_stage,
            )
            stage_elapsed_ms = int((time.monotonic() - stage_started) * 1000)
            stage_changed = stage_data["stage"] != current_stage

        trigger_reason = (
            "final_client_turn"
            if final
            else "chunk_refresh"
        )

        async with self.lock:
            if not (self.active_turn and self.active_turn.id == turn_id):
                return
            if stage_data is not None:
                previous_stage = str((self.stage or {}).get("stage") or current_stage)
                preserved_scorecard = None
                if self.stage and str(self.stage.get("stage")) == stage_data["stage"]:
                    preserved_scorecard = self.stage.get("scorecard")
                self.stage = stage_data
                if preserved_scorecard is not None:
                    self.stage["scorecard"] = preserved_scorecard
                self.last_route = {
                    **route_stage,
                    "replyTriggeredGemini": True,
                    "replyTriggerReason": trigger_reason,
                    "analysisKind": "final" if final else "partial",
                    "partialChars": len(client_text),
                    "previousStage": previous_stage,
                    "currentStage": stage_data["stage"],
                    "stageChanged": stage_changed,
                    "stageLatencyMs": stage_elapsed_ms,
                    "replyProvider": "vertex",
                    "replyModel": self.settings.vertex_model,
                    "replyMode": self.config.reply_mode,
                }
                self.stage_status = "scoring"
                if request is not None:
                    self._start_scorecard_refresh(request=request, stage=stage_data["stage"])
            else:
                self.last_route = {
                    **dict(self.last_route or {}),
                    "replyTriggeredGemini": True,
                    "replyTriggerReason": trigger_reason,
                    "analysisKind": "partial",
                    "partialChars": len(client_text),
                    "replyProvider": "vertex",
                    "replyModel": self.settings.vertex_model,
                    "replyMode": self.config.reply_mode,
                }
        await self._publish_state()

        seller_started = time.monotonic()
        await self._start_reply_generation(
            initial=False,
            messages=messages,
            stage_data=dict(self.stage or stage_preview(DEFAULT_STAGE_TAG, provider="preset", model="preset", confidence=None)),
            kind="reply",
            trigger_reason=trigger_reason,
            await_completion=final,
        )
        if final:
            seller_elapsed_ms = int((time.monotonic() - seller_started) * 1000)
            async with self.lock:
                if self.last_route is not None:
                    self.last_route["sellerLatencyMs"] = seller_elapsed_ms
            await self._publish_state()

    async def _analyze_semantic_trigger(self, turn_id: str) -> None:
        async with self.lock:
            turn = self._require_active_turn_locked(turn_id)
            messages = self._messages_snapshot_locked()
            client_message = self._find_message_in(messages, turn.client_message_id)
            client_text = compact_text(client_message.text if client_message else "")

            if len(client_text) < MIN_SEMANTIC_TRIGGER_CHARS:
                return
            growth = len(client_text) - len(turn.last_semantic_check_text)
            if growth < MIN_SEMANTIC_TRIGGER_GROWTH_CHARS and not re.search(r"[.?!…,:;]$", client_text):
                return

            turn.last_semantic_check_text = client_text
            self.stage_status = "reacting"
        await self._publish_state()

        gate_started = time.monotonic()
        gate_context = self._stage_context_from(messages)
        logger.info(
            "semantic_trigger check run_id=%s chars=%s mode=%s",
            self.run_id,
            len(client_text),
            self.config.reply_mode,
        )
        try:
            reply_gate = await asyncio.wait_for(
                self._semantic_trigger_gate(context=gate_context),
                timeout=SEMANTIC_TRIGGER_TIMEOUT_SECS,
            )
        except asyncio.TimeoutError:
            reply_gate = {
                "action": "suggest",
                "provider": "semantic-timeout",
                "model": "semantic-timeout",
            }
            logger.warning(
                "semantic_trigger timeout run_id=%s chars=%s timeout_secs=%.1f -> suggest",
                self.run_id,
                len(client_text),
                SEMANTIC_TRIGGER_TIMEOUT_SECS,
            )
        gate_elapsed_ms = int((time.monotonic() - gate_started) * 1000)

        async with self.lock:
            if not (self.active_turn and self.active_turn.id == turn_id):
                return
            self.last_route = {
                **dict(self.last_route or {}),
                "replyGateLatencyMs": gate_elapsed_ms,
                "replyGateAction": reply_gate["action"],
                "replyGateProvider": reply_gate["provider"],
                "replyGateModel": reply_gate["model"],
                "replyTriggeredGemini": reply_gate["action"] == "suggest",
                "replyTriggerReason": (
                    "semantic_trigger" if reply_gate["action"] == "suggest" else "semantic_hold"
                ),
                "analysisKind": "partial",
                "partialChars": len(client_text),
                "replyProvider": "vertex",
                "replyModel": self.settings.vertex_model,
                "replyMode": self.config.reply_mode,
            }
            if reply_gate["action"] != "suggest":
                self.stage_status = "ready"
        await self._publish_state()
        logger.info(
            "semantic_trigger decision run_id=%s chars=%s action=%s provider=%s model=%s elapsed_ms=%s",
            self.run_id,
            len(client_text),
            reply_gate["action"],
            reply_gate["provider"],
            reply_gate["model"],
            gate_elapsed_ms,
        )

        if reply_gate["action"] != "suggest":
            return

        await self._start_reply_generation(
            initial=False,
            messages=messages,
            stage_data=dict(
                self.stage
                or stage_preview(
                    DEFAULT_STAGE_TAG,
                    provider="preset",
                    model="preset",
                    confidence=None,
                )
            ),
            kind="reply",
            trigger_reason="semantic_trigger",
            await_completion=False,
        )

    async def _finalize_semantic_trigger(self, turn_id: str) -> None:
        async with self.lock:
            turn = self._require_active_turn_locked(turn_id)
            messages = self._messages_snapshot_locked()
            client_message = self._find_message_in(messages, turn.client_message_id)
            client_text = compact_text(client_message.text if client_message else "")
            current_stage = normalize_stage(str((self.stage or {}).get("stage") or "")) or DEFAULT_STAGE_TAG
            existing_pending_text = ""
            if self.pending_seller and self.pending_seller.get("text"):
                existing_pending_text = str(self.pending_seller["text"]).strip()
            has_inflight_reply = bool(self.pending_seller and self.pending_seller.get("streaming"))
            self.stage_status = "reacting"
        await self._publish_state()

        stage_started = time.monotonic()
        stage_context = self._stage_context_from(messages)
        request = StageRequest(
            run_id=self.run_id,
            context=stage_context,
            current_stage=current_stage or None,
        )
        try:
            stage_data, route_stage, request = await asyncio.wait_for(
                self._detect_stage_fast(
                    context=stage_context,
                    current_stage=current_stage,
                ),
                timeout=FINAL_STAGE_TIMEOUT_SECS,
            )
            stage_elapsed_ms = int((time.monotonic() - stage_started) * 1000)
            stage_changed = stage_data["stage"] != current_stage
            logger.info(
                "semantic_finalize stage_ready run_id=%s previous=%s current=%s elapsed_ms=%s",
                self.run_id,
                current_stage,
                stage_data["stage"],
                stage_elapsed_ms,
            )
        except asyncio.TimeoutError:
            stage_elapsed_ms = int((time.monotonic() - stage_started) * 1000)
            stage_changed = False
            stage_data = dict(
                self.stage
                or stage_preview(
                    current_stage,
                    provider="stage-timeout",
                    model="stage-timeout",
                    confidence=None,
                )
            )
            route_stage = {
                "detectorProvider": "stage-timeout",
                "detectorModel": "stage-timeout",
                "stageChanged": False,
            }
            logger.warning(
                "semantic_finalize stage_timeout run_id=%s stage=%s timeout_secs=%.1f",
                self.run_id,
                current_stage,
                FINAL_STAGE_TIMEOUT_SECS,
            )

        if has_inflight_reply and not existing_pending_text:
            reply_gate = {
                "action": "skip",
                "provider": "inflight",
                "model": "inflight",
            }
            gate_elapsed_ms = 0
        elif stage_changed or not existing_pending_text:
            reply_gate = {
                "action": "suggest",
                "provider": "shortcut",
                "model": "shortcut",
            }
            gate_elapsed_ms = 0
        else:
            gate_started = time.monotonic()
            gate_context = self._live_gate_context_from(messages)
            reply_gate = await self._reply_refresh_gate(
                context=gate_context,
                current_text=existing_pending_text,
                force=True,
            )
            gate_elapsed_ms = int((time.monotonic() - gate_started) * 1000)

        needs_reply = (
            (stage_changed or not existing_pending_text)
            and not has_inflight_reply
            or reply_gate["action"] == "suggest"
        )
        trigger_reason = (
            "final_keep_inflight_reply"
            if has_inflight_reply and not needs_reply
            else "final_client_turn"
            if needs_reply
            else "final_keep_current_reply"
        )

        async with self.lock:
            if not (self.active_turn and self.active_turn.id == turn_id):
                return
            previous_stage = str((self.stage or {}).get("stage") or current_stage)
            preserved_scorecard = None
            if self.stage and str(self.stage.get("stage")) == stage_data["stage"]:
                preserved_scorecard = self.stage.get("scorecard")
            self.stage = stage_data
            if preserved_scorecard is not None:
                self.stage["scorecard"] = preserved_scorecard
            self.last_route = {
                **route_stage,
                "replyGateLatencyMs": gate_elapsed_ms,
                "replyGateAction": reply_gate["action"],
                "replyGateProvider": reply_gate["provider"],
                "replyGateModel": reply_gate["model"],
                "replyTriggeredGemini": needs_reply,
                "replyTriggerReason": trigger_reason,
                "analysisKind": "final",
                "partialChars": len(client_text),
                "previousStage": previous_stage,
                "currentStage": stage_data["stage"],
                "stageChanged": stage_changed,
                "stageLatencyMs": stage_elapsed_ms,
                "replyProvider": "vertex",
                "replyModel": self.settings.vertex_model,
                "replyMode": self.config.reply_mode,
            }
            self.stage_status = "scoring"
            self._start_scorecard_refresh(request=request, stage=stage_data["stage"])
        await self._publish_state()

        if not needs_reply:
            return

        seller_started = time.monotonic()
        await self._start_reply_generation(
            initial=False,
            messages=messages,
            stage_data=stage_data,
            kind="reply",
            trigger_reason=trigger_reason,
            await_completion=False,
        )
        async with self.lock:
            if self.last_route is not None:
                self.last_route["sellerLatencyMs"] = int((time.monotonic() - seller_started) * 1000)
        await self._publish_state()

    async def _detect_stage_vertex_only(
        self,
        *,
        context: str,
        current_stage: str,
    ) -> tuple[dict[str, object], dict[str, object], StageRequest]:
        request = StageRequest(
            run_id=self.run_id,
            context=context,
            current_stage=current_stage or None,
        )
        previous_stage = current_stage or DEFAULT_STAGE_TAG
        user_content = (
            f"{request.context}\n\n"
            "--- Текущий stage из предыдущего шага ---\n"
            f"{request.current_stage or '(пока неизвестен)'}\n"
        )
        errors: list[str] = []

        if self.orchestrator.vertex.configured():
            thinking_attempts: list[str | None] = [VERTEX_STAGE_THINKING_LEVEL, None]

            for thinking_level in thinking_attempts:
                try:
                    text = await self.orchestrator.vertex.generate_stage_detection(
                        model=self.settings.vertex_stage_model,
                        system_prompt=stage_detection_system_prompt(),
                        user_content=user_content,
                        temperature=0.0,
                        thinking_level=thinking_level,
                    )
                    stage, confidence = parse_stage_detection(text)
                    stage = clamp_stage_forward(request.current_stage, stage)
                    model_label = self.settings.vertex_stage_model
                    if thinking_level is None:
                        model_label = f"{model_label} (no-thinking retry)"
                    return (
                        stage_preview(
                            stage,
                            provider="vertex",
                            model=model_label,
                            confidence=confidence,
                        ),
                        {
                            "detectorProvider": "vertex",
                            "detectorModel": model_label,
                            "stageChanged": stage != previous_stage,
                        },
                        request,
                    )
                except (ProviderError, ValueError, json.JSONDecodeError) as exc:
                    errors.append(
                        f"vertex/{self.settings.vertex_stage_model}"
                        f"[{thinking_level or 'no-thinking'}]: {exc}"
                    )

        fallback_stage = current_stage or DEFAULT_STAGE_TAG
        return (
            stage_preview(
                fallback_stage,
                provider="fallback",
                model="last-known-stage" if not errors else "last-known-stage after vertex retry",
                confidence=0.0,
            ),
            {
                "detectorProvider": "fallback",
                "detectorModel": "last-known-stage" if not errors else "last-known-stage after vertex retry",
                "stageChanged": False,
            },
            request,
        )

    async def _generate_seller_line_current(self, *, initial: bool, trigger_reason: str) -> None:
        async with self.lock:
            messages = self._messages_snapshot_locked()
            stage_data = dict(self.stage or stage_preview(DEFAULT_STAGE_TAG, provider="preset", model="preset", confidence=None))
            self.status = "seller_thinking"
            self.error = None
        await self._publish_state()
        await self._start_reply_generation(
            initial=initial,
            messages=messages,
            stage_data=stage_data,
            kind="opener" if initial else "reply",
            trigger_reason=trigger_reason,
            await_completion=True,
        )

    async def _start_reply_generation(
        self,
        *,
        initial: bool,
        messages: list[Message],
        stage_data: dict[str, object],
        kind: str,
        trigger_reason: str,
        await_completion: bool,
    ) -> None:
        await self._cancel_reply_task()
        generation_id = f"reply-{uuid.uuid4().hex[:8]}"
        async with self.lock:
            self.reply_generation_id = generation_id
            self.pending_seller = {
                "text": "",
                "model": self.settings.vertex_model,
                "provider": "vertex",
                "kind": kind,
                "updatedAt": time.time(),
                "streaming": True,
                "triggerReason": trigger_reason,
                "generationId": generation_id,
            }
        await self._publish_state()

        task = asyncio.create_task(
            self._run_reply_generation(
                generation_id=generation_id,
                initial=initial,
                messages=messages,
                stage_data=stage_data,
                kind=kind,
                trigger_reason=trigger_reason,
            )
        )
        self.reply_task = task
        if await_completion:
            await task

    async def _run_reply_generation(
        self,
        *,
        generation_id: str,
        initial: bool,
        messages: list[Message],
        stage_data: dict[str, object],
        kind: str,
        trigger_reason: str,
    ) -> None:
        text_parts: list[str] = []
        try:
            async for delta in self.orchestrator.vertex.stream_text(
                system_prompt=SELLER_SYSTEM_PROMPT,
                user_content=self._seller_user_content_from(
                    messages=messages,
                    stage_data=stage_data,
                    initial=initial,
                ),
                temperature=0.6 if initial else 0.5,
                thinking_level=VERTEX_SELLER_THINKING_LEVEL,
            ):
                if not delta:
                    continue
                async for fragment in self._paced_fragments(delta, self.config.seller_wpm):
                    text_parts.append(fragment)
                    partial_text = self._streaming_text("".join(text_parts))
                    async with self.lock:
                        if self.reply_generation_id != generation_id or self.pending_seller is None:
                            return
                        self.pending_seller["text"] = partial_text
                        self.pending_seller["updatedAt"] = time.time()
                        self.pending_seller["streaming"] = True
                    await self._publish_state()

            text = sanitize_seller_line("".join(text_parts))
            if not text:
                raise RuntimeError("Gemini не вернула реплику продавца.")
            async with self.lock:
                if self.reply_generation_id != generation_id:
                    return
                self.pending_seller = {
                    "text": text,
                    "model": self.settings.vertex_model,
                    "provider": "vertex",
                    "kind": kind,
                    "updatedAt": time.time(),
                    "streaming": False,
                    "triggerReason": trigger_reason,
                    "generationId": generation_id,
                }
            await self._publish_state()
        finally:
            if self.reply_task is asyncio.current_task():
                self.reply_task = None

    async def _reply_refresh_gate(
        self,
        *,
        context: str,
        current_text: str | None,
        force: bool,
    ) -> dict[str, str]:
        if not self.orchestrator.cerebras.configured():
            return {
                "action": "suggest",
                "provider": "fallback",
                "model": "fallback",
            }
        verdict = await self.orchestrator._cerebras_live_validator(  # noqa: SLF001
            LiveRequest(
                run_id=self.run_id,
                content=context,
                current_text=current_text,
                force=force,
            )
        )
        return {
            "action": verdict["action"],
            "provider": "cerebras",
            "model": self.settings.cerebras_model,
        }

    async def _semantic_trigger_gate(self, *, context: str) -> dict[str, str]:
        if not self.orchestrator.cerebras.configured():
            return {
                "action": "suggest",
                "provider": "fallback",
                "model": "fallback",
            }
        text = await self.orchestrator.cerebras.text(
            model=self.settings.cerebras_model,
            system_prompt=SEMANTIC_TRIGGER_SYSTEM_PROMPT,
            user_content=context,
            temperature=0.0,
            prompt_cache_key=f"fresh-start-semantic-trigger-v1-{self.run_id}",
            max_tokens=32,
            response_format=cerebras_structured_response_format(),
        )
        suggestion = parse_json_suggestion(text)
        return {
            "action": suggestion["action"],
            "provider": "cerebras",
            "model": self.settings.cerebras_model,
        }

    async def _detect_stage_fast(
        self,
        *,
        context: str,
        current_stage: str,
    ) -> tuple[dict[str, object], dict[str, object], StageRequest]:
        request = StageRequest(
            run_id=self.run_id,
            context=context,
            current_stage=current_stage or None,
        )
        previous_stage = current_stage or DEFAULT_STAGE_TAG

        if self.orchestrator.cerebras.configured():
            user_content = (
                f"{request.context}\n\n"
                "--- Текущий stage из предыдущего шага ---\n"
                f"{request.current_stage or '(пока неизвестен)'}\n"
            )
            text = await self.orchestrator.cerebras.text(
                model=self.settings.cerebras_stage_model,
                system_prompt=stage_detection_system_prompt(),
                user_content=user_content,
                temperature=0.0,
                prompt_cache_key=f"fresh-start-stage-detect-v1-{self.run_id}",
                max_tokens=96,
                response_format=cerebras_stage_response_format(),
            )
            stage, confidence = parse_stage_detection(text)
            stage = clamp_stage_forward(request.current_stage, stage)
            return (
                stage_preview(
                    stage,
                    provider="cerebras",
                    model=self.settings.cerebras_stage_model,
                    confidence=confidence,
                ),
                {
                    "detectorProvider": "cerebras",
                    "detectorModel": self.settings.cerebras_stage_model,
                    "stageChanged": stage != previous_stage,
                },
                request,
            )

        response = await self.orchestrator.stage_agenda(request)
        if response is None:
            raise RuntimeError("Не удалось определить stage.")
        return (
            response.model_dump(),
            {
                "detectorProvider": response.provider,
                "detectorModel": response.model,
                "stageChanged": response.stage != previous_stage,
            },
            request,
        )

    def _start_scorecard_refresh(self, *, request: StageRequest, stage: str) -> None:
        if self.scorecard_task is not None:
            self.scorecard_task.cancel()
        version = self.turn_version = self.turn_version + 1
        self.scorecard_task = asyncio.create_task(
            self._refresh_scorecard(version=version, request=request, stage=stage)
        )

    async def _refresh_scorecard(
        self,
        *,
        version: int,
        request: StageRequest,
        stage: str,
    ) -> None:
        try:
            scorecard = await self.orchestrator._stage_scorecard(  # noqa: SLF001
                request,
                STAGE_AGENDA_BY_TAG[stage],
            )
        except Exception as exc:
            async with self.lock:
                if version != self.turn_version:
                    return
                self.stage_status = f"scorecard error: {exc}"
            await self._publish_state()
            return

        async with self.lock:
            if version != self.turn_version or self.stage is None:
                return
            if str(self.stage.get("stage")) != stage:
                return
            self.stage["scorecard"] = (
                scorecard.model_dump() if hasattr(scorecard, "model_dump") else scorecard
            )
            if self.stage_status != "error":
                self.stage_status = "ready"
        await self._publish_state()

    def _client_user_content(self, *, messages: list[Message], seller_text: str) -> str:
        return (
            f"Продукт / контекст:\n{event_facts()}\n\n"
            f"Референс-персона:\nСценарий {self.reference.number}: {self.reference.title}\n"
            f"Персона: {self.reference.persona}\n\n"
            "Референс-дуга клиента, НЕ копируй дословно:\n"
            f"{client_reference_arc(self.reference)}\n\n"
            f"История разговора:\n{dialogue_tail(self._history_pairs_from(messages))}\n\n"
            f"Последняя реплика продавца:\nSeller: {seller_text}\n\n"
            "Дай следующую живую реплику клиента."
        )

    def _seller_user_content_from(
        self,
        *,
        messages: list[Message],
        stage_data: dict[str, object],
        initial: bool,
    ) -> str:
        lines = [
            "--- Событие / продукт ---",
            event_facts(),
            "",
            "--- Сценарий клиента ---",
            f"Сценарий {self.reference.number}: {self.reference.title}",
            f"Персона: {self.reference.persona}",
            f"Режим клиента: {self.config.persona_mode}",
            f"Стиль продавца: {seller_style_hint(self.config.persona_mode)}",
            "",
            "--- Диалог ---",
            self._dialogue_block(messages),
            "",
            "--- Текущий stage ---",
            f"Stage: {stage_data.get('stage') or DEFAULT_STAGE_TAG}",
            f"Title: {stage_data.get('title') or STAGE_AGENDA_BY_TAG[DEFAULT_STAGE_TAG].title}",
            f"Agenda: {stage_data.get('agenda') or STAGE_AGENDA_BY_TAG[DEFAULT_STAGE_TAG].agenda}",
            f"Emotion: {stage_data.get('emotion') or STAGE_AGENDA_BY_TAG[DEFAULT_STAGE_TAG].emotion}",
            f"Step: {stage_data.get('step') or STAGE_AGENDA_BY_TAG[DEFAULT_STAGE_TAG].step}",
        ]
        scorecard = stage_data.get("scorecard")
        if isinstance(scorecard, dict):
            lines.extend(
                [
                    "",
                    "--- Scorecard ---",
                    f"Readiness: {scorecard.get('readiness_label') or scorecard.get('readiness')}",
                    f"Summary: {scorecard.get('summary') or '(нет)'}",
                    f"Next action: {scorecard.get('next_action') or '(нет)'}",
                ]
            )
        lines.extend(
            [
                "",
                "--- Задача ---",
                (
                    "Дай стартовую opener-реплику продавца."
                    if initial
                    else "Дай одну следующую лучшую реплику продавца после текущего состояния разговора."
                ),
            ]
        )
        return "\n".join(lines)

    def _stage_context_from(self, messages: list[Message]) -> str:
        return "\n".join(
            [
                "Живой high-check B2C sales roleplay.",
                "",
                "--- Событие / продукт ---",
                event_facts(),
                "",
                "--- Диалог ---",
                self._dialogue_block(messages),
            ]
        )

    def _live_gate_context_from(self, messages: list[Message]) -> str:
        lines = [
            "Живой high-check B2C sales roleplay.",
            "",
            "--- Событие / продукт ---",
            event_facts(),
            "",
            "--- Диалог ---",
            self._dialogue_block(messages),
        ]
        if self.stage:
            lines.extend(
                [
                    "",
                    "--- Текущий stage / agenda ---",
                    f"Stage: {self.stage.get('stage')}",
                    f"Agenda: {self.stage.get('agenda')}",
                    f"Step: {self.stage.get('step')}",
                ]
            )
        return "\n".join(lines)

    def _dialogue_block(self, messages: list[Message]) -> str:
        parts = []
        for message in messages:
            text = compact_text(message.text)
            if not text:
                continue
            parts.append(f"{message.history_role()}: {text}")
        return "\n".join(parts) if parts else "(диалог пока не начался)"

    def _append_message(self, role: Literal["seller", "client"], text: str) -> Message:
        message = Message(
            id=f"{role}-{len(self.messages) + 1}",
            role=role,
            text=compact_text(text),
            created_at=time.time(),
        )
        self.messages.append(message)
        return message

    def _find_message_locked(self, message_id: str) -> Message | None:
        return self._find_message_in(self.messages, message_id)

    def _find_message_in(self, messages: list[Message], message_id: str) -> Message | None:
        for message in messages:
            if message.id == message_id:
                return message
        return None

    def _messages_snapshot_locked(self, *, skip_message_id: str | None = None) -> list[Message]:
        return [
            Message(
                id=message.id,
                role=message.role,
                text=message.text,
                created_at=message.created_at,
            )
            for message in self.messages
            if message.id != skip_message_id
        ]

    def _history_pairs_from(self, messages: list[Message]) -> list[tuple[str, str]]:
        return [
            (message.history_role(), compact_text(message.text))
            for message in messages
            if compact_text(message.text)
        ]

    def _require_active_turn_locked(self, turn_id: str) -> ActiveTurn:
        if self.active_turn is None or self.active_turn.id != turn_id:
            raise RuntimeError("Активный ход уже завершен.")
        return self.active_turn

    def _streaming_text(self, raw: str) -> str:
        return re.sub(r"\s+", " ", raw.replace("\n", " ")).strip()

    async def _paced_fragments(self, text: str, wpm: int) -> AsyncIterator[str]:
        fragments = speech_fragments(text)
        if not fragments:
            return
        words_per_second = max(clamp_wpm(wpm) / 60.0, 0.1)
        for index, fragment in enumerate(fragments):
            if index > 0:
                word_count = max(len(re.findall(r"\S+", fragment)), 1)
                await asyncio.sleep(word_count / words_per_second)
            yield fragment

app_state: dict[str, object] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    session = RoleplaySession(app_state["initial_config"])
    app_state["session"] = session
    await session.bootstrap()
    try:
        yield
    finally:
        await session.aclose()


app = FastAPI(title="Fresh Start Chat Loop", lifespan=lifespan)


def session() -> RoleplaySession:
    value = app_state.get("session")
    if not isinstance(value, RoleplaySession):
        raise RuntimeError("Session is not ready.")
    return value


def sse_snapshot(version: int, snapshot: dict[str, object]) -> str:
    payload = json.dumps({"version": version, "snapshot": snapshot}, ensure_ascii=False)
    return f"event: snapshot\ndata: {payload}\n\n"


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(UI_HTML.read_text(encoding="utf-8"))


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True, "status": session().status})


@app.get("/api/session")
async def get_session() -> JSONResponse:
    return JSONResponse(await session().snapshot_now())


@app.get("/api/session/stream")
async def stream_session(request: Request) -> StreamingResponse:
    async def events():
        last_version, snapshot = await session().current_snapshot_with_version()
        yield sse_snapshot(last_version, snapshot)
        while True:
            if await request.is_disconnected():
                return
            result = await session().wait_for_snapshot(last_version, timeout_secs=15.0)
            if result is None:
                yield ": keepalive\n\n"
                continue
            last_version, snapshot = result
            yield sse_snapshot(last_version, snapshot)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/session/reset")
async def reset_session(body: ResetRequest) -> JSONResponse:
    try:
        snapshot = await session().reset(body)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse(snapshot)


@app.post("/api/advance")
async def advance_turn() -> JSONResponse:
    try:
        snapshot = await session().advance_turn()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return JSONResponse(snapshot)


@app.post("/api/seller/regenerate")
async def regenerate_seller() -> JSONResponse:
    try:
        snapshot = await session().regenerate_seller()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return JSONResponse(snapshot)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    for env_file in args.env_file:
        load_env_file(env_file)
    app_state["initial_config"] = ResetRequest()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
