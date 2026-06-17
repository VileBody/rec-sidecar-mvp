#!/usr/bin/env python3
"""Small local web app for live seller/client roleplay."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from live_client_voice_agent import (
    DEFAULT_CHUNK_MS,
    DEFAULT_CLIENT_ACTOR_MODEL,
    DEFAULT_CEREBRAS_API_BASE,
    DEFAULT_INPUT,
    DEFAULT_INWORLD_SELLER_VOICE,
    DEFAULT_INWORLD_CLIENT_VOICE,
    DEFAULT_INWORLD_STT_WS_URL,
    DEFAULT_INWORLD_TTS_API_BASE,
    DEFAULT_INWORLD_TTS_MODEL,
    SessionLogger,
    generate_client_reply,
    load_env_file,
    parse_reference_script,
    play_audio,
    synthesize_inworld_text,
    write_pcm_wav,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = Path(__file__).resolve().parent / "live_client_chat_ui"
UI_HTML = UI_ROOT / "index.html"
DEFAULT_LOG_ROOT = REPO_ROOT / "logs" / "live_client_chat"


class ResetRequest(BaseModel):
    script: int = 2
    persona_mode: Literal["neutral", "cold", "hostile"] = "hostile"


class SellerMessageRequest(BaseModel):
    text: str


@dataclass
class ChatMessage:
    id: str
    role: Literal["seller", "client"]
    speaker: str
    text: str
    turn: int
    audio_path: Path | None = None
    created_at: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "role": self.role,
            "speaker": self.speaker,
            "text": self.text,
            "turn": self.turn,
            "createdAt": self.created_at,
            "audioUrl": f"/api/audio/{self.id}" if self.audio_path else None,
        }


def parse_app_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8097)
    parser.add_argument("--script", type=int, default=2)
    parser.add_argument("--persona-mode", choices=["neutral", "cold", "hostile"], default="hostile")
    parser.add_argument("--audio-device-index", type=int, default=None)
    parser.add_argument("--env-file", type=Path, action="append", default=[Path(".env"), Path(".env.iac")])
    return parser.parse_args(argv)


def make_output_dir(root: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = root / stamp
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = root / f"{stamp}-{suffix}"
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def list_reference_scripts(path: Path) -> list[dict[str, object]]:
    text = path.read_text(encoding="utf-8")
    return [
        {"number": int(match.group(1)), "title": match.group(2).strip()}
        for match in re.finditer(r"(?m)^## Скрипт\s+(\d+)\.\s*(.+)$", text)
    ]


def build_runtime_args(*, output_dir: Path, script: int, persona_mode: str):
    return argparse.Namespace(
        input=DEFAULT_INPUT,
        script=script,
        output_dir=output_dir,
        audio_device_index=None,
        list_devices=False,
        max_turns=20,
        stt_timeout_secs=12.0,
        stt_idle_finish_secs=1.0,
        chunk_ms=DEFAULT_CHUNK_MS,
        inworld_api_key=None,
        inworld_stt_ws_url=os.getenv("INWORLD_STT_WS_URL", DEFAULT_INWORLD_STT_WS_URL),
        inworld_tts_api_base=os.getenv("INWORLD_TTS_API_BASE", DEFAULT_INWORLD_TTS_API_BASE),
        inworld_tts_model=os.getenv("INWORLD_TTS_MODEL", DEFAULT_INWORLD_TTS_MODEL),
        inworld_seller_voice=os.getenv("INWORLD_TTS_SELLER_VOICE", DEFAULT_INWORLD_SELLER_VOICE),
        inworld_client_voice=os.getenv("INWORLD_TTS_CLIENT_VOICE", DEFAULT_INWORLD_CLIENT_VOICE),
        inworld_language=os.getenv("INWORLD_TTS_LANGUAGE", "ru-RU"),
        client_actor_model=os.getenv("CEREBRAS_MODEL", DEFAULT_CLIENT_ACTOR_MODEL),
        client_actor_temperature=0.85,
        client_actor_max_tokens=220,
        cerebras_api_key=None,
        cerebras_api_base=os.getenv("CEREBRAS_API_BASE", DEFAULT_CEREBRAS_API_BASE),
        cerebras_reasoning_effort=os.getenv("CEREBRAS_REASONING_EFFORT", "none"),
        persona_mode=persona_mode,
        play=False,
        save_audio=True,
        env_file=[Path(".env"), Path(".env.iac")],
    )


class VoiceChatSession:
    def __init__(self, initial_config: ResetRequest):
        self.initial_config = initial_config
        self.lock = asyncio.Lock()
        self.subscribers: set[asyncio.Queue[dict[str, object]]] = set()
        self.script_options = list_reference_scripts(DEFAULT_INPUT)
        self.messages: list[ChatMessage] = []
        self.message_audio: dict[str, Path] = {}
        self.history: list[tuple[str, str]] = []
        self.turn_index = 0
        self.status = "booting"
        self.error: str | None = None
        self.args = None
        self.logger: SessionLogger | None = None
        self.reference = None
        self.config = initial_config.model_copy(deep=True)

    async def subscribe(self) -> asyncio.Queue[dict[str, object]]:
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=64)
        self.subscribers.add(queue)
        await queue.put({"type": "snapshot", "data": self.snapshot()})
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, object]]) -> None:
        self.subscribers.discard(queue)

    async def broadcast(self, event_type: str, data: dict[str, object]) -> None:
        event = {"type": event_type, "data": data}
        stale: list[asyncio.Queue[dict[str, object]]] = []
        for queue in self.subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except Exception:
                    stale.append(queue)
        for queue in stale:
            self.subscribers.discard(queue)

    async def broadcast_snapshot(self) -> None:
        await self.broadcast("snapshot", self.snapshot())

    def snapshot(self) -> dict[str, object]:
        return {
            "status": self.status,
            "error": self.error,
            "config": self.config.model_dump(),
            "messages": [message.as_dict() for message in self.messages],
            "canSend": self.args is not None and self.status in {"ready", "error"},
            "scriptOptions": self.script_options,
            "turnIndex": self.turn_index,
        }

    async def bootstrap(self) -> None:
        try:
            await self.reset(self.initial_config)
        except Exception as exc:
            self.status = "error"
            self.error = str(exc)
            await self.broadcast_snapshot()

    async def reset(self, config: ResetRequest) -> None:
        async with self.lock:
            if self.status not in {"ready", "error", "booting"}:
                raise RuntimeError("Нельзя сбросить сессию во время активного хода.")
            output_dir = make_output_dir(DEFAULT_LOG_ROOT)
            runtime_args = build_runtime_args(
                output_dir=output_dir,
                script=config.script,
                persona_mode=config.persona_mode,
            )
            reference = parse_reference_script(runtime_args.input, config.script)
            logger = SessionLogger(output_dir)
            logger.log(
                "session_start",
                persona_mode=config.persona_mode,
                script_number=reference.number,
                script_title=reference.title,
                mode="text_to_voice",
            )

            self.args = runtime_args
            self.logger = logger
            self.reference = reference
            self.config = config.model_copy(deep=True)
            self.messages = []
            self.message_audio = {}
            self.history = []
            self.turn_index = 0
            self.error = None
            self.status = "ready"
        await self.broadcast_snapshot()

    async def send_message(self, text: str) -> dict[str, object]:
        seller_text = " ".join(text.split()).strip()
        if not seller_text:
            raise RuntimeError("Напиши реплику перед отправкой.")

        async with self.lock:
            logger = self.logger
            reference = self.reference
            args = self.args
            if logger is None or reference is None or args is None:
                raise RuntimeError("Сессия не инициализирована.")
            if self.status not in {"ready", "error"}:
                raise RuntimeError("Дождись завершения текущего хода.")

            self.turn_index += 1
            turn_index = self.turn_index
            self.error = None
            seller_message = ChatMessage(
                id=f"seller-{turn_index}",
                role="seller",
                speaker="Ты",
                text=seller_text,
                turn=turn_index,
                created_at=time.time(),
            )
            self.messages.append(seller_message)
            self.history.append(("Seller", seller_text))
            logger.log("seller_text", turn=turn_index, text=seller_text)
            logger.append_dialogue("Seller", seller_text)
            self.status = "rendering_seller"
        await self.broadcast_snapshot()
        try:
            seller_tts_started_at = time.monotonic()
            seller_pcm = await asyncio.to_thread(
                synthesize_inworld_text,
                args,
                seller_text,
                speaker="Seller",
            )
            seller_tts_elapsed_ms = int((time.monotonic() - seller_tts_started_at) * 1000)
            seller_audio_path = args.output_dir / f"seller_turn_{turn_index:03d}.wav"
            write_pcm_wav(seller_audio_path, seller_pcm)
            logger.log(
                "seller_audio",
                turn=turn_index,
                elapsed_ms=seller_tts_elapsed_ms,
                audio_path=str(seller_audio_path),
            )

            async with self.lock:
                seller_message.audio_path = seller_audio_path
                self.message_audio[seller_message.id] = seller_audio_path
                self.status = "playing_seller"
            await self.broadcast_snapshot()
            await asyncio.to_thread(play_audio, seller_audio_path)

            async with self.lock:
                self.status = "thinking"
            await self.broadcast_snapshot()

            llm_started_at = time.monotonic()
            client_text = await asyncio.to_thread(
                generate_client_reply,
                args=args,
                reference=reference,
                history=list(self.history),
                seller_transcript=seller_text,
            )
            llm_elapsed_ms = int((time.monotonic() - llm_started_at) * 1000)
            logger.log("client_reply_text", turn=turn_index, text=client_text, elapsed_ms=llm_elapsed_ms)

            async with self.lock:
                self.status = "rendering_client"
            await self.broadcast_snapshot()

            client_tts_started_at = time.monotonic()
            client_pcm = await asyncio.to_thread(
                synthesize_inworld_text,
                args,
                client_text,
                speaker="Client",
            )
            client_tts_elapsed_ms = int((time.monotonic() - client_tts_started_at) * 1000)
            client_audio_path = args.output_dir / f"client_turn_{turn_index:03d}.wav"
            write_pcm_wav(client_audio_path, client_pcm)
            logger.log(
                "client_reply_audio",
                turn=turn_index,
                elapsed_ms=client_tts_elapsed_ms,
                audio_path=str(client_audio_path),
            )

            async with self.lock:
                client_message = ChatMessage(
                    id=f"client-{turn_index}",
                    role="client",
                    speaker="Клиент",
                    text=client_text,
                    turn=turn_index,
                    audio_path=client_audio_path,
                    created_at=time.time(),
                )
                self.messages.append(client_message)
                self.message_audio[client_message.id] = client_audio_path
                self.history.append(("Client", client_text))
                logger.append_dialogue("Client", client_text)
                self.status = "playing_client"
            await self.broadcast_snapshot()
            await asyncio.to_thread(play_audio, client_audio_path)

            async with self.lock:
                self.status = "ready"
            await self.broadcast_snapshot()
            return self.snapshot()
        except Exception as exc:
            logger.log("turn_error", turn=turn_index, error=str(exc))
            async with self.lock:
                self.status = "error"
                self.error = str(exc)
            await self.broadcast_snapshot()
            raise

    def audio_path_for(self, message_id: str) -> Path | None:
        return self.message_audio.get(message_id)


app_state: dict[str, object] = {}

@asynccontextmanager
async def lifespan(_: FastAPI):
    manager = VoiceChatSession(app_state["initial_config"])
    app_state["manager"] = manager
    await manager.bootstrap()
    yield


app = FastAPI(title="Live Client Chat", lifespan=lifespan)


def manager() -> VoiceChatSession:
    value = app_state.get("manager")
    if not isinstance(value, VoiceChatSession):
        raise RuntimeError("Session manager is not ready.")
    return value


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(UI_HTML.read_text(encoding="utf-8"))


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True, "status": manager().snapshot()["status"]})


@app.get("/api/session")
async def get_session() -> JSONResponse:
    return JSONResponse(manager().snapshot())


@app.post("/api/session/reset")
async def reset_session(body: ResetRequest) -> JSONResponse:
    try:
        await manager().reset(body)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse(manager().snapshot())


@app.post("/api/message")
async def post_message(body: SellerMessageRequest) -> JSONResponse:
    try:
        snapshot = await manager().send_message(body.text)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse(snapshot)


@app.get("/api/audio/{message_id}")
async def get_audio(message_id: str) -> FileResponse:
    path = manager().audio_path_for(message_id)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="Audio not found.")
    return FileResponse(path, media_type="audio/wav", filename=path.name)


@app.get("/api/events")
async def events() -> StreamingResponse:
    session = manager()
    queue = await session.subscribe()

    async def event_stream():
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield (
                    f"event: {item['type']}\n"
                    f"data: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
                )
        except asyncio.CancelledError:
            return
        finally:
            session.unsubscribe(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def main(argv: list[str]) -> int:
    args = parse_app_args(argv)
    for env_file in args.env_file:
        load_env_file(env_file)
    app_state["initial_config"] = ResetRequest(
        script=args.script,
        persona_mode=args.persona_mode,
        audio_device_index=args.audio_device_index,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
