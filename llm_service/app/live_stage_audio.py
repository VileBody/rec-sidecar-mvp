from __future__ import annotations

import asyncio
import json
import logging
import ssl
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket

from .config import Settings, default_vertex_api_base
from .live_asr import decode_live_json, live_error_message, vertex_live_ssl_context
from .providers import ProviderError, VertexClient, compact_json, vertex_api_host
from .schemas import StageAgendaResponse
from .scorecard import normalize_scorecard, safe_parse_scorecard
from .stage_assets import (
    CURRENT_STAGE_AGENDA_PROMPT,
    KNOWN_STAGES,
    STAGE_AGENDA_BY_TAG,
    clamp_stage_forward,
    normalize_stage,
    parse_stage_detection,
    stage_is_backward,
)

try:
    from websockets.asyncio.client import connect
except ImportError:  # pragma: no cover - covered by deployment dependency checks.
    connect = None  # type: ignore[assignment]


logger = logging.getLogger("uvicorn.error")

LIVE_STAGE_AUDIO_TOOL_NAME = "submit_stage_scorecard"
LIVE_STAGE_AUDIO_MIME_TYPE = "audio/pcm;rate=16000"


@dataclass(frozen=True)
class LiveStageToolCall:
    name: str
    args: dict[str, Any]
    call_id: str | None = None


class VertexLiveStageAudioBridge:
    def __init__(self, *, settings: Settings, vertex: VertexClient):
        self.settings = settings
        self.vertex = vertex
        self._last_stage: str | None = None
        self._last_emit_key: tuple[Any, ...] | None = None

    async def run(self, websocket: WebSocket) -> None:
        if connect is None:
            await send_stage_audio_error(websocket, "websockets package is not installed")
            return
        if not self.vertex.configured():
            await send_stage_audio_error(
                websocket, "Vertex auth is not configured for Gemini Live stage audio"
            )
            return

        try:
            vertex_ws = await connect(
                self._live_bidi_url(),
                additional_headers=await self.vertex.auth_headers(),
                ssl=vertex_live_ssl_context(),
                open_timeout=min(10.0, self.settings.vertex_live_stage_timeout_secs),
                ping_interval=20,
                ping_timeout=20,
                max_size=8 * 1024 * 1024,
            )
        except Exception as exc:
            await send_stage_audio_error(websocket, f"Gemini Live stage connect failed: {exc}")
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
            logger.info("gemini_live_stage_audio bridge_error error=%s", exc)
            await send_stage_audio_error(websocket, f"Gemini Live stage error: {exc}")
        finally:
            await vertex_ws.close()

    async def _consume_setup_ack(self, vertex_ws: Any, websocket: WebSocket) -> bool:
        try:
            raw = await asyncio.wait_for(
                vertex_ws.recv(),
                timeout=min(5.0, self.settings.vertex_live_stage_timeout_secs),
            )
        except TimeoutError:
            return True
        value = decode_live_json(raw)
        if not isinstance(value, dict):
            await send_stage_audio_error(
                websocket, f"Gemini Live stage unexpected setup frame: {raw!r}"
            )
            return False
        message = live_error_message(value)
        if message:
            await send_stage_audio_error(websocket, f"Gemini Live stage setup failed: {message}")
            return False
        return True

    async def _client_to_vertex(self, websocket: WebSocket, vertex_ws: Any) -> None:
        while True:
            raw = await websocket.receive_text()
            try:
                value = json.loads(raw)
            except ValueError:
                logger.info("gemini_live_stage_audio invalid_client_json")
                continue
            if not isinstance(value, dict):
                continue

            if "close_stream" in value:
                return
            if "transcribe_config" in value:
                logger.info("gemini_live_stage_audio client_config %s", compact_json(value))
                continue
            if "end_turn" in value:
                await vertex_ws.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))
                await vertex_ws.send(
                    json.dumps(
                        live_stage_control_turn(current_stage=self._last_stage),
                        ensure_ascii=False,
                    )
                )
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
                                "mimeType": LIVE_STAGE_AUDIO_MIME_TYPE,
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
                await send_stage_audio_error(websocket, message)
                return

            tool_calls = live_stage_audio_tool_calls(value)
            if not tool_calls:
                continue

            await vertex_ws.send(
                json.dumps(
                    {"toolResponse": {"functionResponses": live_stage_audio_tool_responses(value)}},
                    ensure_ascii=False,
                )
            )
            for call in tool_calls:
                response = live_stage_audio_stage_response(
                    call.args,
                    model=self.settings.vertex_live_stage_model,
                    last_stage=self._last_stage,
                )
                self._last_stage = response.stage
                emit_key = stage_response_key(response)
                if emit_key == self._last_emit_key:
                    logger.info(
                        "gemini_live_stage_audio no_update stage=%s model=%s",
                        response.stage,
                        response.model,
                    )
                    continue
                self._last_emit_key = emit_key
                logger.info(
                    "gemini_live_stage_audio stage=%s readiness=%s model=%s",
                    response.stage,
                    response.scorecard.readiness if response.scorecard else "none",
                    response.model,
                )
                await websocket.send_json({"stage_agenda": response.model_dump(mode="json")})

    def _setup_message(self) -> dict[str, Any]:
        return {
            "setup": {
                "model": self._model_resource(),
                "systemInstruction": {"parts": [{"text": live_stage_audio_system_prompt()}]},
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "temperature": 0.0,
                },
                "tools": [
                    {
                        "functionDeclarations": [
                            {
                                "name": LIVE_STAGE_AUDIO_TOOL_NAME,
                                "description": (
                                    "Submit the current sales stage, scorecard checks, and "
                                    "one tactical next action for the seller."
                                ),
                                "parameters": live_stage_audio_tool_schema(),
                            }
                        ]
                    }
                ],
                "toolConfig": {
                    "functionCallingConfig": {
                        "mode": "ANY",
                        "allowedFunctionNames": [LIVE_STAGE_AUDIO_TOOL_NAME],
                    }
                },
            }
        }

    def _model_resource(self) -> str:
        if not self.settings.vertex_project:
            raise ProviderError("vertex", "missing GOOGLE_CLOUD_PROJECT")
        return (
            f"projects/{self.settings.vertex_project}"
            f"/locations/{self.settings.vertex_live_stage_location}"
            f"/publishers/google/models/{self.settings.vertex_live_stage_model}"
        )

    def _live_bidi_url(self) -> str:
        api_base = default_vertex_api_base(self.settings.vertex_live_stage_location)
        return (
            f"wss://{vertex_api_host(api_base)}"
            "/ws/google.cloud.aiplatform.v1.LlmBidiService/BidiGenerateContent"
        )


