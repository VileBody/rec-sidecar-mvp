#!/usr/bin/env python3
"""Benchmark realtime STT latency across Soniox native and the old Inworld proxy path."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import ssl
import statistics
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import certifi
from websockets.asyncio.client import connect


REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
NATIVE_SONIOX_WS_URL = "wss://stt-rt.soniox.com/transcribe-websocket"
INWORLD_STT_WS_URL = "wss://api.inworld.ai/stt/v1/transcribe:streamBidirectional"


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


def latest_smoke_wavs() -> tuple[Path, Path]:
    roots = sorted((REPO_ROOT / "logs" / "call_simulator").glob("both-sides-smoke-*"))
    if not roots:
        raise RuntimeError("No logs/call_simulator/both-sides-smoke-* directory found")
    root = roots[-1]
    return root / "seller_smoke.wav", root / "client_smoke.wav"


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


def silence_ms(ms: int) -> bytes:
    return b"\x00" * int(SAMPLE_RATE * SAMPLE_WIDTH * ms / 1000)


def build_conversation_pcm(seller_pcm: bytes, client_pcm: bytes) -> bytes:
    return (
        seller_pcm
        + silence_ms(350)
        + client_pcm
        + silence_ms(350)
        + seller_pcm[: len(seller_pcm) // 2]
    )


def inworld_authorization(api_key: str) -> str:
    if api_key.lower().startswith("basic "):
        return api_key
    return f"Basic {api_key}"


def compact_text(text: str) -> str:
    return " ".join(text.split())


def native_text_from_message(value: dict[str, Any]) -> str:
    tokens = value.get("tokens")
    if not isinstance(tokens, list):
        return ""
    parts: list[str] = []
    for item in tokens:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "")
        if text and text != "<fin>":
            parts.append(text)
    return compact_text("".join(parts))


def native_has_final(value: dict[str, Any]) -> bool:
    tokens = value.get("tokens")
    if not isinstance(tokens, list):
        return False
    return any(isinstance(item, dict) and bool(item.get("is_final")) for item in tokens)


def inworld_text_from_message(value: dict[str, Any]) -> tuple[str, bool]:
    transcription: dict[str, Any] | None = None
    result = value.get("result")
    if isinstance(result, dict) and isinstance(result.get("transcription"), dict):
        transcription = result["transcription"]
    elif isinstance(value.get("transcription"), dict):
        transcription = value["transcription"]
    if not transcription:
        return "", False
    text = compact_text(str(transcription.get("transcript") or ""))
    final = bool(transcription.get("isFinal") or transcription.get("is_final"))
    return text, final


@dataclass
class BenchResult:
    name: str
    provider: str
    model: str
    audio_secs: float
    ok: bool = True
    error: str = ""
    responses: int = 0
    text_updates: int = 0
    final_updates: int = 0
    finished: bool = False
    first_response_ms: int | None = None
    first_text_ms: int | None = None
    first_final_ms: int | None = None
    finished_ms: int | None = None
    send_done_ms: int | None = None
    total_ms: int | None = None
    sample_text: str = ""
    response_intervals_ms: list[int] = field(default_factory=list)

    def row(self) -> str:
        fields = [
            self.name,
            self.model,
            fmt_ms(self.first_response_ms),
            fmt_ms(self.first_text_ms),
            fmt_ms(self.first_final_ms),
            fmt_ms(self.finished_ms),
            fmt_ms(self.total_ms),
            str(self.responses),
            str(self.text_updates),
            "ok" if self.ok else f"ERR {self.error}",
        ]
        return " | ".join(fields)


def fmt_ms(value: int | None) -> str:
    return "-" if value is None else f"{value}ms"


def elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


async def send_native_pcm(websocket, pcm: bytes, chunk_ms: int, fast: bool) -> None:
    chunk_size = int(SAMPLE_RATE * SAMPLE_WIDTH * chunk_ms / 1000)
    for offset in range(0, len(pcm), chunk_size):
        await websocket.send(pcm[offset : offset + chunk_size])
        if not fast:
            await asyncio.sleep(chunk_ms / 1000)
    await websocket.send("")


async def send_inworld_pcm(websocket, pcm: bytes, chunk_ms: int, fast: bool) -> None:
    chunk_size = int(SAMPLE_RATE * SAMPLE_WIDTH * chunk_ms / 1000)
    min_chunk = int(SAMPLE_RATE * SAMPLE_WIDTH * 20 / 1000)
    for offset in range(0, len(pcm), chunk_size):
        chunk = pcm[offset : offset + chunk_size]
        if len(chunk) < min_chunk:
            padded = bytearray(min_chunk)
            padded[: len(chunk)] = chunk
            chunk = bytes(padded)
        await websocket.send(
            json.dumps(
                {"audio_chunk": {"content": base64.b64encode(chunk).decode("ascii")}},
                ensure_ascii=False,
            )
        )
        if not fast:
            await asyncio.sleep(chunk_ms / 1000)
    await websocket.send(json.dumps({"end_turn": {}}, ensure_ascii=False))
    await websocket.send(json.dumps({"close_stream": {}}, ensure_ascii=False))


async def bench_native_soniox(
    *,
    pcm: bytes,
    name: str,
    model: str,
    args: argparse.Namespace,
) -> BenchResult:
    api_key = os.getenv("SONIOX_API_KEY", "").strip()
    if not api_key:
        return BenchResult(name, "soniox", model, audio_secs(pcm), ok=False, error="missing SONIOX_API_KEY")

    result = BenchResult(name=name, provider="soniox", model=model, audio_secs=audio_secs(pcm))
    context = ssl.create_default_context(cafile=certifi.where())
    started = time.perf_counter()
    last_response_ms: int | None = None
    try:
        async with connect(
            args.soniox_ws_url,
            ssl=context,
            open_timeout=args.open_timeout_secs,
            max_size=16 * 1024 * 1024,
            ping_interval=None,
        ) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "api_key": api_key,
                        "model": model,
                        "audio_format": args.audio_format,
                        "sample_rate": SAMPLE_RATE,
                        "num_channels": 1,
                        "language_hints": [args.language],
                        "language_hints_strict": args.language_strict,
                        "enable_speaker_diarization": True,
                        "enable_language_identification": False,
                        "enable_endpoint_detection": args.endpoint_detection,
                    },
                    ensure_ascii=False,
                )
            )

            async def sender() -> None:
                await send_native_pcm(websocket, pcm, args.chunk_ms, args.fast)
                result.send_done_ms = elapsed_ms(started)

            send_task = asyncio.create_task(sender())
            while True:
                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=args.timeout_secs)
                except asyncio.TimeoutError:
                    if send_task.done():
                        break
                    raise
                now_ms = elapsed_ms(started)
                if last_response_ms is not None:
                    result.response_intervals_ms.append(now_ms - last_response_ms)
                last_response_ms = now_ms
                result.responses += 1
                result.first_response_ms = result.first_response_ms or now_ms
                value = json.loads(raw)
                if value.get("error_code"):
                    raise RuntimeError(json.dumps(value, ensure_ascii=False))
                text = native_text_from_message(value)
                if text:
                    result.text_updates += 1
                    result.first_text_ms = result.first_text_ms or now_ms
                    result.sample_text = text
                if native_has_final(value):
                    result.final_updates += 1
                    result.first_final_ms = result.first_final_ms or now_ms
                if value.get("finished"):
                    result.finished = True
                    result.finished_ms = now_ms
                    break
            await send_task
    except Exception as exc:  # noqa: BLE001 - diagnostic script should report lane errors.
        result.ok = False
        result.error = str(exc)
    result.total_ms = elapsed_ms(started)
    return result


async def bench_inworld_proxy(
    *,
    pcm: bytes,
    args: argparse.Namespace,
) -> BenchResult:
    api_key = os.getenv("INWORLD_API_KEY", "").strip()
    model = args.inworld_model
    if not api_key:
        return BenchResult("inworld-rust-proxy-v4", "inworld", model, audio_secs(pcm), ok=False, error="missing INWORLD_API_KEY")

    result = BenchResult(
        name="inworld-rust-proxy-v4",
        provider="inworld",
        model=model,
        audio_secs=audio_secs(pcm),
    )
    context = ssl.create_default_context(cafile=certifi.where())
    started = time.perf_counter()
    last_response_ms: int | None = None
    try:
        async with connect(
            args.inworld_ws_url,
            additional_headers={"Authorization": inworld_authorization(api_key)},
            ssl=context,
            open_timeout=args.open_timeout_secs,
            max_size=16 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        ) as websocket:
            transcribe_config: dict[str, Any] = {
                "modelId": model,
                "audioEncoding": "LINEAR16",
                "sampleRateHertz": SAMPLE_RATE,
                "numberOfChannels": 1,
                "enableSpeakerDiarization": True,
            }
            if args.inworld_language:
                transcribe_config["language"] = args.inworld_language
            else:
                transcribe_config["enableLanguageDetection"] = True
            await websocket.send(json.dumps({"transcribe_config": transcribe_config}, ensure_ascii=False))

            async def sender() -> None:
                await send_inworld_pcm(websocket, pcm, args.chunk_ms, args.fast)
                result.send_done_ms = elapsed_ms(started)

            send_task = asyncio.create_task(sender())
            while True:
                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=args.timeout_secs)
                except asyncio.TimeoutError:
                    if send_task.done():
                        break
                    raise
                now_ms = elapsed_ms(started)
                if last_response_ms is not None:
                    result.response_intervals_ms.append(now_ms - last_response_ms)
                last_response_ms = now_ms
                result.responses += 1
                result.first_response_ms = result.first_response_ms or now_ms
                value = json.loads(raw)
                if value.get("code") is not None or value.get("error"):
                    raise RuntimeError(json.dumps(value, ensure_ascii=False))
                text, final = inworld_text_from_message(value)
                if text:
                    result.text_updates += 1
                    result.first_text_ms = result.first_text_ms or now_ms
                    result.sample_text = text
                if final:
                    result.final_updates += 1
                    result.first_final_ms = result.first_final_ms or now_ms
                if send_task.done() and final:
                    break
            await send_task
    except Exception as exc:  # noqa: BLE001 - diagnostic script should report lane errors.
        result.ok = False
        result.error = str(exc)
    result.total_ms = elapsed_ms(started)
    return result


def audio_secs(pcm: bytes) -> float:
    return len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH)


def percentile(values: list[int], ratio: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * ratio)))
    return ordered[index]


def print_result_details(result: BenchResult) -> None:
    intervals = result.response_intervals_ms
    if intervals:
        interval_summary = (
            f"response_interval_avg={int(statistics.mean(intervals))}ms "
            f"p50={percentile(intervals, 0.5)}ms p90={percentile(intervals, 0.9)}ms"
        )
    else:
        interval_summary = "response_interval_avg=- p50=- p90=-"
    print(
        f"\n[{result.name}] provider={result.provider} model={result.model} "
        f"audio={result.audio_secs:.2f}s ok={result.ok}"
    )
    print(
        "  "
        f"first_response={fmt_ms(result.first_response_ms)} "
        f"first_text={fmt_ms(result.first_text_ms)} "
        f"first_final={fmt_ms(result.first_final_ms)} "
        f"send_done={fmt_ms(result.send_done_ms)} "
        f"finished={fmt_ms(result.finished_ms)} "
        f"total={fmt_ms(result.total_ms)}"
    )
    print(
        "  "
        f"responses={result.responses} text_updates={result.text_updates} "
        f"final_updates={result.final_updates} {interval_summary}"
    )
    if result.sample_text:
        print(f"  sample_text={result.sample_text[:240]}")
    if result.error:
        print(f"  error={result.error}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seller-wav", type=Path)
    parser.add_argument("--client-wav", type=Path)
    parser.add_argument("--chunk-ms", type=int, default=100)
    parser.add_argument("--fast", action="store_true", help="Send audio as fast as possible instead of realtime pacing.")
    parser.add_argument("--timeout-secs", type=float, default=15)
    parser.add_argument("--open-timeout-secs", type=float, default=10)
    parser.add_argument("--language", default=os.getenv("SONIOX_LANGUAGE", "ru"))
    parser.add_argument("--language-strict", action="store_true")
    parser.add_argument("--audio-format", default=os.getenv("SONIOX_AUDIO_FORMAT", "s16le"))
    parser.add_argument("--endpoint-detection", action="store_true")
    parser.add_argument("--soniox-ws-url", default=os.getenv("SONIOX_STT_WS_URL", NATIVE_SONIOX_WS_URL))
    parser.add_argument("--soniox-v4-model", default="stt-rt-v4")
    parser.add_argument("--soniox-v5-model", default="stt-rt-v5")
    parser.add_argument("--inworld-ws-url", default=os.getenv("INWORLD_STT_WS_URL", INWORLD_STT_WS_URL))
    parser.add_argument("--inworld-model", default=os.getenv("INWORLD_STT_MODEL", "soniox/stt-rt-v4"))
    parser.add_argument("--inworld-language", default=os.getenv("INWORLD_STT_LANGUAGE", ""))
    return parser.parse_args()


async def run(args: argparse.Namespace) -> list[BenchResult]:
    seller_wav, client_wav = latest_smoke_wavs()
    if args.seller_wav:
        seller_wav = args.seller_wav
    if args.client_wav:
        client_wav = args.client_wav
    pcm = build_conversation_pcm(read_pcm(seller_wav), read_pcm(client_wav))
    print(f"seller_wav={seller_wav}")
    print(f"client_wav={client_wav}")
    print(f"audio_secs={audio_secs(pcm):.2f} chunk_ms={args.chunk_ms} fast={args.fast}")

    lanes = [
        bench_native_soniox(pcm=pcm, name="soniox-native-v4", model=args.soniox_v4_model, args=args),
        bench_native_soniox(pcm=pcm, name="soniox-native-v5", model=args.soniox_v5_model, args=args),
        bench_inworld_proxy(pcm=pcm, args=args),
    ]
    results: list[BenchResult] = []
    for lane in lanes:
        result = await lane
        results.append(result)
        print_result_details(result)
    return results


def main() -> int:
    load_env_file(REPO_ROOT / ".env")
    load_env_file(REPO_ROOT / ".env.local")
    load_env_file(REPO_ROOT / ".env.iac")
    args = parse_args()
    results = asyncio.run(run(args))
    print("\nsummary")
    print("lane | model | first_response | first_text | first_final | finished | total | responses | text_updates | status")
    for result in results:
        print(result.row())
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
