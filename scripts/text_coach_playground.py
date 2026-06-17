#!/usr/bin/env python3
"""Text-only seller/client roleplay playground backed by the shared coach service."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from live_client_voice_agent import (
    DEFAULT_CEREBRAS_API_BASE,
    DEFAULT_CLIENT_ACTOR_MODEL,
    DEFAULT_INPUT,
    event_facts,
    generate_client_reply,
    load_env_file,
    parse_reference_script,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
UI_HTML = (
    Path(__file__).resolve().parent / "text_coach_playground_ui" / "index.html"
)
DEFAULT_SERVICE_URL = "http://127.0.0.1:8088"


class ResetRequest(BaseModel):
    script: int = 2
    persona_mode: Literal["neutral", "cold", "hostile"] = "hostile"


@dataclass
class TranscriptMessage:
    id: str
    role: Literal["seller", "client"]
    text: str
    created_at: float

    @property
    def speaker(self) -> str:
        return "Продавец" if self.role == "seller" else "Клиент"

    def history_role(self) -> str:
        return "Seller" if self.role == "seller" else "Client"

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "role": self.role,
            "speaker": self.speaker,
            "text": self.text,
            "createdAt": self.created_at,
        }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8097)
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


class RoleplaySession:
    def __init__(self, initial_config: ResetRequest):
        self.initial_config = initial_config
        self.lock = asyncio.Lock()
        self.client = httpx.AsyncClient(timeout=45.0)
        self.service_url = env_var("COACH_LLM_SERVICE_URL") or DEFAULT_SERVICE_URL
        self.service_token = env_var("COACH_LLM_SERVICE_TOKEN")
        self.script_options = list_reference_scripts(DEFAULT_INPUT)
        self._apply_reset(initial_config)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def bootstrap(self) -> None:
        last_error: Exception | None = None
        for attempt in range(6):
            try:
                await self.reset(self.initial_config)
                return
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(min(0.75 * (attempt + 1), 2.5))
        if last_error is not None:
            raise last_error

    def _apply_reset(self, config: ResetRequest) -> None:
        self.config = config.model_copy(deep=True)
        self.run_id = f"web-{uuid.uuid4().hex[:10]}"
        self.messages: list[TranscriptMessage] = []
        self.stage: dict[str, object] | None = None
        self.current_seller_line: dict[str, object] | None = None
        self.help_result: dict[str, object] | None = None
        self.error: str | None = None
        self.status = "booting"
        self.reference = parse_reference_script(DEFAULT_INPUT, config.script)
        self.client_args = build_client_args(config)

    def snapshot(self) -> dict[str, object]:
        return {
            "runId": self.run_id,
            "status": self.status,
            "error": self.error,
            "config": self.config.model_dump(),
            "scriptOptions": self.script_options,
            "messages": [message.as_dict() for message in self.messages],
            "stage": self.stage,
            "currentSellerLine": self.current_seller_line,
            "helpResult": self.help_result,
            "serviceUrl": self.service_url,
            "canAdvance": bool(
                self.current_seller_line and self.status in {"ready", "error"}
            ),
        }

    async def reset(self, config: ResetRequest) -> dict[str, object]:
        async with self.lock:
            self._apply_reset(config)
            try:
                await self._generate_seller_line_locked(initial=True)
                self.status = "ready"
            except Exception as exc:
                self.status = "error"
                self.error = str(exc)
            return self.snapshot()

    async def regenerate_seller_line(self) -> dict[str, object]:
        async with self.lock:
            try:
                await self._generate_seller_line_locked(
                    initial=not any(message.role == "seller" for message in self.messages)
                )
                self.status = "ready"
            except Exception as exc:
                self.status = "error"
                self.error = str(exc)
                raise
            return self.snapshot()

    async def advance_turn(self) -> dict[str, object]:
        async with self.lock:
            if not self.current_seller_line or not self.current_seller_line.get("text"):
                raise RuntimeError("Сначала дождись или перегенерируй реплику продавца.")

            seller_text = str(self.current_seller_line["text"]).strip()
            self._append_message("seller", seller_text)
            self.current_seller_line = None
            self.help_result = None
            self.error = None
            self.status = "client_reply"

            client_text = await asyncio.to_thread(
                generate_client_reply,
                args=self.client_args,
                reference=self.reference,
                history=self._history_pairs(),
                seller_transcript=seller_text,
            )
            self._append_message("client", client_text)

            try:
                await self._detect_stage_locked()
            except Exception:
                pass

            await self._generate_seller_line_locked(initial=False)
            self.status = "ready"
            return self.snapshot()

    async def detect_stage(self) -> dict[str, object]:
        async with self.lock:
            try:
                await self._detect_stage_locked()
                self.status = "ready"
            except Exception as exc:
                self.status = "error"
                self.error = str(exc)
                raise
            return self.snapshot()

    async def help(self) -> dict[str, object]:
        async with self.lock:
            if not self.messages and not self.current_seller_line:
                raise RuntimeError("Сначала нужен хотя бы стартовый контекст разговора.")

            if self.stage is None and self.messages:
                with suppress(Exception):
                    await self._detect_stage_locked()

            self.status = "helping"
            self.error = None
            payload = {
                "id": int(time.time() * 1000) % 1_000_000_000,
                "run_id": self.run_id,
                "context": self.render_help_context(),
            }
            try:
                opener_response = await self._post("/v1/coach/help/opener", payload)
                opener = opener_response.json()
                constructive = await self._collect_sse_text(
                    "/v1/coach/help/constructive/stream",
                    payload,
                )
            except Exception as exc:
                self.status = "error"
                self.error = str(exc)
                raise

            self.help_result = {
                "openerText": opener["text"].strip(),
                "openerModel": opener.get("model"),
                "openerFallback": opener.get("fallback", False),
                "constructiveText": constructive["text"].strip(),
                "constructiveModel": constructive.get("model"),
                "updatedAt": time.time(),
            }
            self.status = "ready"
            return self.snapshot()

    async def _generate_seller_line_locked(self, *, initial: bool) -> None:
        self.status = "seller_thinking"
        self.error = None
        payload = {
            "id": int(time.time() * 1000) % 1_000_000_000,
            "run_id": self.run_id,
            "context": self.render_chat_context(),
            "question": self._seller_question(initial=initial),
        }
        data = await self._collect_sse_text("/v1/coach/chat/stream", payload)
        text = sanitize_seller_line(data["text"])
        if not text:
            raise RuntimeError("Backend не вернул реплику продавца.")
        self.current_seller_line = {
            "text": text,
            "model": data.get("model"),
            "updatedAt": time.time(),
            "kind": "opener" if initial else "followup",
        }

    async def _detect_stage_locked(self) -> None:
        if not self.messages:
            return
        self.status = "detecting_stage"
        payload = {
            "run_id": self.run_id,
            "context": self.render_live_context(),
            "current_stage": self.stage["stage"] if self.stage else None,
        }
        response = await self._post("/v1/coach/stage", payload)
        if response.status_code == 204:
            return
        data = response.json()
        self.stage = {
            "stage": data["stage"],
            "title": data["title"],
            "agenda": data["agenda"],
            "emotion": data["emotion"],
            "step": data["step"],
            "provider": data["provider"],
            "model": data["model"],
            "scorecard": data.get("scorecard"),
        }

    async def _post(
        self, path: str, payload: dict[str, object]
    ) -> httpx.Response:
        response = await self.client.post(
            self._service_url(path),
            headers=self._headers(),
            json=payload,
        )
        self._ensure_success(response)
        return response

    async def _collect_sse_text(
        self, path: str, payload: dict[str, object]
    ) -> dict[str, object]:
        model: str | None = None
        parts: list[str] = []
        async with self.client.stream(
            "POST",
            self._service_url(path),
            headers=self._headers(),
            json=payload,
        ) as response:
            self._ensure_success(response)
            async for raw_line in response.aiter_lines():
                if not raw_line.startswith("data:"):
                    continue
                data = raw_line.removeprefix("data:").strip()
                if not data:
                    continue
                event = json.loads(data)
                if event.get("event") == "model":
                    model = event.get("model")
                elif event.get("event") == "delta":
                    parts.append(event.get("text") or "")
                elif event.get("event") == "error":
                    raise RuntimeError(event.get("message") or "SSE stream error")
                elif event.get("event") == "done":
                    break
        return {"model": model, "text": "".join(parts).strip()}

    def _service_url(self, path: str) -> str:
        base = self.service_url.rstrip("/")
        return f"{base}{path if path.startswith('/') else '/' + path}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.service_token:
            headers["Authorization"] = f"Bearer {self.service_token}"
        return headers

    def _ensure_success(self, response: httpx.Response) -> None:
        if response.is_success or response.status_code == 204:
            return
        detail = response.text.strip() or f"HTTP {response.status_code}"
        try:
            payload = response.json()
            detail = payload.get("detail") or detail
        except ValueError:
            pass
        raise RuntimeError(f"LLM service HTTP {response.status_code}: {detail}")

    def _append_message(self, role: Literal["seller", "client"], text: str) -> None:
        self.messages.append(
            TranscriptMessage(
                id=f"{role}-{len(self.messages) + 1}",
                role=role,
                text=text.strip(),
                created_at=time.time(),
            )
        )

    def _history_pairs(self) -> list[tuple[str, str]]:
        return [(message.history_role(), message.text) for message in self.messages]

    def _seller_question(self, *, initial: bool) -> str:
        if initial:
            return (
                "Дай одну короткую opener-реплику продавца для старта разговора про "
                "glubina.core в Казани. Это именно начало: установить контакт, задать "
                "рамку, получить permission на пару вопросов. Без раннего оффера, без "
                "объяснений, без markdown. Ровно 1-2 предложения, готовых к зачитыванию."
            )
        return (
            "Дай одну лучшую следующую реплику продавца, которую можно сразу сказать "
            "клиенту сейчас. Это должны быть готовые слова продавца, а не инструкция. "
            "Можно задать вопрос, уточнить факт, двинуть stage или мягко вернуть фокус. "
            "Ровно 1-2 предложения, без markdown и без пояснений."
        )

    def render_live_context(self) -> str:
        lines = [
            'Живой B2C sales call. Роли размечены как "Продавец:" и "Клиент:".',
            "",
            "--- Событие / продукт ---",
            event_facts(),
            "",
            "--- Диалог ---",
        ]
        if not self.messages:
            lines.append("(диалог пока не начался)")
        else:
            for message in self.messages:
                lines.append(f"{message.speaker}: {message.text}")
        lines.extend(["", "--- Уже показанные подсказки тренера ---"])
        if self.current_seller_line and self.current_seller_line.get("text"):
            lines.append(str(self.current_seller_line["text"]).strip())
        else:
            lines.append("(пока нет)")
        lines.extend(
            ["", "Верни строго EOS или BOS + один короткий абзац на 2-3 предложения."]
        )
        return "\n".join(lines)

    def render_chat_context(self) -> str:
        lines = [
            "Снимок контекста на момент запроса следующей реплики продавца.",
            "",
            "--- Событие / продукт ---",
            event_facts(),
            "",
            "--- Диалог ---",
        ]
        if not self.messages:
            lines.append("(диалог пока не начался)")
        else:
            for message in self.messages:
                lines.append(f"{message.speaker}: {message.text}")

        lines.extend(["", "--- Текущий stage / agenda ---"])
        if self.stage:
            lines.append(f"Stage: {self.stage['stage']}")
            lines.append(f"Title: {self.stage['title']}")
            lines.append(f"Agenda: {self.stage['agenda']}")
            lines.append(f"Step: {self.stage['step']}")
        else:
            lines.append("(stage пока не определен)")

        lines.extend(["", "--- Уже предложенная реплика продавца ---"])
        if self.current_seller_line and self.current_seller_line.get("text"):
            lines.append(str(self.current_seller_line["text"]).strip())
        else:
            lines.append("(пока нет)")
        return "\n".join(lines)

    def render_help_context(self) -> str:
        lines = [
            "CONTEXT_VERSION: text-roleplay-help-v1",
            "",
            "--- Событие / продукт ---",
            event_facts(),
            "",
            "--- Диалог, последние фрагменты ---",
        ]
        if not self.messages:
            lines.append("(диалог пока не начался)")
        else:
            for message in self.messages[-18:]:
                lines.append(f"{message.speaker}: {message.text}")

        lines.extend(["", "--- Текущий stage / agenda ---"])
        if self.stage:
            lines.append(f"Stage: {self.stage['stage']}")
            lines.append(f"Title: {self.stage['title']}")
            lines.append(f"Agenda: {self.stage['agenda']}")
            lines.append(f"Эмоциональная рамка: {self.stage['emotion']}")
            lines.append(f"Следующий шаг из mapping: {self.stage['step']}")
        else:
            lines.append("(stage пока не определен)")

        lines.extend(["", "--- Уже предложенная реплика продавца ---"])
        if self.current_seller_line and self.current_seller_line.get("text"):
            lines.append(str(self.current_seller_line["text"]).strip())
        else:
            lines.append("(пока нет)")
        return "\n".join(lines)


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


app = FastAPI(title="Coach Text Roleplay", lifespan=lifespan)


def session() -> RoleplaySession:
    value = app_state.get("session")
    if not isinstance(value, RoleplaySession):
        raise RuntimeError("Session is not ready.")
    return value


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
    return JSONResponse(session().snapshot())


@app.post("/api/session/reset")
async def reset_session(body: ResetRequest) -> JSONResponse:
    try:
        snapshot = await session().reset(body)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse(snapshot)


@app.post("/api/seller/refresh")
async def regenerate_seller_line() -> JSONResponse:
    try:
        snapshot = await session().regenerate_seller_line()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return JSONResponse(snapshot)


@app.post("/api/seller/advance")
async def advance_turn() -> JSONResponse:
    try:
        snapshot = await session().advance_turn()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return JSONResponse(snapshot)


@app.post("/api/stage")
async def detect_stage() -> JSONResponse:
    try:
        snapshot = await session().detect_stage()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return JSONResponse(snapshot)


@app.post("/api/help")
async def help_action() -> JSONResponse:
    try:
        snapshot = await session().help()
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
