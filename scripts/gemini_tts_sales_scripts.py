#!/usr/bin/env python3
"""Render two-speaker sales scripts with Gemini TTS."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import signal
import sys
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from google import genai
from google.genai import types


DEFAULT_INPUT = Path("sales_scripts/glubina_kazan_10_call_scripts_v1.md")
DEFAULT_OUTPUT_DIR = Path("sales_scripts/audio")
DEFAULT_MODEL = "gemini-3.1-flash-tts-preview"
DEFAULT_MAX_REQUEST_BYTES = 2800
SAMPLE_RATE_HZ = 24_000
SAMPLE_WIDTH_BYTES = 2
CHANNELS = 1


@dataclass(frozen=True)
class Turn:
    speaker: str
    text: str


@dataclass(frozen=True)
class Script:
    number: int
    title: str
    turns: list[Turn]


@dataclass(frozen=True)
class RenderedScript:
    script: Script
    output_path: Path
    chunk_count: int
    pcm_bytes: int
    elapsed_secs: float


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


def normalize_placeholders(text: str, client_name: str, seller_name: str) -> str:
    replacements = {
        "Здравствуйте, [Имя]": f"Здравствуйте, {client_name}",
        "Добрый день, [Имя]": f"Добрый день, {client_name}",
        "Это [Имя]": f"Это {seller_name}",
        "Меня зовут [Имя]": f"Меня зовут {seller_name}",
        "[Имя]": client_name,
        "[Отвечает.]": "У меня сервисный бизнес, команда восемь человек, сейчас уперлись в рост.",
        "[цель клиента]": "выйти из операционки и поднять прибыль",
        "[барьер клиента]": "хаос в команде и нехватка фокуса",
        "[физлицо/юрлицо]": "юрлицо",
        "[время]": "12:00",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def strip_markdown(text: str) -> str:
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = text.replace("—", "-")
    return re.sub(r"\s+", " ", text).strip()


def extract_scripts(markdown: str, client_name: str, seller_name: str) -> list[Script]:
    parts = re.split(r"(?m)^## Скрипт\s+(\d+)\.\s*(.+)$", markdown)
    scripts: list[Script] = []
    role_re = re.compile(r"^\*\*(Продавец|Клиент):\*\*\s*(.*)$")
    time_re = re.compile(r"^\*\*\d{2}:\d{2}")

    for i in range(1, len(parts), 3):
        number = int(parts[i])
        title = strip_markdown(parts[i + 1])
        body = parts[i + 2]
        turns: list[Turn] = []
        current_role: str | None = None

        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#") or time_re.match(line):
                current_role = None
                continue
            match = role_re.match(line)
            if match:
                role, text = match.groups()
                current_role = "Seller" if role == "Продавец" else "Client"
                text = normalize_placeholders(strip_markdown(text), client_name, seller_name)
                if text:
                    turns.append(Turn(current_role, text))
                continue
            if current_role and not line.startswith("**"):
                text = normalize_placeholders(strip_markdown(line), client_name, seller_name)
                if text:
                    previous = turns[-1]
                    turns[-1] = Turn(previous.speaker, f"{previous.text} {text}")

        if turns:
            scripts.append(Script(number=number, title=title, turns=turns))
    return scripts


def split_turns_into_chunks(
    script: Script,
    max_request_bytes: int,
    style_prompt: str,
) -> list[list[Turn]]:
    chunks: list[list[Turn]] = []
    current: list[Turn] = []

    for turn in script.turns:
        candidate = [*current, turn]
        candidate_prompt = build_prompt(script, candidate, style_prompt)
        if current and len(candidate_prompt.encode("utf-8")) > max_request_bytes:
            chunks.append(current)
            current = [turn]
        else:
            current = candidate

    if current:
        chunks.append(current)
    return chunks


def build_prompt(script: Script, turns: Iterable[Turn], style_prompt: str) -> str:
    dialogue = "\n".join(f"{turn.speaker}: {turn.text}" for turn in turns)
    return (
        "TTS the following Russian phone sales training conversation between Seller and Client.\n"
        "Do not read speaker labels aloud. Keep every line in Russian.\n"
        f"Scenario: {script.title}\n"
        f"Style: {style_prompt}\n\n"
        f"{dialogue}"
    )


def make_tts_config(args: argparse.Namespace) -> types.GenerateContentConfig:
    speech_kwargs: dict[str, object] = {
        "multi_speaker_voice_config": types.MultiSpeakerVoiceConfig(
            speaker_voice_configs=[
                types.SpeakerVoiceConfig(
                    speaker="Seller",
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=args.seller_voice,
                        )
                    ),
                ),
                types.SpeakerVoiceConfig(
                    speaker="Client",
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=args.client_voice,
                        )
                    ),
                ),
            ]
        )
    }
    if args.language_code:
        speech_kwargs["language_code"] = args.language_code

    return types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(**speech_kwargs),
    )


def create_client(args: argparse.Namespace) -> genai.Client:
    use_vertex = args.vertex or os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in {
        "1",
        "true",
        "yes",
    }
    if use_vertex:
        project = args.project or os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("VERTEX_PROJECT_ID")
        location = args.location or os.getenv("GOOGLE_CLOUD_REGION") or os.getenv("VERTEX_LOCATION") or "global"
        if not project:
            raise RuntimeError(
                "Vertex mode needs GOOGLE_CLOUD_PROJECT/VERTEX_PROJECT_ID or --project."
            )
        return genai.Client(vertexai=True, project=project, location=location)

    api_key = args.api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key and os.getenv("VERTEX_AI_API_KEY"):
        api_key = os.getenv("VERTEX_AI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Gemini API mode needs GEMINI_API_KEY/GOOGLE_API_KEY or --api-key. "
            "For ADC/Vertex use --vertex --project <project-id>."
        )
    return genai.Client(api_key=api_key)


def inline_data_to_bytes(data: object) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if isinstance(data, str):
        return base64.b64decode(data)
    raise TypeError(f"Unsupported inline audio data type: {type(data)!r}")


def extract_audio_part(response: object) -> bytes:
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) if content else None
        for part in parts or []:
            inline_data = getattr(part, "inline_data", None)
            data = getattr(inline_data, "data", None) if inline_data else None
            if data:
                return inline_data_to_bytes(data)
    return b""


@contextmanager
def request_timeout(seconds: int):
    if seconds <= 0 or threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(_signum, _frame):
        raise TimeoutError(f"Gemini TTS chunk timed out after {seconds}s")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    signal.signal(signal.SIGALRM, handler)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])


def synthesize_prompt(
    client: genai.Client,
    model: str,
    prompt: str,
    config: types.GenerateContentConfig,
    stream: bool,
    timeout_secs: int,
) -> bytes:
    with request_timeout(timeout_secs):
        if not stream:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=config,
            )
            pcm = extract_audio_part(response)
            if not pcm:
                raise RuntimeError("Gemini TTS returned no inline audio data.")
            return pcm

        pcm_parts: list[bytes] = []
        for chunk in client.models.generate_content_stream(
            model=model,
            contents=prompt,
            config=config,
        ):
            pcm = extract_audio_part(chunk)
            if pcm:
                pcm_parts.append(pcm)
                print(".", end="", flush=True)
        print("", flush=True)
        if not pcm_parts:
            raise RuntimeError("Gemini TTS stream returned no inline audio data.")
        return b"".join(pcm_parts)

    raise RuntimeError("unreachable")


def synthesize_prompt_with_retry(
    client: genai.Client,
    model: str,
    prompt: str,
    config: types.GenerateContentConfig,
    stream: bool,
    timeout_secs: int,
    retries: int,
) -> bytes:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return synthesize_prompt(
                client=client,
                model=model,
                prompt=prompt,
                config=config,
                stream=stream,
                timeout_secs=timeout_secs,
            )
        except Exception as exc:  # noqa: BLE001 - CLI should show provider errors.
            last_error = exc
            if attempt >= retries:
                break
            print(f"    retry {attempt + 1}/{retries}: {exc}", flush=True)
            time.sleep(1.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def silence_pcm(milliseconds: int) -> bytes:
    frames = int(SAMPLE_RATE_HZ * milliseconds / 1000)
    return b"\x00" * frames * SAMPLE_WIDTH_BYTES * CHANNELS


def write_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH_BYTES)
        wf.setframerate(SAMPLE_RATE_HZ)
        wf.writeframes(pcm)


def render_script(
    client: genai.Client,
    script: Script,
    args: argparse.Namespace,
    config: types.GenerateContentConfig,
) -> RenderedScript:
    chunks = split_turns_into_chunks(script, args.max_request_bytes, args.style_prompt)
    output_path = args.output_dir / f"script_{script.number:02d}.wav"
    if args.skip_existing and output_path.exists():
        pcm_bytes = max(output_path.stat().st_size - 44, 0)
        print(f"[script {script.number:02d}] skip existing {output_path}", flush=True)
        return RenderedScript(
            script=script,
            output_path=output_path,
            chunk_count=len(chunks),
            pcm_bytes=pcm_bytes,
            elapsed_secs=0.0,
        )

    pause = silence_pcm(args.chunk_pause_ms)
    pcm = bytearray()
    started = time.monotonic()

    print(
        f"[script {script.number:02d}] {script.title}: "
        f"{len(script.turns)} turns, {len(chunks)} chunk(s)",
        flush=True,
    )
    for index, turns in enumerate(chunks, 1):
        prompt = build_prompt(script, turns, args.style_prompt)
        print(
            f"[script {script.number:02d}] chunk {index}/{len(chunks)}: "
            f"{len(turns)} turns, {len(prompt.encode('utf-8'))} bytes",
            flush=True,
        )
        chunk_pcm = synthesize_prompt_with_retry(
            client=client,
            model=args.model,
            prompt=prompt,
            config=config,
            stream=args.stream,
            timeout_secs=args.chunk_timeout_secs,
            retries=args.retries,
        )
        pcm.extend(chunk_pcm)
        if index != len(chunks):
            pcm.extend(pause)

    write_wav(output_path, bytes(pcm))
    elapsed = time.monotonic() - started
    print(f"  wrote {output_path} ({len(pcm)} pcm bytes, {elapsed:.1f}s)", flush=True)
    return RenderedScript(
        script=script,
        output_path=output_path,
        chunk_count=len(chunks),
        pcm_bytes=len(pcm),
        elapsed_secs=elapsed,
    )


def render_script_worker(script: Script, args: argparse.Namespace) -> RenderedScript:
    client = create_client(args)
    config = make_tts_config(args)
    return render_script(client, script, args, config)


def parse_script_filter(values: list[str]) -> set[int] | None:
    if not values or values == ["all"]:
        return None
    selected: set[int] = set()
    for value in values:
        if value == "all":
            return None
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            selected.add(int(part))
    return selected


def write_manifest(output_dir: Path, rendered: list[RenderedScript], args: argparse.Namespace) -> None:
    manifest = {
        "model": args.model,
        "seller_voice": args.seller_voice,
        "client_voice": args.client_voice,
        "language_code": args.language_code,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "scripts": [
            {
                "number": item.script.number,
                "title": item.script.title,
                "path": str(item.output_path),
                "turns": len(item.script.turns),
                "chunks": item.chunk_count,
                "pcm_bytes": item.pcm_bytes,
                "elapsed_secs": round(item.elapsed_secs, 3),
            }
            for item in rendered
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--script", nargs="*", default=["all"], help="all, 1, 1,3,7")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--seller-voice", default="Kore")
    parser.add_argument("--client-voice", default="Puck")
    parser.add_argument("--language-code", default="ru-RU")
    parser.add_argument("--client-name", default="Алексей")
    parser.add_argument("--seller-name", default="Ирина")
    parser.add_argument("--max-request-bytes", type=int, default=DEFAULT_MAX_REQUEST_BYTES)
    parser.add_argument("--chunk-pause-ms", type=int, default=450)
    parser.add_argument(
        "--style-prompt",
        default=(
            "Seller is warm, calm, confident and concise. Client sounds like a real "
            "Russian entrepreneur on a phone call: natural, thoughtful, sometimes "
            "skeptical. No announcer voice, no theatrical overacting, medium pace."
        ),
    )
    parser.add_argument("--env-file", type=Path, action="append", default=[Path(".env"), Path(".env.iac")])
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--vertex", action="store_true")
    parser.add_argument("--project", default=None)
    parser.add_argument("--location", default=None)
    parser.add_argument("--stream", action="store_true", help="Use generate_content_stream.")
    parser.add_argument("--no-stream", dest="stream", action="store_false")
    parser.add_argument("--chunk-timeout-secs", type=int, default=180)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    args.output_dir = args.output_dir.resolve()

    for env_file in args.env_file:
        load_env_file(env_file)

    markdown = args.input.read_text(encoding="utf-8")
    scripts = extract_scripts(markdown, args.client_name, args.seller_name)
    selected = parse_script_filter(args.script)
    if selected is not None:
        scripts = [script for script in scripts if script.number in selected]

    if not scripts:
        print("No scripts matched selection.", file=sys.stderr)
        return 2

    if args.dry_run:
        for script in scripts:
            chunks = split_turns_into_chunks(script, args.max_request_bytes, args.style_prompt)
            print(f"[script {script.number:02d}] {script.title}")
            for index, turns in enumerate(chunks, 1):
                prompt = build_prompt(script, turns, args.style_prompt)
                print(
                    f"  chunk {index}/{len(chunks)}: "
                    f"{len(turns)} turns, {len(prompt.encode('utf-8'))} bytes"
                )
        return 0

    rendered: list[RenderedScript] = []
    errors: list[tuple[Script, Exception]] = []

    if args.concurrency <= 1:
        client = create_client(args)
        config = make_tts_config(args)
        for script in scripts:
            try:
                rendered.append(render_script(client, script, args, config))
            except Exception as exc:  # noqa: BLE001 - CLI should report provider errors.
                print(f"[script {script.number:02d}] failed: {exc}", file=sys.stderr, flush=True)
                errors.append((script, exc))
                break
    else:
        print(f"Rendering with concurrency={args.concurrency}", flush=True)
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            future_to_script = {
                executor.submit(render_script_worker, script, args): script for script in scripts
            }
            for future in as_completed(future_to_script):
                script = future_to_script[future]
                try:
                    rendered.append(future.result())
                except Exception as exc:  # noqa: BLE001 - CLI should report provider errors.
                    print(f"[script {script.number:02d}] failed: {exc}", file=sys.stderr, flush=True)
                    errors.append((script, exc))

    rendered.sort(key=lambda item: item.script.number)
    write_manifest(args.output_dir, rendered, args)
    print(f"Wrote manifest: {args.output_dir / 'manifest.json'}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
