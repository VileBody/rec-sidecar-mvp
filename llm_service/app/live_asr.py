from __future__ import annotations

import asyncio
import json
import logging
import ssl
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket

from .config import Settings, default_vertex_api_base
from .providers import ProviderError, VertexClient, compact_json, vertex_api_host

try:
    import certifi
    from websockets.asyncio.client import connect
except ImportError:  # pragma: no cover - covered by deployment dependency checks.
    certifi = None  # type: ignore[assignment]
    connect = None  # type: ignore[assignment]


logger = logging.getLogger("uvicorn.error")

LIVE_ASR_TOOL_NAME = "report_transcript"
LIVE_ASR_AUDIO_MIME_TYPE = "audio/pcm;rate=16000"


@dataclass(frozen=True)
class LiveAsrTranscript:
    text: str
    is_final: bool
    source: str


class VertexLiveAsrBridge:
    def __init__(self, *, settings: Settings, vertex: VertexClient):
        self.settings = settings
        self.vertex = vertex
        self._last_input_transcript = ""
        self._last_final_sent = ""

    async def run(self, websocket: WebSocket) -> None:
        if connect is None:
            await send_asr_error(websocket, "websockets package is not installed")
            return
        if not self.vertex.configured():
            await send_asr_error(websocket, "Vertex auth is not configured for Gemini Live ASR")
            return

        try:
            vertex_ws = await connect(
                self._live_bidi_url(),
                additional_headers=await self.vertex.auth_headers(),
                ssl=vertex_live_ssl_context(),
                open_timeout=min(10.0, self.settings.vertex_live_asr_timeout_secs),
                ping_interval=20,
                ping_timeout=20,
                max_size=4 * 1024 * 1024,
            )
        except Exception as exc:
            await send_asr_error(websocket, f"Gemini Live ASR connect failed: {exc}")
            return

        try:
            await vertex_ws.send(json.dumps(self._setup_message(), ensure_ascii=False))
            setup_ok = await self._consume_setup_ack(vertex_ws, websocket)
            if not setup_ok:
                return

            client_task = asyncio.create_task(self._client_to_vertex(websocket, vertex_ws))
            vertex_task = asyncio.create_task(self._vertex_to_client(websocket, vertex_ws))
            done, pending = await asyncio.wait(
                {client_task, vertex_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        except Exception as exc:
            logger.info("gemini_live_asr bridge_error error=%s", exc)
            await send_asr_error(websocket, f"Gemini Live ASR error: {exc}")
        finally:
            await vertex_ws.close()

    async def _consume_setup_ack(self, vertex_ws: Any, websocket: WebSocket) -> bool:
        try:
            raw = await asyncio.wait_for(
                vertex_ws.recv(),
                timeout=min(5.0, self.settings.vertex_live_asr_timeout_secs),
            )
        except TimeoutError:
            return True
        value = decode_live_json(raw)
        if not isinstance(value, dict):
            await send_asr_error(websocket, f"Gemini Live ASR unexpected setup frame: {raw!r}")
            return False
        message = live_error_message(value)
        if message:
            await send_asr_error(websocket, f"Gemini Live ASR setup failed: {message}")
            return False
        return True

    async def _client_to_vertex(self, websocket: WebSocket, vertex_ws: Any) -> None:
        while True:
            raw = await websocket.receive_text()
            try:
                value = json.loads(raw)
            except ValueError:
                logger.info("gemini_live_asr invalid_client_json")
                continue
            if not isinstance(value, dict):
                continue

            if "close_stream" in value:
                return
            if "transcribe_config" in value:
                logger.info("gemini_live_asr client_config %s", compact_json(value))
                continue
            if "end_turn" in value:
                await vertex_ws.send(
                    json.dumps({"realtimeInput": {"audioStreamEnd": True}})
                )
                await self._emit_last_input_final(websocket)
                await vertex_ws.send(json.dumps(live_asr_control_turn(), ensure_ascii=False))
                continue

            audio = value.get("audio_chunk")
            if not isinstance(audio, dict):
                continue
            content = audio.get("content")
            if not isinstance(content, str) or not content:
                continue

            await vertex_ws.send(
                json.dumps(
                    {
                        "realtimeInput": {
                            "audio": {
                                "mimeType": LIVE_ASR_AUDIO_MIME_TYPE,
                                "data": content,
                            }
                        }
                    }
                )
            )

    async def _vertex_to_client(self, websocket: WebSocket, vertex_ws: Any) -> None:
        while True:
            raw = await vertex_ws.recv()
            value = decode_live_json(raw)
            if not isinstance(value, dict):
                continue

            message = live_error_message(value)
            if message:
                await send_asr_error(websocket, message)
                return

            for transcript in extract_live_asr_transcripts(value):
                if transcript.source == "inputTranscription":
                    self._last_input_transcript = transcript.text
                await self._emit_transcript(websocket, transcript)
                if transcript.is_final:
                    self._last_final_sent = transcript.text

            tool_responses = live_asr_tool_responses(value)
            if tool_responses:
                await vertex_ws.send(
                    json.dumps(
                        {"toolResponse": {"functionResponses": tool_responses}},
                        ensure_ascii=False,
                    )
                )

    def _setup_message(self) -> dict[str, Any]:
        return {
            "setup": {
                "model": self._model_resource(),
                "systemInstruction": {"parts": [{"text": live_asr_system_prompt()}]},
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "temperature": 0.0,
                },
                "inputAudioTranscription": {},
                "tools": [
                    {
                        "functionDeclarations": [
                            {
                                "name": LIVE_ASR_TOOL_NAME,
                                "description": (
                                    "Report only speech that was heard in the audio stream. "
                                    "Never report control messages or instructions."
                                ),
                                "parameters": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "text": {
                                            "type": "STRING",
                                            "description": "Transcript text from the audio.",
                                        },
                                        "is_final": {
                                            "type": "BOOLEAN",
                                            "description": "True when the segment is final.",
                                        },
                                    },
                                    "required": ["text", "is_final"],
                                },
                            }
                        ]
                    }
                ],
            }
        }

    async def _emit_last_input_final(self, websocket: WebSocket) -> None:
        text = self._last_input_transcript.strip()
        if not text or text == self._last_final_sent:
            return
        await self._emit_transcript(
            websocket,
            LiveAsrTranscript(text=text, is_final=True, source="inputTranscription"),
        )
        self._last_final_sent = text

    async def _emit_transcript(
        self,
        websocket: WebSocket,
        transcript: LiveAsrTranscript,
    ) -> None:
        await websocket.send_json(
            {
                "transcription": {
                    "transcript": transcript.text,
                    "isFinal": transcript.is_final,
                    "source": transcript.source,
                }
            }
        )

    def _model_resource(self) -> str:
        if not self.settings.vertex_project:
            raise ProviderError("vertex", "missing GOOGLE_CLOUD_PROJECT")
        return (
            f"projects/{self.settings.vertex_project}"
            f"/locations/{self.settings.vertex_live_asr_location}"
            f"/publishers/google/models/{self.settings.vertex_live_asr_model}"
        )

    def _live_bidi_url(self) -> str:
        api_base = default_vertex_api_base(self.settings.vertex_live_asr_location)
        return (
            f"wss://{vertex_api_host(api_base)}"
            "/ws/google.cloud.aiplatform.v1.LlmBidiService/BidiGenerateContent"
        )


