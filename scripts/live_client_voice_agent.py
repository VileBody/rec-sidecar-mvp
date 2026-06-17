#!/usr/bin/env python3
"""Standalone voice client agent for manual sales-call testing.

Flow per turn:
1. User starts recording and speaks the seller line.
2. Audio is streamed live to Inworld STT, partial/final transcript is logged.
3. A client persona model generates the buyer reply from the transcript + history.
4. The reply is voiced with Inworld TTS, saved to disk, and optionally played.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Awaitable

import certifi
import httpx

try:
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed, ConnectionClosedOK
except ImportError as exc:  # pragma: no cover
    raise SystemExit("websockets package is required") from exc


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = REPO_ROOT / "sales_scripts" / "glubina_kazan_10_call_scripts_v1.md"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "logs" / "live_client_voice_agent"
DEFAULT_INWORLD_STT_WS_URL = "wss://api.inworld.ai/stt/v1/transcribe:streamBidirectional"
DEFAULT_INWORLD_TTS_API_BASE = "https://api.inworld.ai"
DEFAULT_INWORLD_TTS_MODEL = "inworld-tts-1"
DEFAULT_INWORLD_SELLER_VOICE = "Elena"
DEFAULT_INWORLD_CLIENT_VOICE = "Arkady"
DEFAULT_CEREBRAS_API_BASE = "https://api.cerebras.ai/v1"
DEFAULT_CLIENT_ACTOR_MODEL = "zai-glm-4.7"
DEFAULT_SAMPLE_RATE = 16_000
DEFAULT_CHANNELS = 1
DEFAULT_SAMPLE_WIDTH_BYTES = 2
DEFAULT_STT_MIN_CHUNK_MS = 20
DEFAULT_STT_MAX_CHUNK_MS = 1000
DEFAULT_CHUNK_MS = 100
DEFAULT_CHUNK_BYTES = int(DEFAULT_SAMPLE_RATE * DEFAULT_SAMPLE_WIDTH_BYTES * DEFAULT_CHUNK_MS / 1000)

TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where())


@dataclass
class ScriptPersona:
    number: int
    title: str
    persona: str
    client_lines: list[str]


@dataclass
class TranscriptResult:
    text: str
    partials: list[str]
    finals: list[str]
    elapsed_ms: int
    audio_path: Path
    ffmpeg_log_path: Path


@dataclass
class ClientReplyResult:
    text: str
    elapsed_ms: int
    audio_path: Path | None


class SessionLogger:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.output_dir / "session.jsonl"
        self.transcript_path = self.output_dir / "dialogue.txt"

    def log(self, kind: str, **payload: Any) -> None:
        value = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": kind,
            **payload,
        }
        line = json.dumps(value, ensure_ascii=False)
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def append_dialogue(self, speaker: str, text: str) -> None:
        with self.transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{speaker}: {text}\n")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def print_log(message: str) -> None:
    print(message, flush=True)


def authorization_header(api_key: str) -> str:
    if api_key.lower().startswith("basic "):
        return api_key
    return f"Basic {api_key}"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--script", type=int, default=2, help="Reference persona script number.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR / time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--audio-device-index", type=int, default=None)
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--max-turns", type=int, default=20)
    parser.add_argument("--stt-timeout-secs", type=float, default=12.0)
    parser.add_argument("--stt-idle-finish-secs", type=float, default=1.0)
    parser.add_argument("--chunk-ms", type=int, default=DEFAULT_CHUNK_MS)
    parser.add_argument("--inworld-api-key", default=None)
    parser.add_argument("--inworld-stt-ws-url", default=os.getenv("INWORLD_STT_WS_URL", DEFAULT_INWORLD_STT_WS_URL))
    parser.add_argument("--inworld-tts-api-base", default=os.getenv("INWORLD_TTS_API_BASE", DEFAULT_INWORLD_TTS_API_BASE))
    parser.add_argument("--inworld-tts-model", default=os.getenv("INWORLD_TTS_MODEL", DEFAULT_INWORLD_TTS_MODEL))
    parser.add_argument("--inworld-seller-voice", default=os.getenv("INWORLD_TTS_SELLER_VOICE", DEFAULT_INWORLD_SELLER_VOICE))
    parser.add_argument("--inworld-client-voice", default=os.getenv("INWORLD_TTS_CLIENT_VOICE", DEFAULT_INWORLD_CLIENT_VOICE))
    parser.add_argument("--inworld-language", default=os.getenv("INWORLD_TTS_LANGUAGE", "ru-RU"))
    parser.add_argument("--client-actor-model", default=os.getenv("CEREBRAS_MODEL", DEFAULT_CLIENT_ACTOR_MODEL))
    parser.add_argument("--client-actor-temperature", type=float, default=0.85)
    parser.add_argument("--client-actor-max-tokens", type=int, default=220)
    parser.add_argument("--cerebras-api-key", default=None)
    parser.add_argument("--cerebras-api-base", default=os.getenv("CEREBRAS_API_BASE", DEFAULT_CEREBRAS_API_BASE))
    parser.add_argument("--cerebras-reasoning-effort", default=os.getenv("CEREBRAS_REASONING_EFFORT", "none"))
    parser.add_argument("--persona-mode", choices=["neutral", "cold", "hostile"], default="hostile")
    parser.add_argument("--play", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--save-audio", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--env-file", type=Path, action="append", default=[Path(".env"), Path(".env.iac")])
    args = parser.parse_args(argv)
    if not (DEFAULT_STT_MIN_CHUNK_MS <= args.chunk_ms <= DEFAULT_STT_MAX_CHUNK_MS):
        parser.error(
            f"--chunk-ms must be between {DEFAULT_STT_MIN_CHUNK_MS} and {DEFAULT_STT_MAX_CHUNK_MS}"
        )
    return args


def list_avfoundation_audio_devices() -> list[tuple[int, str]]:
    result = subprocess.run(
        ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        check=False,
        capture_output=True,
        text=True,
    )
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    devices: list[tuple[int, str]] = []
    in_audio = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if "AVFoundation audio devices:" in line:
            in_audio = True
            continue
        if "AVFoundation video devices:" in line:
            in_audio = False
            continue
        if not in_audio:
            continue
        match = re.search(r"\[(\d+)\]\s+(.+)$", line)
        if match:
            devices.append((int(match.group(1)), match.group(2).strip()))
    return devices


def choose_audio_device(args: argparse.Namespace) -> tuple[int, str]:
    devices = list_avfoundation_audio_devices()
    if not devices:
        raise RuntimeError("No AVFoundation audio devices found via ffmpeg.")
    if args.audio_device_index is not None:
        for index, name in devices:
            if index == args.audio_device_index:
                return index, name
        raise RuntimeError(f"Audio device index {args.audio_device_index} not found.")

    preferred_markers = ("macbook", "built-in", "встроенн", "микрофон macbook")
    for index, name in devices:
        lowered = name.lower()
        if any(marker in lowered for marker in preferred_markers):
            return index, name
    return devices[0]


def print_audio_devices() -> None:
    devices = list_avfoundation_audio_devices()
    if not devices:
        print("No AVFoundation audio devices found.")
        return
    print("AVFoundation audio devices:")
    for index, name in devices:
        print(f"  [{index}] {name}")


def write_pcm_wav(path: Path, pcm: bytes, sample_rate: int = DEFAULT_SAMPLE_RATE) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(DEFAULT_CHANNELS)
        handle.setsampwidth(DEFAULT_SAMPLE_WIDTH_BYTES)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)


def play_audio(path: Path) -> None:
    subprocess.run(["afplay", str(path)], check=False)


def parse_reference_script(path: Path, script_number: int) -> ScriptPersona:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(r"(?m)^## Скрипт\s+(\d+)\.\s*(.+)$")
    matches = list(pattern.finditer(text))
    for idx, match in enumerate(matches):
        number = int(match.group(1))
        if number != script_number:
            continue
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[start:end]
        persona_match = re.search(r"(?m)^\*\*Персона:\*\*\s*(.+)$", body)
        persona = persona_match.group(1).strip() if persona_match else match.group(2).strip()
        client_lines = [
            m.group(1).strip()
            for m in re.finditer(r"(?m)^\*\*Клиент:\*\*\s*(.+)$", body)
        ]
        return ScriptPersona(
            number=number,
            title=match.group(2).strip(),
            persona=persona,
            client_lines=client_lines,
        )
    raise RuntimeError(f"Script {script_number} not found in {path}.")


def client_reference_arc(reference: ScriptPersona, limit: int = 8) -> str:
    if not reference.client_lines:
        return "(нет)"
    return "\n".join(f"- {line}" for line in reference.client_lines[:limit])


def sanitize_client_reply(text: str) -> str:
    value = " ".join(text.strip().strip("\"'`«»“”").split())
    value = re.sub(r"^(?:клиент|покупатель|client|buyer)\s*[:：-]\s*", "", value, flags=re.I)
    value = re.sub(r"^(?:ответ|реплика)\s*[:：-]\s*", "", value, flags=re.I)
    return value.strip().strip("\"'`«»“”").strip()


def dialogue_tail(history: list[tuple[str, str]], limit: int = 12) -> str:
    if not history:
        return "(диалог только начинается)"
    return "\n".join(f"{speaker}: {text}" for speaker, text in history[-limit:])


def event_facts() -> str:
    return (
        "glubina.core, Казань, 7-10 июля 2026, 4 офлайн дня, 160 участников, "
        "19 менторов, группы по 8 предпринимателей одного уровня, живые разборы, "
        "цели, психологи, личная декларация, цели на 90 дней, группа на связи, "
        "community, стоимость 99 000 руб."
    )


def build_client_system_prompt(persona_mode: str) -> str:
    style = {
        "neutral": (
            "Клиент реалистичный, живой и осторожный. "
            "Может сомневаться, но не конфликтует без причины."
        ),
        "cold": (
            "Клиент холодный и дистанцированный. "
            "Требует перейти к сути, не раскрывается быстро, не помогает продавцу."
        ),
        "hostile": (
            "Клиент неприятный, скептичный и уставший от продажников. "
            "Он режет шаблоны, цепляется к неконкретности, может отвечать колко и жестко, "
            "но остается правдоподобным и по делу."
        ),
    }[persona_mode]
    return (
        "Ты играешь клиента в голосовом high-check B2C sales звонке на русском.\n"
        "Отвечай только репликой клиента без markdown, без имени и без пояснений.\n"
        f"{style}\n"
        "Если продавец задал конкретный вопрос, ответь на него.\n"
        "Если продавец звучит расплывчато, попроси конкретику.\n"
        "Если продавец не ответил на прямой вопрос, укажи на это.\n"
        "1-2 коротких предложения, без длинных монологов."
    )


def cerebras_text(
    *,
    api_key: str,
    api_base: str,
    model: str,
    system_prompt: str,
    user_content: str,
    temperature: float,
    max_tokens: int,
    reasoning_effort: str,
) -> str:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    effort = reasoning_effort.strip().lower()
    if effort == "none" and model.startswith("zai-"):
        payload["reasoning_effort"] = "none"
    elif effort and effort != "none":
        payload["reasoning_effort"] = effort

    with httpx.Client(timeout=45.0) as client:
        response = client.post(
            f"{api_base.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
    if not response.is_success:
        raise RuntimeError(f"Cerebras client actor HTTP {response.status_code}: {response.text}")
    value = response.json()
    choices = value.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"Empty Cerebras response: {value}")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"Empty Cerebras content: {value}")
    return content.strip()


def generate_client_reply(
    *,
    args: argparse.Namespace,
    reference: ScriptPersona,
    history: list[tuple[str, str]],
    seller_transcript: str,
) -> str:
    api_key = args.cerebras_api_key or os.getenv("CEREBRAS_API_KEY")
    if not api_key:
        raise RuntimeError("Missing CEREBRAS_API_KEY for client actor.")

    user_content = (
        f"Продукт / контекст:\n{event_facts()}\n\n"
        f"Референс-персона:\nСценарий {reference.number}: {reference.title}\n"
        f"Персона: {reference.persona}\n\n"
        "Референс-дуга клиента, НЕ копируй дословно:\n"
        f"{client_reference_arc(reference)}\n\n"
        f"История разговора:\n{dialogue_tail(history)}\n\n"
        f"Последняя реплика продавца:\nSeller: {seller_transcript}\n\n"
        "Дай следующую живую реплику клиента."
    )
    text = cerebras_text(
        api_key=api_key,
        api_base=args.cerebras_api_base,
        model=args.client_actor_model,
        system_prompt=build_client_system_prompt(args.persona_mode),
        user_content=user_content,
        temperature=args.client_actor_temperature,
        max_tokens=args.client_actor_max_tokens,
        reasoning_effort=args.cerebras_reasoning_effort,
    )
    reply = sanitize_client_reply(text)
    if not reply:
        raise RuntimeError("Client actor returned an empty reply.")
    return reply


def inworld_voice_id(args: argparse.Namespace, speaker: str) -> str:
    if speaker.lower() == "seller":
        return args.inworld_seller_voice
    return args.inworld_client_voice


def synthesize_inworld_text(
    args: argparse.Namespace,
    text: str,
    *,
    speaker: str = "Client",
) -> bytes:
    api_key = args.inworld_api_key or os.getenv("INWORLD_API_KEY")
    if not api_key:
        raise RuntimeError("Missing INWORLD_API_KEY for client TTS.")
    payload = {
        "text": text,
        "voiceId": inworld_voice_id(args, speaker),
        "modelId": args.inworld_tts_model,
        "audioConfig": {
            "audioEncoding": "LINEAR16",
            "sampleRateHertz": DEFAULT_SAMPLE_RATE,
            "language": args.inworld_language,
        },
    }
    request = urllib.request.Request(
        f"{args.inworld_tts_api_base.rstrip('/')}/tts/v1/voice",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": authorization_header(api_key),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45, context=TLS_CONTEXT) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Inworld TTS HTTP {exc.code}: {body}") from exc
    audio_content = value.get("audioContent") or value.get("audio_content")
    if not isinstance(audio_content, str) or not audio_content:
        raise RuntimeError(f"Inworld TTS returned no audioContent: {value}")
    audio = base64.b64decode(audio_content)
    return wav_bytes_to_pcm(audio)


def wav_bytes_to_pcm(audio: bytes) -> bytes:
    if not audio.startswith(b"RIFF"):
        return audio
    import io

    with wave.open(io.BytesIO(audio), "rb") as handle:
        if handle.getframerate() != DEFAULT_SAMPLE_RATE or handle.getnchannels() != DEFAULT_CHANNELS:
            raise RuntimeError(
                "Inworld TTS WAV format mismatch: "
                f"{handle.getframerate()} Hz, {handle.getnchannels()} ch"
            )
        return handle.readframes(handle.getnframes())


def extract_transcription_message(raw: str) -> tuple[str, bool] | None:
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    error = value.get("error") if isinstance(value.get("error"), dict) else None
    if value.get("code") is not None:
        message = value.get("message") or "unknown STT server error"
        raise RuntimeError(str(message))
    if error and error.get("code") is not None:
        message = error.get("message") or "unknown STT server error"
        raise RuntimeError(str(message))
    result = value.get("result") if isinstance(value.get("result"), dict) else None
    transcription = None
    if result and isinstance(result.get("transcription"), dict):
        transcription = result.get("transcription")
    elif isinstance(value.get("transcription"), dict):
        transcription = value.get("transcription")
    if not isinstance(transcription, dict):
        return None
    text = transcription.get("transcript")
    if not isinstance(text, str) or not text.strip():
        return None
    is_final = bool(transcription.get("isFinal") or transcription.get("is_final"))
    return " ".join(text.split()), is_final


class LiveSellerRecorder:
    def __init__(
        self,
        *,
        args: argparse.Namespace,
        logger: SessionLogger,
        turn_index: int,
        device_index: int,
        device_name: str,
        event_callback: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ):
        self.args = args
        self.logger = logger
        self.turn_index = turn_index
        self.device_index = device_index
        self.device_name = device_name
        self.event_callback = event_callback

        self.api_key = args.inworld_api_key or os.getenv("INWORLD_API_KEY")
        if not self.api_key:
            raise RuntimeError("Missing INWORLD_API_KEY for live transcription.")

        self.raw_pcm = bytearray()
        self.partials: list[str] = []
        self.finals: list[str] = []
        self.ffmpeg_log_path = args.output_dir / f"seller_turn_{turn_index:03d}.ffmpeg.log"
        self.wav_path = args.output_dir / f"seller_turn_{turn_index:03d}.wav"
        self.chunk_bytes = int(DEFAULT_SAMPLE_RATE * DEFAULT_SAMPLE_WIDTH_BYTES * args.chunk_ms / 1000)
        self.min_chunk_bytes = int(
            DEFAULT_SAMPLE_RATE * DEFAULT_SAMPLE_WIDTH_BYTES * DEFAULT_STT_MIN_CHUNK_MS / 1000
        )
        self.ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "avfoundation",
            "-i",
            f":{device_index}",
            "-ac",
            "1",
            "-ar",
            str(DEFAULT_SAMPLE_RATE),
            "-f",
            "s16le",
            "pipe:1",
        ]

        self.started_at = 0.0
        self.last_event_at = 0.0
        self.stop_requested = asyncio.Event()
        self.end_turn_sent = asyncio.Event()
        self.ffmpeg: asyncio.subprocess.Process | None = None
        self.websocket = None
        self.send_task: asyncio.Task[None] | None = None
        self.recv_task: asyncio.Task[None] | None = None
        self.stderr_bytes = b""

    async def _emit(self, kind: str, **payload: Any) -> None:
        if self.event_callback is None:
            return
        await self.event_callback(kind, payload)

    async def start(self) -> None:
        print_log(
            f"[turn {self.turn_index}] recording from mic [{self.device_index}] {self.device_name}. "
            "Нажми Enter, когда закончишь реплику."
        )
        self.logger.log(
            "record_start",
            turn=self.turn_index,
            device_index=self.device_index,
            device_name=self.device_name,
        )
        print_log(f"[turn {self.turn_index}] launching ffmpeg mic capture...")

        self.ffmpeg = await asyncio.create_subprocess_exec(
            *self.ffmpeg_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        print_log(f"[turn {self.turn_index}] ffmpeg started pid={self.ffmpeg.pid}")
        self.started_at = time.monotonic()
        self.last_event_at = time.monotonic()

        try:
            print_log(f"[turn {self.turn_index}] connecting Inworld STT...")
            self.websocket = await connect(
                self.args.inworld_stt_ws_url,
                additional_headers={"Authorization": authorization_header(self.api_key)},
                ssl=TLS_CONTEXT,
                open_timeout=10,
                max_size=8 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=20,
            )
            await self.websocket.send(
                json.dumps(
                    {
                        "transcribe_config": {
                            "modelId": os.getenv("INWORLD_STT_MODEL", "soniox/stt-rt-v4"),
                            "audioEncoding": "LINEAR16",
                            "sampleRateHertz": DEFAULT_SAMPLE_RATE,
                            "numberOfChannels": 1,
                            "enableSpeakerDiarization": True,
                            "enableLanguageDetection": True,
                        }
                    }
                )
            )
            self.logger.log("stt_connected", turn=self.turn_index, ws_url=self.args.inworld_stt_ws_url)
            print_log(f"[turn {self.turn_index}] STT connected. Говори. Потом жми Enter для остановки.")
            await self._emit("recording_started", turn=self.turn_index)

            await asyncio.sleep(0.35)
            if self.ffmpeg.returncode is not None:
                self.stderr_bytes = await self.ffmpeg.stderr.read() if self.ffmpeg.stderr is not None else b""
                raise RuntimeError(
                    "ffmpeg exited before recording started: "
                    + (
                        self.stderr_bytes.decode("utf-8", errors="replace").strip()
                        or f"code={self.ffmpeg.returncode}"
                    )
                )

            self.send_task = asyncio.create_task(self._send_audio())
            self.recv_task = asyncio.create_task(self._receive_transcript())
        except Exception:
            await self._finalize_process()
            raise

    async def _send_pcm_chunk(self, chunk: bytes, *, sent_chunks: int, sent_bytes: int) -> tuple[bool, int, int]:
        if not chunk:
            return True, sent_chunks, sent_bytes
        try:
            assert self.websocket is not None
            await self.websocket.send(
                json.dumps(
                    {
                        "audio_chunk": {
                            "content": base64.b64encode(chunk).decode("ascii")
                        }
                    }
                )
            )
            sent_chunks += 1
            sent_bytes += len(chunk)
            return True, sent_chunks, sent_bytes
        except ConnectionClosedOK as exc:
            self.logger.log(
                "stt_sender_closed_ok",
                turn=self.turn_index,
                code=exc.rcvd.code if exc.rcvd else 1000,
                chunks=sent_chunks,
                bytes=sent_bytes,
            )
            return False, sent_chunks, sent_bytes
        except ConnectionClosed as exc:
            self.logger.log(
                "stt_sender_closed",
                turn=self.turn_index,
                code=exc.rcvd.code if exc.rcvd else None,
                reason=exc.rcvd.reason if exc.rcvd else "",
                chunks=sent_chunks,
                bytes=sent_bytes,
            )
            return False, sent_chunks, sent_bytes

    async def _send_audio(self) -> None:
        assert self.ffmpeg is not None
        assert self.ffmpeg.stdout is not None
        sent_chunks = 0
        sent_bytes = 0
        pending = bytearray()

        while True:
            chunk = await self.ffmpeg.stdout.read(self.chunk_bytes)
            if chunk:
                self.raw_pcm.extend(chunk)
                pending.extend(chunk)
                while len(pending) >= self.chunk_bytes:
                    ok, sent_chunks, sent_bytes = await self._send_pcm_chunk(
                        bytes(pending[: self.chunk_bytes]),
                        sent_chunks=sent_chunks,
                        sent_bytes=sent_bytes,
                    )
                    if not ok:
                        return
                    del pending[: self.chunk_bytes]
                continue

            if pending:
                if len(pending) < self.min_chunk_bytes:
                    self.logger.log(
                        "stt_chunk_padded",
                        turn=self.turn_index,
                        original_bytes=len(pending),
                        padded_bytes=self.min_chunk_bytes,
                    )
                    pending.extend(b"\x00" * (self.min_chunk_bytes - len(pending)))
                ok, sent_chunks, sent_bytes = await self._send_pcm_chunk(
                    bytes(pending),
                    sent_chunks=sent_chunks,
                    sent_bytes=sent_bytes,
                )
                if not ok:
                    return
                pending.clear()

            self.logger.log(
                "ffmpeg_audio_eof",
                turn=self.turn_index,
                chunks=sent_chunks,
                bytes=sent_bytes,
            )
            return

    async def _receive_transcript(self) -> None:
        last_partial = ""
        last_final = ""
        while True:
            try:
                assert self.websocket is not None
                raw = await asyncio.wait_for(self.websocket.recv(), timeout=0.25)
            except asyncio.TimeoutError:
                if self.end_turn_sent.is_set() and (
                    time.monotonic() - self.last_event_at
                ) >= self.args.stt_idle_finish_secs:
                    self.logger.log("stt_idle_finish", turn=self.turn_index)
                    return
                continue
            except ConnectionClosedOK as exc:
                self.logger.log(
                    "stt_receiver_closed_ok",
                    turn=self.turn_index,
                    code=exc.rcvd.code if exc.rcvd else 1000,
                )
                return
            except ConnectionClosed as exc:
                self.logger.log(
                    "stt_receiver_closed",
                    turn=self.turn_index,
                    code=exc.rcvd.code if exc.rcvd else None,
                    reason=exc.rcvd.reason if exc.rcvd else "",
                )
                return
            except ssl.SSLError as exc:
                self.logger.log("stt_receiver_ssl_error", turn=self.turn_index, error=str(exc))
                return

            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            message = extract_transcription_message(raw)
            if not message:
                continue
            text, is_final = message
            self.last_event_at = time.monotonic()
            if is_final:
                if text != last_final:
                    self.finals.append(text)
                    last_final = text
                    print_log(f"[turn {self.turn_index}] seller final: {text}")
                    self.logger.log("seller_final", turn=self.turn_index, text=text)
                    await self._emit("seller_final", turn=self.turn_index, text=text)
            else:
                if text != last_partial:
                    self.partials.append(text)
                    last_partial = text
                    print_log(f"[turn {self.turn_index}] seller partial: {text}")
                    self.logger.log("seller_partial", turn=self.turn_index, text=text)
                    await self._emit("seller_partial", turn=self.turn_index, text=text)

    async def stop(self) -> TranscriptResult:
        self.stop_requested.set()
        self.logger.log("record_stop_requested", turn=self.turn_index)
        print_log(f"[turn {self.turn_index}] stopping capture...")

        if self.ffmpeg is not None and self.ffmpeg.stdin is not None:
            try:
                self.ffmpeg.stdin.write(b"q\n")
                await self.ffmpeg.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass

        if self.send_task is not None:
            try:
                await asyncio.wait_for(self.send_task, timeout=4.0)
            except asyncio.TimeoutError:
                self.send_task.cancel()
                self.logger.log("ffmpeg_drain_timeout", turn=self.turn_index, timeout_secs=4.0)
            except ConnectionClosed:
                pass

        try:
            if self.websocket is not None:
                await self.websocket.send(json.dumps({"end_turn": {}}))
                self.logger.log("end_turn", turn=self.turn_index)
        except ConnectionClosed:
            self.logger.log("end_turn_skipped_closed_socket", turn=self.turn_index)
        finally:
            self.end_turn_sent.set()

        if self.recv_task is not None:
            try:
                await asyncio.wait_for(self.recv_task, timeout=self.args.stt_timeout_secs)
            except asyncio.TimeoutError:
                self.recv_task.cancel()
                self.logger.log("stt_receive_timeout", turn=self.turn_index, timeout_secs=self.args.stt_timeout_secs)
                print_log(f"[turn {self.turn_index}] STT tail timeout after {self.args.stt_timeout_secs}s")

        try:
            if self.websocket is not None:
                await self.websocket.send(json.dumps({"close_stream": {}}))
        except ConnectionClosed:
            pass
        finally:
            await self._finalize_process()
            await self._close_websocket()

        transcript_text = " ".join(self.finals).strip() or (self.finals[-1] if self.finals else "") or (
            self.partials[-1] if self.partials else ""
        )
        elapsed_ms = int((time.monotonic() - self.started_at) * 1000)
        self.logger.log(
            "record_stop",
            turn=self.turn_index,
            transcript=transcript_text,
            elapsed_ms=elapsed_ms,
            audio_path=str(self.wav_path),
            ffmpeg_log_path=str(self.ffmpeg_log_path),
        )
        await self._emit(
            "recording_stopped",
            turn=self.turn_index,
            transcript=transcript_text,
            elapsed_ms=elapsed_ms,
        )
        if not transcript_text:
            raise RuntimeError("STT returned an empty transcript.")
        return TranscriptResult(
            text=transcript_text,
            partials=list(self.partials),
            finals=list(self.finals),
            elapsed_ms=elapsed_ms,
            audio_path=self.wav_path,
            ffmpeg_log_path=self.ffmpeg_log_path,
        )

    async def _finalize_process(self) -> None:
        if self.ffmpeg is None:
            return
        if self.ffmpeg.returncode is None:
            self.ffmpeg.terminate()
            try:
                await asyncio.wait_for(self.ffmpeg.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self.ffmpeg.kill()
                await self.ffmpeg.wait()
        self.stderr_bytes = await self.ffmpeg.stderr.read() if self.ffmpeg.stderr is not None else b""
        with self.ffmpeg_log_path.open("wb") as handle:
            handle.write(self.stderr_bytes)
        if self.args.save_audio:
            write_pcm_wav(self.wav_path, bytes(self.raw_pcm))

    async def _close_websocket(self) -> None:
        if self.websocket is None:
            return
        try:
            await self.websocket.close()
        except Exception:
            pass
        finally:
            self.websocket = None


async def stream_seller_turn(
    *,
    args: argparse.Namespace,
    logger: SessionLogger,
    turn_index: int,
    device_index: int,
    device_name: str,
) -> TranscriptResult:
    recorder = LiveSellerRecorder(
        args=args,
        logger=logger,
        turn_index=turn_index,
        device_index=device_index,
        device_name=device_name,
    )
    await recorder.start()
    await asyncio.to_thread(input, f"[turn {turn_index}] Press Enter to stop recording > ")
    return await recorder.stop()


async def run_session(args: argparse.Namespace) -> int:
    reference = parse_reference_script(args.input, args.script)
    device_index, device_name = choose_audio_device(args)
    logger = SessionLogger(args.output_dir)
    history: list[tuple[str, str]] = []

    print_log(
        f"[session] persona={args.persona_mode} script={reference.number}:{reference.title} "
        f"mic=[{device_index}] {device_name}"
    )
    print_log("[session] Enter = start turn, q = quit. После старта говоришь свою реплику и жмешь Enter.")
    logger.log(
        "session_start",
        persona_mode=args.persona_mode,
        script_number=reference.number,
        script_title=reference.title,
        device_index=device_index,
        device_name=device_name,
    )

    for turn_index in range(1, args.max_turns + 1):
        command = (await asyncio.to_thread(input, f"\n[turn {turn_index}] Enter to talk, q to quit > ")).strip().lower()
        if command in {"q", "quit", "exit"}:
            logger.log("session_stop", reason="user_exit", turns_completed=turn_index - 1)
            print_log("[session] stopped by user.")
            return 0

        transcript = await stream_seller_turn(
            args=args,
            logger=logger,
            turn_index=turn_index,
            device_index=device_index,
            device_name=device_name,
        )
        history.append(("Seller", transcript.text))
        logger.append_dialogue("Seller", transcript.text)

        print_log(f"[turn {turn_index}] generating client reply...")
        llm_started_at = time.monotonic()
        client_text = generate_client_reply(
            args=args,
            reference=reference,
            history=history,
            seller_transcript=transcript.text,
        )
        llm_elapsed_ms = int((time.monotonic() - llm_started_at) * 1000)
        logger.log("client_reply_text", turn=turn_index, text=client_text, elapsed_ms=llm_elapsed_ms)
        print_log(f"[turn {turn_index}] client: {client_text}")
        history.append(("Client", client_text))
        logger.append_dialogue("Client", client_text)

        audio_path: Path | None = None
        if args.save_audio or args.play:
            tts_started_at = time.monotonic()
            pcm = synthesize_inworld_text(args, client_text)
            tts_elapsed_ms = int((time.monotonic() - tts_started_at) * 1000)
            audio_path = args.output_dir / f"client_turn_{turn_index:03d}.wav"
            write_pcm_wav(audio_path, pcm)
            logger.log(
                "client_reply_audio",
                turn=turn_index,
                elapsed_ms=tts_elapsed_ms,
                audio_path=str(audio_path),
            )
            if args.play and audio_path.exists():
                print_log(f"[turn {turn_index}] playing client audio...")
                play_audio(audio_path)

    logger.log("session_stop", reason="max_turns_reached", turns_completed=args.max_turns)
    print_log("[session] max turns reached.")
    return 0


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    args.output_dir = args.output_dir.resolve()
    for env_file in args.env_file:
        load_env_file(env_file)
    if args.list_devices:
        print_audio_devices()
        return 0
    return asyncio.run(run_session(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