def live_stage_audio_system_prompt() -> str:
    return (
        "Ты realtime intelligence engine для high-ticket B2C sales call на русском.\n"
        "Ты слушаешь аудио звонка. Не отвечай голосом клиенту и продавцу. "
        f"На CONTROL-сообщение вызывай только функцию {LIVE_STAGE_AUDIO_TOOL_NAME}.\n\n"
        "Задачи функции:\n"
        "1. Определи текущий stage строго одним из тегов: "
        f"{', '.join(KNOWN_STAGES)}.\n"
        "2. Оцени checks текущей стадии как hit/miss/pending/uncertain/na.\n"
        "3. Дай короткий next_action для продавца.\n\n"
        "Правила:\n"
        "- stage передавай только тегом S2.1...S3.5, без человеческого названия.\n"
        "- Никогда не возвращай stage назад относительно последнего CONTROL current_stage; "
        "если снова слышишь старую тему, оставь текущую/более позднюю стадию и дай уточнение "
        "внутри неё.\n"
        "- hit/miss ставь только по услышанному evidence; если данных мало, ставь pending.\n"
        '- Если readiness красный/желтый: next_action начни с "Уточнить:".\n'
        '- Если можно идти дальше: next_action начни с "Переход:".\n'
        "- После префикса next_action должен быть готовой фразой продавца клиенту от первого лица, "
        "которую можно зачитать вслух прямо сейчас. Не пиши мета-инструкции вроде "
        '"спроси", "уточни", "скажи клиенту", "дай аргумент".\n'
        "- evidence цитируй коротко и только из звонка.\n\n"
        "--- Stage -> agenda mapping ---\n"
        f"{CURRENT_STAGE_AGENDA_PROMPT}\n\n"
        "--- Scorecard checks ---\n"
        f"{format_scorecard_definitions_for_live_prompt()}"
    )


def live_stage_control_turn(*, current_stage: str | None = None) -> dict[str, Any]:
    stage_hint = current_stage or "(пока неизвестен)"
    return {
        "clientContent": {
            "turns": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": (
                                "CONTROL: оцени текущий момент звонка по услышанному аудио и "
                                f"вызови {LIVE_STAGE_AUDIO_TOOL_NAME}. "
                                f"Текущий stage: {stage_hint}. "
                                "Не возвращайся на более ранний stage; если сомневаешься, "
                                "оставь текущий stage и уточни внутри него. "
                                "Не произноси ответ голосом."
                            )
                        }
                    ],
                }
            ],
            "turnComplete": True,
        }
    }