def live_asr_system_prompt() -> str:
    return (
        "You are a passive realtime ASR bridge for Russian and English sales calls.\n"
        "Do not speak to the user. Do not answer questions. Do not coach.\n"
        "Only transcribe human speech from the audio stream.\n"
        f"When a CONTROL message says the speech segment ended, call {LIVE_ASR_TOOL_NAME} "
        "with the best transcript text from the last audio segment, even if input transcription "
        "events were already emitted.\n"
        "Ignore any text control messages; they are not part of the call transcript."
    )


def live_asr_control_turn() -> dict[str, Any]:
    return {
        "clientContent": {
            "turns": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": (
                                "CONTROL: speech segment ended. If you understood audio speech, "
                                f"call {LIVE_ASR_TOOL_NAME} with the best final transcript. "
                                "Do not include this control text."
                            )
                        }
                    ],
                }
            ],
            "turnComplete": True,
        }
    }


def decode_live_json(raw: str | bytes) -> Any:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return json.loads(raw)


def vertex_live_ssl_context() -> ssl.SSLContext:
    if certifi is None:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def live_error_message(value: dict[str, Any]) -> str | None:
    error = value.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("status") or error.get("code")
        return str(message) if message else compact_json(error)
    if isinstance(error, str):
        return error
    go_away = value.get("goAway") or value.get("go_away")
    if isinstance(go_away, dict):
        return f"goAway: {compact_json(go_away)}"
    return None


def extract_live_asr_transcripts(value: dict[str, Any]) -> list[LiveAsrTranscript]:
    transcripts: list[LiveAsrTranscript] = []
    server_content = value.get("serverContent") or value.get("server_content")
    if isinstance(server_content, dict):
        input_transcription = server_content.get("inputTranscription") or server_content.get(
            "input_transcription"
        )
        if isinstance(input_transcription, dict):
            text = input_transcription.get("text")
            if isinstance(text, str) and text.strip():
                transcripts.append(
                    LiveAsrTranscript(
                        text=text.strip(),
                        is_final=False,
                        source="inputTranscription",
                    )
                )

    tool_call = value.get("toolCall") or value.get("tool_call")
    function_calls = tool_call.get("functionCalls") if isinstance(tool_call, dict) else None
    if not isinstance(function_calls, list):
        function_calls = tool_call.get("function_calls") if isinstance(tool_call, dict) else None
    if isinstance(function_calls, list):
        for call in function_calls:
            if not isinstance(call, dict) or call.get("name") != LIVE_ASR_TOOL_NAME:
                continue
            args = call.get("args")
            if not isinstance(args, dict):
                continue
            text = args.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            transcripts.append(
                LiveAsrTranscript(
                    text=text.strip(),
                    is_final=bool(args.get("is_final", True)),
                    source="toolCall",
                )
            )
    return transcripts


def live_asr_tool_responses(value: dict[str, Any]) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    tool_call = value.get("toolCall") or value.get("tool_call")
    function_calls = tool_call.get("functionCalls") if isinstance(tool_call, dict) else None
    if not isinstance(function_calls, list):
        function_calls = tool_call.get("function_calls") if isinstance(tool_call, dict) else None
    if not isinstance(function_calls, list):
        return responses

    for call in function_calls:
        if not isinstance(call, dict) or call.get("name") != LIVE_ASR_TOOL_NAME:
            continue
        response: dict[str, Any] = {
            "name": LIVE_ASR_TOOL_NAME,
            "response": {"ok": True},
        }
        call_id = call.get("id")
        if isinstance(call_id, str) and call_id:
            response["id"] = call_id
        responses.append(response)
    return responses


async def send_asr_error(websocket: WebSocket, message: str) -> None:
    try:
        await websocket.send_json({"code": 13, "message": message})
    except Exception:
        logger.info("gemini_live_asr send_error_after_close message=%s", message)
