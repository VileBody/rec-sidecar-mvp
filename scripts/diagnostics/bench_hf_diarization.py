#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import wave
from pathlib import Path


DEFAULT_SELLER_WAV = (
    "logs/call_simulator/both-sides-smoke-20260619-094133/seller_smoke.wav"
)
DEFAULT_CLIENT_WAV = (
    "logs/call_simulator/both-sides-smoke-20260619-094133/client_smoke.wav"
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark HF/pyannote speaker diarization on a local seller/client smoke WAV."
    )
    parser.add_argument("--seller-wav", default=DEFAULT_SELLER_WAV)
    parser.add_argument("--client-wav", default=DEFAULT_CLIENT_WAV)
    parser.add_argument("--out", default="logs/diagnostics/hf_diarization_mix.wav")
    parser.add_argument("--model", default="pyannote/speaker-diarization-3.1")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv(Path(".env"))
    load_dotenv(Path(".env.iac"))
    out_path = Path(args.out)
    audio_secs = build_mix(Path(args.seller_wav), Path(args.client_wav), out_path)
    print(f"mix={out_path} audio_secs={audio_secs:.2f}")

    if args.dry_run:
        print("dry_run=true")
        return 0

    token = (
        os.getenv("HF_TOKEN")
        or os.getenv("HUGGINGFACE_TOKEN")
        or os.getenv("HUGGING_FACE_HUB_TOKEN")
    )
    if not token:
        print(
            "missing HF_TOKEN. Install deps and export a token accepted for the pyannote model.",
            file=sys.stderr,
        )
        print(
            "deps: python3 -m pip install 'pyannote.audio>=3.1' torch torchaudio",
            file=sys.stderr,
        )
        return 2

    try:
        from pyannote.audio import Pipeline
        import torch
    except ImportError as exc:
        print(f"missing pyannote deps: {exc}", file=sys.stderr)
        print(
            "deps: python3 -m pip install 'pyannote.audio>=3.1' torch torchaudio",
            file=sys.stderr,
        )
        return 2

    started = time.perf_counter()
    try:
        try:
            pipeline = Pipeline.from_pretrained(args.model, token=token)
        except TypeError:
            pipeline = Pipeline.from_pretrained(args.model, use_auth_token=token)
    except Exception as exc:
        print(f"model_load_error={exc}", file=sys.stderr)
        return 3
    load_secs = time.perf_counter() - started

    if args.device != "cpu":
        pipeline.to(torch.device(args.device))

    run_started = time.perf_counter()
    diarization = pipeline(str(out_path))
    run_secs = time.perf_counter() - run_started

    segments = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        segments.append(
            {
                "start_sec": round(float(turn.start), 3),
                "end_sec": round(float(turn.end), 3),
                "speaker": speaker,
            }
        )

    summary = {
        "model": args.model,
        "device": args.device,
        "audio_secs": round(audio_secs, 3),
        "load_secs": round(load_secs, 3),
        "run_secs": round(run_secs, 3),
        "rtf": round(run_secs / max(audio_secs, 0.001), 3),
        "speaker_count": len({segment["speaker"] for segment in segments}),
        "segments": segments,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.replace("export ", "").strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def build_mix(seller_path: Path, client_path: Path, out_path: Path) -> float:
    seller = read_wav(seller_path)
    client = read_wav(client_path)
    if seller["params"] != client["params"]:
        raise RuntimeError(f"WAV params differ: {seller_path} vs {client_path}")

    channels, sample_width, frame_rate = seller["params"]
    silence = b"\x00" * int(frame_rate * 0.35) * channels * sample_width
    pcm = (
        seller["pcm"]
        + silence
        + client["pcm"]
        + silence
        + seller["pcm"][: len(seller["pcm"]) // 2]
        + silence
        + client["pcm"][: len(client["pcm"]) // 2]
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(frame_rate)
        wav.writeframes(pcm)
    return len(pcm) / (channels * sample_width * frame_rate)


def read_wav(path: Path) -> dict[str, object]:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        frame_rate = wav.getframerate()
        pcm = wav.readframes(wav.getnframes())
    if channels != 1 or sample_width != 2 or frame_rate != 16000:
        raise RuntimeError(
            f"{path} must be mono 16-bit 16k WAV, got channels={channels} width={sample_width} rate={frame_rate}"
        )
    return {"params": (channels, sample_width, frame_rate), "pcm": pcm}


if __name__ == "__main__":
    raise SystemExit(main())