def live_stage_audio_tool_schema() -> dict[str, Any]:
    check_ids = sorted(
        {
            check.id
            for definitions in __import__(
                "llm_service.app.scorecard", fromlist=["STAGE_SCORECARD_DEFINITIONS"]
            ).STAGE_SCORECARD_DEFINITIONS.values()
            for check in definitions
        }
    )
    return {
        "type": "OBJECT",
        "properties": {
            "stage": {
                "type": "STRING",
                "enum": list(KNOWN_STAGES),
                "description": "Current sales stage tag.",
            },
            "confidence": {"type": "NUMBER"},
            "summary": {"type": "STRING"},
            "next_action": {"type": "STRING"},
            "checks": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "id": {
                            "type": "STRING",
                            "enum": check_ids,
                        },
                        "result": {
                            "type": "STRING",
                            "enum": ["hit", "miss", "pending", "uncertain", "na"],
                        },
                        "reason": {"type": "STRING"},
                        "evidence": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "speaker": {"type": "STRING"},
                                    "quote": {"type": "STRING"},
                                },
                                "required": ["quote"],
                            },
                        },
                    },
                    "required": ["id", "result", "reason"],
                },
            },
        },
        "required": ["stage", "summary", "next_action", "checks"],
    }


def format_scorecard_definitions_for_live_prompt() -> str:
    from .scorecard import STAGE_SCORECARD_DEFINITIONS

    blocks: list[str] = []
    for stage in KNOWN_STAGES:
        definitions = STAGE_SCORECARD_DEFINITIONS.get(stage, ())
        checks = "\n".join(
            f"- {check.id} [{check.level}/{check.signal}] {check.label}; "
            f"HIT: {check.hit}; MISS: {check.miss}"
            for check in definitions
        )
        blocks.append(f"{stage}\n{checks}")
    return "\n\n".join(blocks)


def live_stage_audio_tool_calls(value: dict[str, Any]) -> list[LiveStageToolCall]:
    tool_call = value.get("toolCall") or value.get("tool_call")
    function_calls = tool_call.get("functionCalls") if isinstance(tool_call, dict) else None
    if not isinstance(function_calls, list):
        function_calls = tool_call.get("function_calls") if isinstance(tool_call, dict) else None
    if not isinstance(function_calls, list):
        return []

    calls: list[LiveStageToolCall] = []
    for call in function_calls:
        if not isinstance(call, dict) or call.get("name") != LIVE_STAGE_AUDIO_TOOL_NAME:
            continue
        args = call.get("args")
        if not isinstance(args, dict):
            args = {}
        call_id = call.get("id") if isinstance(call.get("id"), str) else None
        calls.append(LiveStageToolCall(name=LIVE_STAGE_AUDIO_TOOL_NAME, args=args, call_id=call_id))
    return calls


def live_stage_audio_tool_responses(value: dict[str, Any]) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    for call in live_stage_audio_tool_calls(value):
        response: dict[str, Any] = {
            "name": LIVE_STAGE_AUDIO_TOOL_NAME,
            "response": {"ok": True},
        }
        if call.call_id:
            response["id"] = call.call_id
        responses.append(response)
    return responses


def live_stage_audio_stage_response(
    args: dict[str, Any],
    *,
    model: str,
    last_stage: str | None = None,
) -> StageAgendaResponse:
    stage = normalize_stage(str(args.get("stage", "")))
    if stage not in STAGE_AGENDA_BY_TAG:
        try:
            stage, _ = parse_stage_detection(str(args.get("stage", "")))
        except ValueError:
            stage = normalize_stage(last_stage or "") or KNOWN_STAGES[0]
    if stage_is_backward(last_stage, stage):
        logger.info(
            "gemini_live_stage_audio backward_stage_ignored current_stage=%s incoming_stage=%s",
            last_stage,
            stage,
        )
    stage = clamp_stage_forward(last_stage, stage)
    agenda = STAGE_AGENDA_BY_TAG[stage]

    try:
        raw = safe_parse_scorecard(json.dumps(args, ensure_ascii=False))
    except ValueError as exc:
        logger.info("gemini_live_stage_audio invalid_scorecard args=%s error=%s", args, exc)
        raw = safe_parse_scorecard(
            json.dumps(
                {
                    "summary": str(args.get("summary") or "Оценка по live audio."),
                    "next_action": str(args.get("next_action") or agenda.step),
                    "checks": [],
                },
                ensure_ascii=False,
            )
        )
    scorecard = normalize_scorecard(stage=agenda.stage, agenda=agenda, raw=raw)
    confidence = args.get("confidence")
    return StageAgendaResponse(
        stage=agenda.stage,
        title=agenda.title,
        agenda=agenda.agenda,
        emotion=agenda.emotion,
        step=agenda.step,
        provider="vertex-live-audio",
        model=model,
        confidence=confidence if isinstance(confidence, (int, float)) else None,
        scorecard=scorecard,
    )


def stage_response_key(response: StageAgendaResponse) -> tuple[Any, ...]:
    scorecard = response.scorecard
    checks = tuple((check.id, check.result) for check in (scorecard.checks if scorecard else []))
    return (
        response.stage,
        scorecard.readiness if scorecard else None,
        scorecard.next_action if scorecard else None,
        checks,
    )


async def send_stage_audio_error(websocket: WebSocket, message: str) -> None:
    try:
        await websocket.send_json({"code": 13, "message": message})
    except Exception:
        logger.info("gemini_live_stage_audio send_error_after_close message=%s", message)
