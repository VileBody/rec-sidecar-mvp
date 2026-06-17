from __future__ import annotations

import asyncio
import json
import logging
import ssl
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .providers import (
    ProviderError,
    VertexClient,
    compact_json,
    vertex_live_error_message,
    vertex_live_response_text,
    vertex_live_turn_complete,
)
from .scorecard import RawScorecard, STAGE_SCORECARD_DEFINITIONS
from .stage_assets import (
    CURRENT_STAGE_AGENDA_PROMPT,
    STAGE_AGENDA_BY_TAG,
    extract_json_object,
    normalize_stage,
)

try:
    import certifi
    from websockets.asyncio.client import connect
except ImportError:  # pragma: no cover - covered by deployment dependency checks.
    certifi = None  # type: ignore[assignment]
    connect = None  # type: ignore[assignment]


logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class LiveIntelligenceResult:
    stage: str
    confidence: float | None
    scorecard: RawScorecard
    raw_text: str


class LiveIntelligenceNoUpdate(Exception):
    """Raised when the live model intentionally emits no UI update."""


class VertexLiveIntelligenceSession:
    def __init__(self, vertex: VertexClient, *, model: str, timeout_secs: float):
        self.vertex = vertex
        self.model = model
        self.timeout_secs = timeout_secs
        self._ws: Any | None = None
        self._lock = asyncio.Lock()

    async def analyze(
        self,
        *,
        context: str,
        current_stage: str | None,
    ) -> LiveIntelligenceResult:
        async with self._lock:
            await self._ensure_connected()
            try:
                await self._send(
                    {
                        "client_content": {
                            "turns": [
                                {
                                    "role": "user",
                                    "parts": [
                                        {
                                            "text": live_intelligence_user_prompt(
                                                context=context,
                                                current_stage=current_stage,
                                            )
                                        }
                                    ],
                                }
                            ],
                            "turn_complete": True,
                        }
                    }
                )
                text = await self._receive_turn_text()
                return parse_live_intelligence_response(text)
            except Exception:
                await self.aclose()
                raise

    async def aclose(self) -> None:
        if self._ws is None:
            return
        ws = self._ws
        self._ws = None
        with suppress(Exception):
            await ws.close()

    async def _ensure_connected(self) -> None:
        if self._ws is not None:
            return
        if connect is None:
            raise ProviderError("vertex-live", "websockets package is not installed")
        try:
            self._ws = await connect(
                self.vertex.live_bidi_url(),
                additional_headers=await self.vertex.auth_headers(),
                ssl=vertex_live_ssl_context(),
                open_timeout=min(10.0, self.timeout_secs),
                ping_interval=20,
                ping_timeout=20,
                max_size=2 * 1024 * 1024,
            )
            await self._send(
                {
                    "setup": {
                        "model": self.vertex.model_resource(self.model),
                        "system_instruction": {
                            "parts": [{"text": live_intelligence_system_prompt()}]
                        },
                        "generation_config": {
                            "response_modalities": ["TEXT"],
                            "temperature": 0.0,
                            "max_output_tokens": 1400,
                        },
                    }
                }
            )
            await self._consume_optional_setup_ack()
        except ProviderError:
            await self.aclose()
            raise
        except Exception as exc:
            await self.aclose()
            raise ProviderError("vertex-live", f"connect failed: {exc}") from exc

    async def _send(self, value: dict[str, Any]) -> None:
        if self._ws is None:
            raise ProviderError("vertex-live", "websocket is not connected")
        await self._ws.send(json.dumps(value, ensure_ascii=False))

    async def _consume_optional_setup_ack(self) -> None:
        try:
            raw = await asyncio.wait_for(self._recv_json(), timeout=min(5.0, self.timeout_secs))
        except TimeoutError:
            return
        message = vertex_live_error_message(raw)
        if message:
            raise ProviderError("vertex-live", message)

    async def _receive_turn_text(self) -> str:
        parts: list[str] = []
        deadline = asyncio.get_running_loop().time() + self.timeout_secs
        while True:
            timeout = deadline - asyncio.get_running_loop().time()
            if timeout <= 0:
                raise ProviderError("vertex-live", f"timeout after {self.timeout_secs:.0f}s")

            value = await asyncio.wait_for(self._recv_json(), timeout=timeout)
            message = vertex_live_error_message(value)
            if message:
                raise ProviderError("vertex-live", message)

            text = vertex_live_response_text(value)
            if text:
                parts.append(text)
            if vertex_live_turn_complete(value):
                break

        combined = "".join(parts).strip()
        if not combined:
            raise LiveIntelligenceNoUpdate("live intelligence unchanged")
        return combined

    async def _recv_json(self) -> dict[str, Any]:
        if self._ws is None:
            raise ProviderError("vertex-live", "websocket is not connected")
        raw = await self._ws.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise ProviderError("vertex-live", f"invalid JSON frame: {raw!r}") from exc
        if not isinstance(value, dict):
            raise ProviderError("vertex-live", f"unexpected frame: {compact_json(value)}")
        return value


