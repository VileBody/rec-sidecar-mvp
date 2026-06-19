#!/usr/bin/env python3
"""Check Inworld/Soniox speaker diarization on a known seller/client WAV pair."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import ssl
import wave
from pathlib import Path

import certifi
from websockets.asyncio.client import connect


REPO_ROOT = Path(__file__).resolve().parents[2]
STT_WS_URL = "wss://api.inworld.ai/stt/v1/transcribe:streamBidirectional"
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


def auth_header() -> str:
    api_key = os.getenv("INWORLD_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing INWORLD_API_KEY")
    if api_key.lower().startswith("basic "):
        return api_key
    return f"Basic {api_key}"


def read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != SAMPLE_RATE or handle.getnchannels() != 1 or handle.getsampwidth() != SAMPLE_WIDTH:
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


def compact_tokens(tokens: list[dict]) -> list[dict[str, str]]:
    segments: list[dict[str, str]] = []
    speaker = ""
    text = ""
    for token in tokens:
        word = str(token.get("word") or "").strip()
        if not word:
            continue
        next_speaker = str(token.get("speaker") or "")
        if text and next_speaker != speaker:
            segments.append({"speaker": speaker, "text": " ".join(text.split())})
            text = ""
        speaker = next_speaker
        text += str(token.get("word") or "")
    if text:
        segments.append({"speaker": speaker, "text": " ".join(text.split())})
    return segments


async def send_pcm(websocket, pcm: bytes, realtime: bool) -> None:
    chunk_size = 3200
    for offset in range(0, len(pcm), chunk_size):
        chunk = pcm[offset : offset + chunk_size]
        await websocket.send(
            json.dumps({"audio_chunk": {"content": base64.b64encode(chunk).decode("ascii")}})
        )
        if realtime:
            await asyncio.sleep(0.1)


async def collect_finals(websocket, *, timeout_after_final: float = 1.8) -> list[dict]:
    final_transcriptions: list[dict] = []
    last_transcription: dict = {}
    for _ in range(80):
        timeout = timeout_after_final if final_transcriptions else 10
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            break
        value = json.loads(raw)
        if value.get("error"):
            raise RuntimeError(json.dumps(value["error"], ensure_ascii=False))
        transcription = (
            value.get("result", {}).get("transcription")
            or value.get("transcription")
            or {}
        )
        if transcription:
            last_transcription = transcription
        if transcription.get("isFinal") or transcription.get("is_final"):
            final_transcriptions.append(transcription)
            break
    return final_transcriptions or ([last_transcription] if last_transcription else [])


async def transcribe_turns(turns: list[bytes], model: str, realtime: bool) -> list[dict]:
    context = ssl.create_default_context(cafile=certifi.where())
    async with connect(
        STT_WS_URL,
        additional_headers={"Authorization": auth_header()},
        ssl=context,
        open_timeout=10,
        max_size=8 * 1024 * 1024,
        ping_interval=None,
    ) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "transcribe_config": {
                        "modelId": model,
                        "audioEncoding": "LINEAR16",
                        "sampleRateHertz": SAMPLE_RATE,
                        "numberOfChannels": 1,
                        "language": "ru",
                        "enableSpeakerDiarization": True,
                    }
                },
                ensure_ascii=False,
            )
        )
        final_transcriptions: list[dict] = []
        for pcm in turns:
            await send_pcm(websocket, pcm, realtime)
            await websocket.send(json.dumps({"end_turn": {}}))
            final_transcriptions.extend(await collect_finals(websocket))
        try:
            await websocket.send(json.dumps({"close_stream": {}}))
        except Exception:
            pass
        return final_transcriptions


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seller-wav", type=Path)
    parser.add_argument("--client-wav", type=Path)
    parser.add_argument("--model", default=os.getenv("INWORLD_STT_MODEL", "soniox/stt-rt-v4"))
    parser.add_argument("--fast", action="store_true", help="Send audio as fast as possible instead of pacing chunks.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env_file(REPO_ROOT / ".env")
    load_env_file(REPO_ROOT / ".env.iac")
    seller_wav, client_wav = latest_smoke_wavs()
    if args.seller_wav:
        seller_wav = args.seller_wav
    if args.client_wav:
        client_wav = args.client_wav

    seller_pcm = read_pcm(seller_wav)
    client_pcm = read_pcm(client_wav)
    transcriptions = asyncio.run(transcribe_turns([seller_pcm, client_pcm], args.model, realtime=not args.fast))
    segments: list[dict[str, str]] = []
    transcripts: list[str] = []
    for transcription in transcriptions:
        value = str(transcription.get("transcript") or "").strip()
        if value:
            transcripts.append(value)
        tokens = transcription.get("wordTimestamps") or []
        segments.extend(compact_tokens(tokens if isinstance(tokens, list) else []))
    speakers = [segment["speaker"] for segment in segments]
    unique = []
    for speaker in speakers:
        if speaker and speaker not in unique:
            unique.append(speaker)

    print(f"model={args.model}")
    print(f"seller_wav={seller_wav}")
    print(f"client_wav={client_wav}")
    print(f"final_count={len(transcriptions)}")
    print(f"transcript={' | '.join(transcripts)}")
    print(f"speaker_count={len(unique)} speakers={','.join(unique) or '<none>'}")
    if len(unique) < 2:
        print("quality=poor reason=expected two speakers for seller/client smoke, got fewer than two")
    else:
        print("quality=ok reason=detected at least two speaker ids")
    print("segments:")
    for idx, segment in enumerate(segments, start=1):
        print(f"{idx}. speaker={segment['speaker'] or '?'} text={segment['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
