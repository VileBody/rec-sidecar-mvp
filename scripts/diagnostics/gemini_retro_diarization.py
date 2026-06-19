#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
import wave
from pathlib import Path


DEFAULT_SELLER_WAV = (
    "logs/call_simulator/both-sides-smoke-20260619-094133/seller_smoke.wav"
)
DEFAULT_CLIENT_WAV = (
    "logs/call_simulator/both-sides-smoke-20260619-094133/client_smoke.wav"
)
DEFAULT_MODEL = "gemini-2.5-flash"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ask Gemini to retrospectively estimate speaker-change times for a local WAV."
    )
    parser.add_argument("--seller-wav", default=DEFAULT_SELLER_WAV)
    parser.add_argument("--client-wav", default=DEFAULT_CLIENT_WAV)
    parser.add_argument("--out", default="logs/diagnostics/gemini_retro_mix.wav")
    parser.add_argument("--model", default=os.getenv("GEMINI_DIARIZATION_MODEL", DEFAULT_MODEL))
    parser.add_argument("--provider", choices=["auto", "gemini-api", "vertex"], default="auto")
    parser.add_argument("--location", default=os.getenv("VERTEX_LOCATION", "global"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv(Path(".env"))
    load_dotenv(Path(".env.iac"))
    out_path = Path(args.out)
    audio_secs = build_mix(Path(args.seller_wav), Path(args.client_wav), out_path)
    print(f"mix={out_path} audio_secs={audio_secs:.2f} model={args.model}")

    if args.dry_run:
        print("dry_run=true")
        return 0

    audio_b64 = base64.b64encode(out_path.read_bytes()).decode("ascii")
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            "You are given a Russian two-speaker sales-call audio. "
                            "Return only JSON. Estimate speaker-change times retrospectively. "
                            "Use speaker labels S1/S2 and guess role as seller/client/unknown only when clear. "
                            "Schema: {\"segments\":[{\"start_sec\":number,\"end_sec\":number,"
                            "\"speaker\":\"S1|S2\",\"role_guess\":\"seller|client|unknown\","
                            "\"confidence\":number,\"evidence\":\"short text\"}],\"notes\":\"short\"}. "
                            "Prefer fewer stable segments over word-level fragmentation."
                        )
                    },
                    {"inlineData": {"mimeType": "audio/wav", "data": audio_b64}},
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": diarization_response_schema(),
        },
    }
    started = time.perf_counter()
    try:
        response, provider = generate_content(args, body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"gemini_http_error={exc.code}: {detail}", file=sys.stderr)
        return 3
    except urllib.error.URLError as exc:
        print(f"gemini_url_error={exc}", file=sys.stderr)
        return 3
    elapsed = time.perf_counter() - started

    text = response_text(response)
    parsed = parse_json_text(text)
    summary = {
        "provider": provider,
        "model": args.model,
        "audio_secs": round(audio_secs, 3),
        "latency_secs": round(elapsed, 3),
        "rtf": round(elapsed / max(audio_secs, 0.001), 3),
        "raw_text": text,
        "parsed": parsed,
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


def generate_content(args: argparse.Namespace, body: dict[str, object]) -> tuple[dict[str, object], str]:
    if args.provider in {"auto", "gemini-api"}:
        api_key = (
            os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
            or os.getenv("VERTEX_AI_API_KEY")
        )
        if api_key:
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                + args.model
                + ":generateContent?key="
                + api_key
            )
            try:
                return post_json(url, body), "gemini-api"
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if args.provider == "gemini-api" or "API_KEY_SERVICE_BLOCKED" not in detail:
                    raise HTTPErrorWithBody(exc, detail) from exc
                print("gemini_api_blocked=true; falling back to vertex adc", file=sys.stderr)
        elif args.provider == "gemini-api":
            raise RuntimeError("missing GEMINI_API_KEY/GOOGLE_API_KEY/VERTEX_AI_API_KEY")

    return post_json(vertex_url(args), body, headers=vertex_headers()), "vertex"


def post_json(
    url: str,
    body: dict[str, object],
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    raw = json.dumps(body).encode("utf-8")
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        url,
        data=raw,
        headers=request_headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


class HTTPErrorWithBody(urllib.error.HTTPError):
    def __init__(self, original: urllib.error.HTTPError, body: str):
        super().__init__(original.url, original.code, original.reason, original.headers, None)
        self._body = body.encode("utf-8")

    def read(self, *args: object, **kwargs: object) -> bytes:
        return self._body


def vertex_url(args: argparse.Namespace) -> str:
    project = (
        os.getenv("GOOGLE_CLOUD_PROJECT")
        or os.getenv("VERTEX_PROJECT_ID")
        or os.getenv("VERTEX_PROJECT")
        or adc_credentials().get("quota_project_id")
    )
    if not project:
        raise RuntimeError("missing GOOGLE_CLOUD_PROJECT/VERTEX_PROJECT_ID and ADC quota_project_id")
    location = args.location or "global"
    api_base = (
        "https://aiplatform.googleapis.com"
        if location == "global"
        else f"https://{location}-aiplatform.googleapis.com"
    )
    return (
        f"{api_base}/v1/projects/{project}/locations/{location}"
        f"/publishers/google/models/{args.model}:generateContent"
    )


def vertex_headers() -> dict[str, str]:
    token = vertex_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    quota_project = adc_credentials().get("quota_project_id")
    if isinstance(quota_project, str) and quota_project:
        headers["x-goog-user-project"] = quota_project
    return headers


def vertex_access_token() -> str:
    credentials = adc_credentials()
    data = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": required_adc_field(credentials, "client_id"),
            "client_secret": required_adc_field(credentials, "client_secret"),
            "refresh_token": required_adc_field(credentials, "refresh_token"),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        value = json.loads(response.read().decode("utf-8"))
    return str(value["access_token"])


_ADC_CACHE: dict[str, object] | None = None


def adc_credentials() -> dict[str, object]:
    global _ADC_CACHE
    if _ADC_CACHE is not None:
        return _ADC_CACHE
    path = (
        os.getenv("VERTEX_ADC_CREDENTIALS")
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        or "~/.config/gcloud/application_default_credentials.json"
    )
    _ADC_CACHE = json.loads(Path(path).expanduser().read_text())
    return _ADC_CACHE


def required_adc_field(credentials: dict[str, object], key: str) -> str:
    value = credentials.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"ADC credentials missing {key}")
    return value


def response_text(value: dict[str, object]) -> str:
    candidates = value.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        return ""
    content = candidate.get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    return "".join(part.get("text", "") for part in parts if isinstance(part, dict))


def diarization_response_schema() -> dict[str, object]:
    return {
        "type": "OBJECT",
        "properties": {
            "segments": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "start_sec": {"type": "NUMBER"},
                        "end_sec": {"type": "NUMBER"},
                        "speaker": {"type": "STRING", "enum": ["S1", "S2", "S3", "S4"]},
                        "role_guess": {
                            "type": "STRING",
                            "enum": ["seller", "client", "unknown"],
                        },
                        "confidence": {"type": "NUMBER"},
                        "evidence": {"type": "STRING"},
                    },
                    "required": [
                        "start_sec",
                        "end_sec",
                        "speaker",
                        "role_guess",
                        "confidence",
                        "evidence",
                    ],
                },
            },
            "notes": {"type": "STRING"},
        },
        "required": ["segments", "notes"],
    }


def parse_json_text(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            return json.loads(text[start : end + 1])
        except ValueError:
            return None


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