def live_intelligence_system_prompt() -> str:
    return (
        "Ты realtime intelligence engine для high-ticket B2C sales call.\n"
        "Тебе приходят последовательные срезы транскрибации. На каждый срез верни один "
        "строгий JSON без Markdown и без пояснений, только если есть содержательное изменение.\n\n"
        "Задачи:\n"
        "1. Определи текущий sales stage.\n"
        "2. Для выбранного stage оцени scorecard checks через hit/miss/pending/uncertain/na.\n"
        "3. Сформулируй короткий next_action для продавца.\n\n"
        "Правило движения stage:\n"
        "- Stage может стоять на месте или двигаться вперед, но не назад. Если в новом срезе "
        "снова обсуждают тему прошлой стадии, сохрани текущий stage и дай уточнение внутри него.\n\n"
        "Правила next_action:\n"
        '- Если readiness еще не зеленый, начни с "Уточнить:" и дай одну готовую фразу-вопрос.\n'
        '- Если можно переходить дальше, начни с "Переход:" и дай одну конкретную фразу.\n'
        "- После префикса пиши именно слова продавца клиенту от первого лица, пригодные "
        'для чтения вслух; не пиши мета-инструкции вроде "спроси", "уточни", "скажи".\n'
        "- Не пересказывай статичный канонический шаг, адаптируй к словам клиента.\n"
        "- Не выдумывай факты: evidence только из транскрипта, короткими цитатами.\n\n"
        "Правило тишины:\n"
        "- Если stage, readiness/checks и next_action по сути не изменились относительно "
        "предыдущего шага, не пиши ничего: заверши turn пустым ответом без JSON, без слов "
        'вроде "no change" и без пояснений.\n'
        "- Пиши JSON только когда появился новый факт, изменилась стадия, изменился "
        "светофор/метрики или нужен новый совет продавцу.\n\n"
        "Формат JSON:\n"
        "{\n"
        '  "stage": "S2.2",\n'
        '  "confidence": 0.7,\n'
        '  "summary": "короткий вывод по стадии",\n'
        '  "next_action": "Уточнить: ...",\n'
        '  "checks": [\n'
        '    {"id":"...", "result":"hit|miss|pending|uncertain|na", '
        '"reason":"...", "evidence":[{"speaker":"Клиент","quote":"..."}]}\n'
        "  ]\n"
        "}\n\n"
        "Stage -> agenda mapping:\n"
        f"{CURRENT_STAGE_AGENDA_PROMPT}\n\n"
        "Stage scorecard definitions. Для выбранного stage используй эти id:\n"
        f"{scorecard_definitions_text()}"
    )


def vertex_live_ssl_context() -> ssl.SSLContext:
    if certifi is None:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def live_intelligence_user_prompt(*, context: str, current_stage: str | None) -> str:
    return (
        "--- Последние реплики / контекст звонка ---\n"
        f"{context}\n\n"
        "--- Текущий stage из предыдущего шага ---\n"
        f"{current_stage or '(пока неизвестен)'}\n\n"
        "Определи актуальный stage прямо сейчас и верни JSON по указанному формату. "
        "Не возвращай stage назад относительно предыдущего шага. "
        "Если данных мало, сохрани ближайший stage, но checks оставь pending/uncertain. "
        "Если относительно предыдущего шага ничего содержательно не изменилось, не пиши ничего."
    )


def scorecard_definitions_text() -> str:
    lines: list[str] = []
    for stage, definitions in STAGE_SCORECARD_DEFINITIONS.items():
        agenda = STAGE_AGENDA_BY_TAG.get(stage)
        title = agenda.title if agenda else stage
        lines.append(f"{stage} — {title}")
        for definition in definitions:
            lines.append(
                f"- {definition.id} [{definition.level}/{definition.signal}]: "
                f"HIT: {definition.hit} MISS: {definition.miss}"
            )
    return "\n".join(lines)


def parse_live_intelligence_response(text: str) -> LiveIntelligenceResult:
    if not text.strip():
        raise LiveIntelligenceNoUpdate("live intelligence unchanged")

    value = extract_json_object(text)
    if not isinstance(value, dict):
        raise ValueError(f"live intelligence response is not JSON object: {text!r}")

    stage = normalize_stage(str(value.get("stage", "")))
    if stage not in STAGE_AGENDA_BY_TAG:
        raise ValueError(f"unknown live intelligence stage: {value.get('stage')!r}")

    confidence_value = value.get("confidence")
    confidence = confidence_value if isinstance(confidence_value, (int, float)) else None
    scorecard_value = value.get("scorecard")
    if not isinstance(scorecard_value, dict):
        scorecard_value = value

    try:
        scorecard = RawScorecard.model_validate(
            {
                "summary": scorecard_value.get("summary", ""),
                "next_action": scorecard_value.get("next_action", ""),
                "checks": scorecard_value.get("checks", []),
            }
        )
    except ValidationError as exc:
        raise ValueError(f"invalid live intelligence scorecard: {exc}") from exc

    return LiveIntelligenceResult(
        stage=stage,
        confidence=confidence,
        scorecard=scorecard,
        raw_text=text,
    )
