#!/usr/bin/env python3
"""Check native Soniox v5 realtime diarization on a known seller/client WAV pair."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import time
import wave
from pathlib import Path
from typing import Any

import certifi
from websockets.asyncio.client import connect


REPO_ROOT = Path(__file__).resolve().parents[2]
STT_WS_URL = "wss://stt-rt.soniox.com/transcribe-websocket"
SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2


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
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as handle:
        if (
            handle.getframerate() != SAMPLE_RATE
            or handle.getnchannels() != 1
            or handle.getsampwidth() != SAMPLE_WIDTH
        ):
            raise RuntimeError(
                f"{path} must be mono LINEAR16 {SAMPLE_RATE}Hz; got "
                f"{handle.getnchannels()}ch {handle.getframerate()}Hz width={handle.getsampwidth()}"
            )
        return handle.readframes(handle.getnframes())


def latest_smoke_wavs() -> tuple[Path, Path]:
    roots = sorted((REPO_ROOT / "logs" / "call_simulator").glob("both-sides-smoke-*"))
    if not roots:
        raise RuntimeError("No logs/call_simulator/both-sides-smoke-* directory found")
    root = roots[-1]
    return root / "seller_smoke.wav", root / "client_smoke.wav"


def silence_ms(ms: int) -> bytes:
    return b"\x00" * int(SAMPLE_RATE * SAMPLE_WIDTH * ms / 1000)


def build_conversation_pcm(seller_pcm: bytes, client_pcm: bytes) -> bytes:
    return (
        seller_pcm
        + silence_ms(350)
        + client_pcm
        + silence_ms(350)
        + seller_pcm[: len(seller_pcm) // 2]
        + silence_ms(350)
        + client_pcm[: len(client_pcm) // 2]
    )


def compact_segments(tokens: list[dict[str, Any]], *, only_final: bool) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    speaker = ""
    text = ""
    start_ms: int | None = None
    end_ms: int | None = None

    def flush() -> None:
        nonlocal text, start_ms, end_ms
        compact_text = "".join(text).strip()
        compact_text = " ".join(compact_text.split())
        if compact_text:
            segments.append(
                {
                    "speaker": speaker or "?",
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "text": compact_text,
                }
            )
        text = ""
        start_ms = None
        end_ms = None

    for token in tokens:
        if only_final and not token.get("is_final"):
            continue
        token_text = str(token.get("text") or "")
        if not token_text:
            continue
        token_speaker = str(token.get("speaker") or "?")
        if text and token_speaker != speaker:
            flush()
        speaker = token_speaker
        if start_ms is None and isinstance(token.get("start_ms"), int):
            start_ms = token["start_ms"]
        if isinstance(token.get("end_ms"), int):
            end_ms = token["end_ms"]
        text += token_text
    flush()
    return segments


async def send_pcm(websocket, pcm: bytes, realtime: bool) -> None:
    chunk_size = SAMPLE_RATE * SAMPLE_WIDTH // 10
    for offset in range(0, len(pcm), chunk_size):
        await websocket.send(pcm[offset : offset + chunk_size])
        if realtime:
            await asyncio.sleep(0.1)


async def transcribe(pcm: bytes, args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float]:
    api_key = os.getenv("SONIOX_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing SONIOX_API_KEY. Put it into ignored .env.local or export it.")

    context = ssl.create_default_context(cafile=certifi.where())
    started = time.perf_counter()
    latest_tokens: list[dict[str, Any]] = []
    final_tokens_by_key: dict[tuple[int, int, str, str], dict[str, Any]] = {}
    responses = 0
    async with connect(
        args.ws_url,
        ssl=context,
        open_timeout=10,
        max_size=16 * 1024 * 1024,
        ping_interval=None,
    ) as websocket:
        config: dict[str, Any] = {
            "api_key": api_key,
            "model": args.model,
            "audio_format": args.audio_format,
            "sample_rate": SAMPLE_RATE,
            "num_channels": 1,
            "language_hints": [args.language],
            "language_hints_strict": args.language_strict,
            "enable_speaker_diarization": True,
            "enable_language_identification": False,
            "enable_endpoint_detection": args.endpoint_detection,
        }
        if args.endpoint_detection:
            config["endpoint_sensitivity"] = args.endpoint_sensitivity
            config["max_endpoint_delay_ms"] = args.max_endpoint_delay_ms
        await websocket.send(json.dumps(config, ensure_ascii=False))
        await send_pcm(websocket, pcm, realtime=not args.fast)
        if args.finish_frame == "text":
            await websocket.send("")
        else:
            await websocket.send(b"")

        while True:
            raw = await asyncio.wait_for(websocket.recv(), timeout=args.timeout_secs)
            value = json.loads(raw)
            responses += 1
            if value.get("error_code"):
                raise RuntimeError(json.dumps(value, ensure_ascii=False))
            tokens = value.get("tokens")
            if isinstance(tokens, list):
                latest_tokens = [token for token in tokens if isinstance(token, dict)]
                for token in latest_tokens:
                    if not token.get("is_final"):
                        continue
                    key = (
                        int(token.get("start_ms") or -1),
                        int(token.get("end_ms") or -1),
                        str(token.get("text") or ""),
                        str(token.get("speaker") or ""),
                    )
                    final_tokens_by_key[key] = token
            if value.get("finished"):
                break
    elapsed = time.perf_counter() - started
    print(f"responses={responses}")
    return list(final_tokens_by_key.values()), latest_tokens, elapsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seller-wav", type=Path)
    parser.add_argument("--client-wav", type=Path)
    parser.add_argument("--model", default=os.getenv("SONIOX_STT_MODEL", "stt-rt-v5"))
    parser.add_argument("--ws-url", default=os.getenv("SONIOX_WS_URL", STT_WS_URL))
    parser.add_argument("--audio-format", default=os.getenv("SONIOX_AUDIO_FORMAT", "s16le"))
    parser.add_argument("--finish-frame", choices=["text", "binary"], default="text")
    parser.add_argument("--language", default=os.getenv("SONIOX_LANGUAGE", "ru"))
    parser.add_argument("--language-strict", action="store_true")
    parser.add_argument("--endpoint-detection", action="store_true")
    parser.add_argument("--endpoint-sensitivity", type=float, default=0.0)
    parser.add_argument("--max-endpoint-delay-ms", type=int, default=2000)
    parser.add_argument("--timeout-secs", type=float, default=20)
    parser.add_argument("--fast", action="store_true", help="Send audio as fast as possible instead of pacing chunks.")
    return parser.parse_args()


def main() -> int:
    load_env_file(REPO_ROOT / ".env")
    load_env_file(REPO_ROOT / ".env.local")
    load_env_file(REPO_ROOT / ".env.iac")
    args = parse_args()
    seller_wav, client_wav = latest_smoke_wavs()
    if args.seller_wav:
        seller_wav = args.seller_wav
    if args.client_wav:
        client_wav = args.client_wav

    seller_pcm = read_pcm(seller_wav)
    client_pcm = read_pcm(client_wav)
    pcm = build_conversation_pcm(seller_pcm, client_pcm)
    audio_secs = len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH)
    final_tokens, latest_tokens, elapsed = asyncio.run(transcribe(pcm, args))
    final_segments = compact_segments(final_tokens or latest_tokens, only_final=bool(final_tokens))
    all_segments = compact_segments(latest_tokens, only_final=False)
    speakers = sorted({segment["speaker"] for segment in final_segments if segment["speaker"] != "?"})

    print(f"model={args.model}")
    print(f"seller_wav={seller_wav}")
    print(f"client_wav={client_wav}")
    print(f"audio_secs={audio_secs:.2f}")
    print(f"elapsed_secs={elapsed:.3f}")
    print(f"rtf={elapsed / max(audio_secs, 0.001):.3f}")
    print(f"final_token_count={len(final_tokens)} latest_token_count={len(latest_tokens)}")
    print(f"speaker_count={len(speakers)} speakers={','.join(speakers) or '<none>'}")
    print("quality=ok reason=detected at least two speaker ids" if len(speakers) >= 2 else "quality=poor reason=expected two speaker ids")
    print("final_segments:")
    for idx, segment in enumerate(final_segments, start=1):
        print(
            f"{idx}. speaker={segment['speaker']} "
            f"start_ms={segment['start_ms']} end_ms={segment['end_ms']} text={segment['text']}"
        )
    if not final_segments and all_segments:
        print("all_segments:")
        for idx, segment in enumerate(all_segments, start=1):
            print(
                f"{idx}. speaker={segment['speaker']} "
                f"start_ms={segment['start_ms']} end_ms={segment['end_ms']} text={segment['text']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
