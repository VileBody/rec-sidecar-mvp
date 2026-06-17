#!/usr/bin/env python3
"""Drive a scripted voice roleplay through the coach and Gemini TTS.

The harness keeps the client persona from a sales script, asks the local
FastAPI coach sidecar for the seller's next phrase, renders both voices with
Gemini TTS, and optionally plays each turn through the system audio output.
"""

from __future__ import annotations

import argparse
import json
import base64
import io
import os
import re
import subprocess
import ssl
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from google.genai import types as genai_types

from gemini_tts_sales_scripts import (
    CHANNELS,
    DEFAULT_INPUT,
    DEFAULT_MODEL,
    DEFAULT_OUTPUT_DIR,
    SAMPLE_RATE_HZ,
    SAMPLE_WIDTH_BYTES,
    Script,
    Turn,
    build_prompt,
    create_client,
    extract_scripts,
    load_env_file,
    make_tts_config,
    parse_script_filter,
    silence_pcm,
    synthesize_prompt_with_retry,
    write_wav,
)


DEFAULT_SERVICE_URL = "http://127.0.0.1:8088"
DEFAULT_RUST_UI_STATE_PATH = Path("logs/rec-sidecar.stage-ui.json")
DEFAULT_ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"
DEFAULT_ELEVENLABS_MODEL = "eleven_flash_v2_5"
DEFAULT_ELEVENLABS_OUTPUT_FORMAT = "pcm_24000"
DEFAULT_CLIENT_ACTOR_MODEL = "zai-glm-4.7"
DEFAULT_INWORLD_TTS_API_BASE = "https://api.inworld.ai"
DEFAULT_INWORLD_TTS_MODEL = "inworld-tts-1"
DEFAULT_INWORLD_SELLER_VOICE = "Elena"
DEFAULT_INWORLD_CLIENT_VOICE = "Arkady"
_TLS_CONTEXT: ssl.SSLContext | None = None


@dataclass
class RoleplayStep:
    index: int
    client_text: str | None
    seller_text: str
    stage: str | None
    readiness: str | None
    model: str | None
    audio_path: Path | None = None
    client_audio_path: Path | None = None
    seller_audio_path: Path | None = None
    rust_ui_sequence: int | None = None
    rust_ui_fresh: bool | None = None
    visible_label: str | None = None


def post_json(url: str, payload: dict[str, Any], token: str | None, timeout_secs: int) -> dict[str, Any] | None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_secs) as response:
            if response.status == 204:
                return None
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{url} returned HTTP {exc.code}: {body}") from exc


def tls_context() -> ssl.SSLContext:
    global _TLS_CONTEXT
    if _TLS_CONTEXT is not None:
        return _TLS_CONTEXT
    try:
        import certifi  # type: ignore

        _TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        _TLS_CONTEXT = ssl.create_default_context()
    return _TLS_CONTEXT


def strip_speakable_prefix(text: str) -> str:
    value = " ".join(text.strip().strip("\"'`").split())
    for prefix in (
        "Уточнить:",
        "Переход:",
        "Сказать:",
        "Скажите:",
        "Скажи:",
        "Спросить:",
    ):
        if value.startswith(prefix):
            value = value[len(prefix) :].strip()
            break
    value = re.sub(r"^(?:скажите|скажи|спросите|спроси|уточните|уточни)\s+клиент[ау]?:?\s*", "", value, flags=re.I)
    return value.strip()


def response_value_to_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        text = "".join(part for item in value if (part := response_value_to_text(item)))
        return text or None
    if isinstance(value, dict):
        return response_value_to_text(value.get("text")) or response_value_to_text(value.get("content"))
    return None


def chat_completion_text(value: dict[str, Any]) -> str:
    choices = value.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"empty chat completion response: {value}")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise RuntimeError(f"unexpected chat completion choice: {choice!r}")
    message = choice.get("message")
    if isinstance(message, dict):
        text = response_value_to_text(message.get("content"))
        if text and text.strip():
            return text.strip()
    text = response_value_to_text(choice.get("text") or choice.get("content"))
    if text and text.strip():
        return text.strip()
    raise RuntimeError(f"empty chat completion text: {value}")


def sanitize_actor_reply(text: str) -> str:
    value = " ".join(text.strip().strip("\"'`«»“”").split())
    value = re.sub(r"^(?:клиент|client|алексей)\s*[:：-]\s*", "", value, flags=re.I).strip()
    value = re.sub(r"^(?:ответ|реплика)\s*[:：-]\s*", "", value, flags=re.I).strip()
    value = re.sub(r"\s+", " ", value)
    return value.strip().strip("\"'`«»“”").strip()


def cerebras_text(
    *,
    args: argparse.Namespace,
    system_prompt: str,
    user_content: str,
    temperature: float,
    max_tokens: int,
) -> str:
    api_key = args.cerebras_api_key or os.getenv("CEREBRAS_API_KEY")
    if not api_key:
        raise RuntimeError("Cerebras actor needs CEREBRAS_API_KEY or --cerebras-api-key.")
    base = (args.cerebras_api_base or os.getenv("CEREBRAS_API_BASE") or "https://api.cerebras.ai/v1").rstrip("/")
    payload = {
        "model": args.client_actor_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    reasoning_effort = (args.cerebras_reasoning_effort or "").strip().lower()
    if reasoning_effort == "none" and str(args.client_actor_model).startswith("zai-"):
        payload["reasoning_effort"] = "none"
    elif reasoning_effort and reasoning_effort != "none":
        payload["reasoning_effort"] = reasoning_effort
    try:
        with httpx.Client(timeout=args.client_actor_timeout_secs) as client:
            response = client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Cerebras actor transport error: {exc}") from exc
    if not response.is_success:
        raise RuntimeError(f"Cerebras actor HTTP {response.status_code}: {response.text}")
    return chat_completion_text(response.json())


def gemini_actor_text(
    *,
    args: argparse.Namespace,
    system_prompt: str,
    user_content: str,
    temperature: float,
    max_tokens: int,
) -> str:
    client = create_client(args)
    response = client.models.generate_content(
        model=args.client_actor_model,
        contents=user_content,
        config=genai_types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=temperature,
            max_output_tokens=max_tokens,
        ),
    )
    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError(f"Gemini actor returned no text: {response!r}")
    return str(text)


def generate_text(
    *,
    args: argparse.Namespace,
    system_prompt: str,
    user_content: str,
    temperature: float,
    max_tokens: int,
) -> str:
    if args.client_actor_provider == "gemini":
        return gemini_actor_text(
            args=args,
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    return cerebras_text(
        args=args,
        system_prompt=system_prompt,
        user_content=user_content,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def actor_dialogue_tail(history: list[Turn], limit: int = 12) -> str:
    if not history:
        return "(диалог только начинается)"
    return "\n".join(f"{turn.speaker}: {turn.text}" for turn in history[-limit:])


def client_reference_arc(script: Script, limit: int = 10) -> str:
    replies = source_client_turns(script)[:limit]
    if not replies:
        return "(нет)"
    return "\n".join(f"- {reply}" for reply in replies)


def generate_client_actor_reply(
    *,
    script: Script,
    args: argparse.Namespace,
    history: list[Turn],
    seller_text: str,
    turn_index: int,
) -> str:
    persona_mode = args.client_persona_mode
    if persona_mode == "hostile":
        style_rules = (
            "Клиент неприятный, резкий и уставший от продажников. "
            "Он легко раздражается от шаблонов, перебивает логикой, требует конкретики, "
            "может звучать колко и недоверчиво, но остается реалистичным и не уходит в трэш."
        )
    elif persona_mode == "cold":
        style_rules = (
            "Клиент холодный и дистанцированный. "
            "Он отвечает коротко, не помогает продавцу, требует перейти к сути и не раскрывается быстро."
        )
    else:
        style_rules = (
            "Клиент живой и естественный: иногда короткий, иногда с сопротивлением, но по делу."
        )

    system_prompt = (
        "Ты играешь клиента в тренировочном B2C/high-check sales звонке на русском. "
        "Ты НЕ продавец и НЕ коуч. Отвечай только репликой клиента, без имени, без "
        "markdown и без пояснений. Клиент слышит последнюю реплику продавца и отвечает "
        f"естественно. {style_rules} "
        "Не соглашайся слишком быстро, если продавец не снял сомнения. "
        "Если продавец задал конкретный вопрос, ответь на него. "
        "Обычно 1 короткое предложение, максимум 2."
    )
    user_content = (
        "Продукт: glubina.core Казань, 7-10 июля 2026, 4 офлайн дня, 160 участников, "
        "19 менторов, группы по 8 предпринимателей одного уровня, живые разборы, "
        "цели на 90 дней, психологи, личная декларация, community, цена 99 000 руб.\n\n"
        f"Сценарий/персона: {script.title}.\n"
        f"Режим характера клиента: {persona_mode}.\n\n"
        "Референс дуги клиента, НЕ читай дословно, используй как характер/направление:\n"
        f"{client_reference_arc(script)}\n\n"
        f"История диалога:\n{actor_dialogue_tail(history)}\n\n"
        f"Последняя реплика продавца:\nSeller: {seller_text}\n\n"
        f"Сейчас ход клиента #{turn_index}. Дай следующую живую реплику клиента."
    )
    text = generate_text(
        args=args,
        system_prompt=system_prompt,
        user_content=user_content,
        temperature=args.client_actor_temperature,
        max_tokens=args.client_actor_max_tokens,
    )
    reply = sanitize_actor_reply(text)
    if not reply:
        raise RuntimeError("Client actor returned an empty reply.")
    return reply


def elevenlabs_voice_id(args: argparse.Namespace, speaker: str) -> str:
    if speaker == "Seller":
        value = args.eleven_seller_voice_id or os.getenv("ELEVENLABS_SELLER_VOICE_ID")
    else:
        value = args.eleven_client_voice_id or os.getenv("ELEVENLABS_CLIENT_VOICE_ID")
    if not value:
        raise RuntimeError(
            f"ElevenLabs {speaker} voice id is missing. Set "
            f"ELEVENLABS_{'SELLER' if speaker == 'Seller' else 'CLIENT'}_VOICE_ID "
            f"or pass --eleven-{'seller' if speaker == 'Seller' else 'client'}-voice-id."
        )
    return value


def synthesize_elevenlabs_text(args: argparse.Namespace, speaker: str, text: str) -> bytes:
    api_key = args.elevenlabs_api_key or os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ElevenLabs TTS needs ELEVENLABS_API_KEY or --elevenlabs-api-key.")
    if not args.eleven_output_format.startswith("pcm_"):
        raise RuntimeError("This harness expects ElevenLabs raw PCM output, e.g. pcm_24000.")

    base = (args.eleven_api_base or os.getenv("ELEVENLABS_API_BASE") or DEFAULT_ELEVENLABS_API_BASE).rstrip("/")
    voice_id = elevenlabs_voice_id(args, speaker)
    query = urllib.parse.urlencode({"output_format": args.eleven_output_format})
    payload: dict[str, Any] = {
        "text": text,
        "model_id": args.eleven_model,
        "voice_settings": {
            "stability": args.eleven_stability,
            "similarity_boost": args.eleven_similarity_boost,
            "use_speaker_boost": args.eleven_speaker_boost,
        },
    }
    if args.eleven_language_code:
        payload["language_code"] = args.eleven_language_code
    request = urllib.request.Request(
        f"{base}/text-to-speech/{voice_id}/stream?{query}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/octet-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=args.eleven_timeout_secs,
            context=tls_context(),
        ) as response:
            pcm = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ElevenLabs TTS HTTP {exc.code}: {body}") from exc
    if not pcm:
        raise RuntimeError("ElevenLabs TTS returned empty audio.")
    return pcm


def wav_bytes_to_pcm(audio: bytes) -> bytes:
    if not audio.startswith(b"RIFF"):
        return audio
    with wave.open(io.BytesIO(audio), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        if channels != CHANNELS or width != SAMPLE_WIDTH_BYTES or rate != SAMPLE_RATE_HZ:
            raise RuntimeError(
                "Inworld TTS returned unsupported WAV format: "
                f"{rate}Hz, {channels}ch, {width} bytes/sample"
            )
        return wf.readframes(wf.getnframes())


def inworld_voice_id(args: argparse.Namespace, speaker: str) -> str:
    if speaker == "Seller":
        return args.inworld_seller_voice or os.getenv("INWORLD_TTS_SELLER_VOICE") or DEFAULT_INWORLD_SELLER_VOICE
    return args.inworld_client_voice or os.getenv("INWORLD_TTS_CLIENT_VOICE") or DEFAULT_INWORLD_CLIENT_VOICE


def synthesize_inworld_text(args: argparse.Namespace, speaker: str, text: str) -> bytes:
    api_key = args.inworld_api_key or os.getenv("INWORLD_API_KEY")
    if not api_key:
        raise RuntimeError("Inworld TTS needs INWORLD_API_KEY or --inworld-api-key.")
    base = (args.inworld_tts_api_base or os.getenv("INWORLD_TTS_API_BASE") or DEFAULT_INWORLD_TTS_API_BASE).rstrip("/")
    payload = {
        "text": text,
        "voiceId": inworld_voice_id(args, speaker),
        "modelId": args.inworld_tts_model,
        "audioConfig": {
            "audioEncoding": "LINEAR16",
            "sampleRateHertz": SAMPLE_RATE_HZ,
            "language": args.inworld_language,
        },
    }
    request = urllib.request.Request(
        f"{base}/tts/v1/voice",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Basic {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=args.inworld_tts_timeout_secs,
            context=tls_context(),
        ) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Inworld TTS HTTP {exc.code}: {body}") from exc
    audio_content = value.get("audioContent") or value.get("audio_content")
    if not isinstance(audio_content, str) or not audio_content:
        raise RuntimeError(f"Inworld TTS returned no audioContent: {value}")
    return wav_bytes_to_pcm(base64.b64decode(audio_content))


def stage_next_phrase(
    *,
    service_url: str,
    token: str | None,
    run_id: str,
    context: str,
    current_stage: str | None,
    timeout_secs: int,
) -> tuple[str, str | None, str | None, str | None]:
    response = post_json(
        f"{service_url.rstrip('/')}/v1/coach/stage",
        {
            "run_id": run_id,
            "context": context,
            "current_stage": current_stage,
        },
        token,
        timeout_secs,
    )
    if response is None:
        return "", current_stage, None, None

    scorecard = response.get("scorecard") or {}
    phrase = scorecard.get("next_action") or response.get("step") or ""
    return (
        strip_speakable_prefix(str(phrase)),
        response.get("stage"),
        scorecard.get("readiness"),
        response.get("model"),
    )


def read_rust_ui_state(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None


def rust_ui_sequence(state: dict[str, Any] | None) -> int:
    if not state:
        return -1
    try:
        return int(state.get("sequence", -1))
    except (TypeError, ValueError):
        return -1


def rust_ui_phrase(state: dict[str, Any] | None) -> str:
    if not state:
        return ""
    scorecard = state.get("scorecard") or {}
    phrase = state.get("speakable_next_action") or scorecard.get("next_action") or ""
    return strip_speakable_prefix(str(phrase))


def wait_for_rust_ui_phrase(
    *,
    path: Path,
    min_sequence: int | None,
    timeout_secs: float,
    poll_ms: int,
) -> tuple[dict[str, Any], str, bool]:
    deadline = time.monotonic() + timeout_secs
    latest: dict[str, Any] | None = None
    poll_secs = max(poll_ms, 50) / 1000

    while time.monotonic() < deadline:
        state = read_rust_ui_state(path)
        phrase = rust_ui_phrase(state)
        if state:
            latest = state
        if state and phrase and (min_sequence is None or rust_ui_sequence(state) > min_sequence):
            return state, phrase, True
        time.sleep(poll_secs)

    if latest:
        phrase = rust_ui_phrase(latest)
        if phrase:
            return latest, phrase, False
    raise TimeoutError(f"Rust UI state did not produce a speakable phrase at {path}")


def source_seller_turns(script: Script) -> list[str]:
    return [turn.text for turn in script.turns if turn.speaker == "Seller"]


def source_client_turns(script: Script) -> list[str]:
    return [turn.text for turn in script.turns if turn.speaker == "Client"]


def select_script(scripts: list[Script], selected: set[int] | None) -> Script:
    if selected:
        for script in scripts:
            if script.number in selected:
                return script
    return scripts[0]


def append_context(context_lines: list[str], speaker: str, text: str) -> None:
    context_lines.append(f"{speaker}: {text}")


def render_step_audio(
    *,
    client: Any,
    args: argparse.Namespace,
    config: Any,
    script: Script,
    step: RoleplayStep,
) -> bytes:
    if args.tts_provider == "inworld":
        parts: list[bytes] = []
        if step.client_text:
            parts.append(synthesize_inworld_text(args, "Client", step.client_text))
            parts.append(silence_pcm(max(args.turn_pause_ms // 2, 100)))
        parts.append(synthesize_inworld_text(args, "Seller", step.seller_text))
        return b"".join(parts)
    if args.tts_provider == "elevenlabs":
        parts: list[bytes] = []
        if step.client_text:
            parts.append(synthesize_elevenlabs_text(args, "Client", step.client_text))
            parts.append(silence_pcm(max(args.turn_pause_ms // 2, 100)))
        parts.append(synthesize_elevenlabs_text(args, "Seller", step.seller_text))
        return b"".join(parts)

    turns: list[Turn] = []
    if step.client_text:
        turns.append(Turn("Client", step.client_text))
    turns.append(Turn("Seller", step.seller_text))
    prompt = build_prompt(
        Script(script.number, f"{script.title} / roleplay step {step.index}", turns),
        turns,
        args.style_prompt,
    )
    return synthesize_prompt_with_retry(
        client=client,
        model=args.model,
        prompt=prompt,
        config=config,
        stream=args.stream,
        timeout_secs=args.chunk_timeout_secs,
        retries=args.retries,
    )


def render_single_turn_audio(
    *,
    client: Any,
    args: argparse.Namespace,
    config: Any,
    script: Script,
    index: int,
    speaker: str,
    text: str,
) -> bytes:
    if args.tts_provider == "inworld":
        return synthesize_inworld_text(args, speaker, text)
    if args.tts_provider == "elevenlabs":
        return synthesize_elevenlabs_text(args, speaker, text)

    prompt = build_prompt(
        Script(script.number, f"{script.title} / roleplay turn {index}", [Turn(speaker, text)]),
        [Turn(speaker, text)],
        args.style_prompt,
    )
    return synthesize_prompt_with_retry(
        client=client,
        model=args.model,
        prompt=prompt,
        config=config,
        stream=args.stream,
        timeout_secs=args.chunk_timeout_secs,
        retries=args.retries,
    )


def play_audio(path: Path) -> None:
    subprocess.run(["afplay", str(path)], check=False)


def fill_vertex_project_from_gcloud(args: argparse.Namespace) -> None:
    if not args.vertex or args.project:
        return
    if os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("VERTEX_PROJECT_ID"):
        return
    try:
        result = subprocess.run(
            ["gcloud", "config", "get-value", "project"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return
    project = result.stdout.strip()
    if result.returncode == 0 and project and project != "(unset)":
        args.project = project


def run_roleplay(script: Script, args: argparse.Namespace) -> list[RoleplayStep]:
    context_lines: list[str] = []
    history: list[Turn] = []
    steps: list[RoleplayStep] = []
    current_stage: str | None = None
    seller_source = source_seller_turns(script)
    client_source = source_client_turns(script)
    seller_cursor = 0

    def seller_phrase() -> tuple[str, str | None, str | None, str | None]:
        nonlocal seller_cursor, current_stage
        if args.coach_source == "source":
            phrase = seller_source[min(seller_cursor, len(seller_source) - 1)]
            seller_cursor += 1
            return phrase, current_stage, None, "source-script"
        phrase, stage, readiness, model = stage_next_phrase(
            service_url=args.llm_service_url,
            token=args.service_token,
            run_id=args.run_id,
            context="\n".join(context_lines),
            current_stage=current_stage,
            timeout_secs=args.service_timeout_secs,
        )
        if stage:
            current_stage = stage
        return phrase or seller_source[min(seller_cursor, len(seller_source) - 1)], current_stage, readiness, model

    if not args.seed_source_seller:
        phrase, stage, readiness, model = seller_phrase()
        append_context(context_lines, "Продавец", phrase)
        history.append(Turn("Seller", phrase))
        steps.append(
            RoleplayStep(
                index=1,
                client_text=None,
                seller_text=phrase,
                stage=stage,
                readiness=readiness,
                model=model,
            )
        )
    elif seller_source:
        phrase = seller_source[0]
        seller_cursor = 1
        append_context(context_lines, "Продавец", phrase)
        history.append(Turn("Seller", phrase))
        steps.append(
            RoleplayStep(
                index=1,
                client_text=None,
                seller_text=phrase,
                stage=current_stage,
                readiness=None,
                model="source-script",
            )
        )

    max_client_turns = min(args.max_client_turns, len(client_source))
    for turn_index in range(1, max_client_turns + 1):
        if args.client_source == "actor":
            last_seller_text = history[-1].text if history else ""
            client_text = generate_client_actor_reply(
                script=script,
                args=args,
                history=history,
                seller_text=last_seller_text,
                turn_index=turn_index,
            )
        else:
            client_text = client_source[turn_index - 1]
        append_context(context_lines, "Клиент", client_text)
        history.append(Turn("Client", client_text))
        phrase, stage, readiness, model = seller_phrase()
        append_context(context_lines, "Продавец", phrase)
        history.append(Turn("Seller", phrase))
        steps.append(
            RoleplayStep(
                index=len(steps) + 1,
                client_text=client_text,
                seller_text=phrase,
                stage=stage,
                readiness=readiness,
                model=model,
            )
        )

    return steps


def run_roleplay_from_rust_ui(script: Script, args: argparse.Namespace) -> list[RoleplayStep]:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    client_source = source_client_turns(script)
    seller_source = source_seller_turns(script)
    steps: list[RoleplayStep] = []
    history: list[Turn] = []
    state_path = args.rust_ui_state_path
    state = read_rust_ui_state(state_path)
    last_sequence = rust_ui_sequence(state)
    full_pcm = bytearray()
    pause = silence_pcm(args.turn_pause_ms)

    tts_client = None if args.dry_run or args.tts_provider != "gemini" else create_client(args)
    config = None if args.dry_run or args.tts_provider != "gemini" else make_tts_config(args)

    def state_metadata(
        state: dict[str, Any] | None,
    ) -> tuple[str | None, str | None, str | None, str | None]:
        if not state:
            return None, None, None, None
        scorecard = state.get("scorecard") or {}
        return (
            state.get("stage"),
            scorecard.get("readiness_label") or scorecard.get("readiness"),
            state.get("model"),
            state.get("visible_label"),
        )

    def render_and_maybe_play(index: int, speaker: str, text: str, suffix: str) -> Path | None:
        if args.dry_run:
            return None
        if speaker == "Seller" and args.skip_seller_audio:
            return None
        if args.tts_provider == "gemini":
            assert tts_client is not None
            assert config is not None
        pcm = render_single_turn_audio(
            client=tts_client,
            args=args,
            config=config,
            script=script,
            index=index,
            speaker=speaker,
            text=text,
        )
        path = args.output_dir / f"roleplay_script_{script.number:02d}_turn_{index:03d}_{suffix}.wav"
        write_wav(path, pcm)
        full_pcm.extend(pcm)
        full_pcm.extend(pause)
        print(f"  wrote {path}", flush=True)
        if args.play:
            play_audio(path)
        return path

    print(f"[roleplay] script {script.number}: {script.title}", flush=True)
    print(f"[roleplay] coach source: rust-ui state={state_path}", flush=True)

    if args.seed_source_seller and seller_source:
        seller_text = seller_source[0]
        stage, readiness, model, visible_label = state_metadata(state)
        step = RoleplayStep(
            index=1,
            client_text=None,
            seller_text=seller_text,
            stage=stage,
            readiness=readiness,
            model=model or "source-script",
            rust_ui_sequence=rust_ui_sequence(state),
            rust_ui_fresh=None,
            visible_label=visible_label,
        )
        print(f"Seller[{step.stage or '-'} {step.readiness or '-'} {step.model or '-'}]: {seller_text}", flush=True)
        step.seller_audio_path = render_and_maybe_play(step.index, "Seller", seller_text, "seller")
        step.audio_path = step.seller_audio_path
        steps.append(step)
        history.append(Turn("Seller", seller_text))
    else:
        state, seller_text, fresh = wait_for_rust_ui_phrase(
            path=state_path,
            min_sequence=None,
            timeout_secs=args.rust_ui_wait_secs,
            poll_ms=args.rust_ui_poll_ms,
        )
        last_sequence = rust_ui_sequence(state)
        stage, readiness, model, visible_label = state_metadata(state)
        step = RoleplayStep(
            index=1,
            client_text=None,
            seller_text=seller_text,
            stage=stage,
            readiness=readiness,
            model=model or ("rust-ui" if fresh else "rust-ui-current"),
            rust_ui_sequence=rust_ui_sequence(state),
            rust_ui_fresh=fresh,
            visible_label=visible_label,
        )
        print(f"Seller[{step.stage or '-'} {step.readiness or '-'} {step.model or '-'}]: {seller_text}", flush=True)
        step.seller_audio_path = render_and_maybe_play(step.index, "Seller", seller_text, "seller")
        step.audio_path = step.seller_audio_path
        steps.append(step)
        history.append(Turn("Seller", seller_text))

    max_client_turns = min(args.max_client_turns, len(client_source))
    for turn_index in range(1, max_client_turns + 1):
        step_index = len(steps) + 1
        if args.client_source == "actor":
            client_text = generate_client_actor_reply(
                script=script,
                args=args,
                history=history,
                seller_text=history[-1].text if history else "",
                turn_index=turn_index,
            )
        else:
            client_text = client_source[turn_index - 1]
        print(f"\nClient: {client_text}", flush=True)
        client_audio_path = render_and_maybe_play(step_index, "Client", client_text, "client")
        history.append(Turn("Client", client_text))

        state, seller_text, fresh = wait_for_rust_ui_phrase(
            path=state_path,
            min_sequence=last_sequence,
            timeout_secs=args.rust_ui_wait_secs,
            poll_ms=args.rust_ui_poll_ms,
        )
        last_sequence = max(last_sequence, rust_ui_sequence(state))
        stage, readiness, model, visible_label = state_metadata(state)
        step = RoleplayStep(
            index=step_index,
            client_text=client_text,
            seller_text=seller_text,
            stage=stage,
            readiness=readiness,
            model=model or ("rust-ui" if fresh else "rust-ui-current"),
            client_audio_path=client_audio_path,
            rust_ui_sequence=rust_ui_sequence(state),
            rust_ui_fresh=fresh,
            visible_label=visible_label,
        )
        print(f"Seller[{step.stage or '-'} {step.readiness or '-'} {step.model or '-'}]: {seller_text}", flush=True)
        step.seller_audio_path = render_and_maybe_play(step.index, "Seller", seller_text, "seller")
        step.audio_path = step.seller_audio_path
        steps.append(step)
        history.append(Turn("Seller", seller_text))

    if not args.dry_run:
        write_wav(args.output_dir / f"roleplay_script_{script.number:02d}_full.wav", bytes(full_pcm))

    return steps


def write_roleplay_manifest(script: Script, steps: list[RoleplayStep], output_dir: Path, args: argparse.Namespace) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "script": {"number": script.number, "title": script.title},
        "source": str(args.input),
        "event_url": "https://glubina-community.ru/kazan",
        "run_id": args.run_id,
        "coach_source": args.coach_source,
        "client_source": args.client_source,
        "tts_provider": args.tts_provider,
        "client_actor_provider": args.client_actor_provider,
        "client_actor_model": args.client_actor_model,
        "model": args.model,
        "seller_voice": args.seller_voice,
        "client_voice": args.client_voice,
        "eleven_model": args.eleven_model,
        "eleven_output_format": args.eleven_output_format,
        "inworld_tts_model": args.inworld_tts_model,
        "inworld_seller_voice": args.inworld_seller_voice,
        "inworld_client_voice": args.inworld_client_voice,
        "steps": [
            {
                "index": step.index,
                "client_text": step.client_text,
                "seller_text": step.seller_text,
                "stage": step.stage,
                "readiness": step.readiness,
                "model": step.model,
                "audio_path": str(step.audio_path) if step.audio_path else None,
                "client_audio_path": str(step.client_audio_path) if step.client_audio_path else None,
                "seller_audio_path": str(step.seller_audio_path) if step.seller_audio_path else None,
                "rust_ui_sequence": step.rust_ui_sequence,
                "rust_ui_fresh": step.rust_ui_fresh,
                "visible_label": step.visible_label,
            }
            for step in steps
        ],
    }
    (output_dir / "roleplay_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--script", nargs="*", default=["1"], help="script number, e.g. 1")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR / "roleplay")
    parser.add_argument("--run-id", default=f"roleplay-{int(time.time())}")
    parser.add_argument("--llm-service-url", default=os.getenv("COACH_LLM_SERVICE_URL", DEFAULT_SERVICE_URL))
    parser.add_argument("--service-token", default=os.getenv("COACH_LLM_SERVICE_TOKEN"))
    parser.add_argument("--service-timeout-secs", type=int, default=45)
    parser.add_argument("--coach-source", choices=["stage", "source", "rust-ui"], default="stage")
    parser.add_argument("--client-source", choices=["script", "actor"], default="script")
    parser.add_argument("--client-actor-provider", choices=["cerebras", "gemini"], default="cerebras")
    parser.add_argument("--client-actor-model", default=os.getenv("CEREBRAS_MODEL", DEFAULT_CLIENT_ACTOR_MODEL))
    parser.add_argument("--client-persona-mode", choices=["neutral", "cold", "hostile"], default="hostile")
    parser.add_argument("--client-actor-temperature", type=float, default=0.85)
    parser.add_argument("--client-actor-max-tokens", type=int, default=256)
    parser.add_argument("--client-actor-timeout-secs", type=int, default=30)
    parser.add_argument("--cerebras-api-key", default=None)
    parser.add_argument("--cerebras-api-base", default=None)
    parser.add_argument("--cerebras-reasoning-effort", default=os.getenv("CEREBRAS_REASONING_EFFORT", "none"))
    parser.add_argument("--seed-source-seller", action="store_true")
    parser.add_argument(
        "--rust-ui-state-path",
        type=Path,
        default=Path(os.getenv("REC_STAGE_UI_STATE_PATH", DEFAULT_RUST_UI_STATE_PATH)),
    )
    parser.add_argument("--rust-ui-wait-secs", type=float, default=20.0)
    parser.add_argument("--rust-ui-poll-ms", type=int, default=200)
    parser.add_argument("--max-client-turns", type=int, default=8)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--tts-provider", choices=["gemini", "elevenlabs", "inworld"], default="gemini")
    parser.add_argument("--seller-voice", default="Kore")
    parser.add_argument("--client-voice", default="Puck")
    parser.add_argument("--language-code", default="ru-RU")
    parser.add_argument("--elevenlabs-api-key", default=None)
    parser.add_argument("--eleven-api-base", default=None)
    parser.add_argument("--eleven-model", default=os.getenv("ELEVENLABS_MODEL", DEFAULT_ELEVENLABS_MODEL))
    parser.add_argument("--eleven-output-format", default=os.getenv("ELEVENLABS_OUTPUT_FORMAT", DEFAULT_ELEVENLABS_OUTPUT_FORMAT))
    parser.add_argument("--eleven-seller-voice-id", default=None)
    parser.add_argument("--eleven-client-voice-id", default=None)
    parser.add_argument("--eleven-language-code", default=os.getenv("ELEVENLABS_LANGUAGE_CODE", "ru"))
    parser.add_argument("--eleven-stability", type=float, default=0.5)
    parser.add_argument("--eleven-similarity-boost", type=float, default=0.75)
    parser.add_argument("--eleven-speaker-boost", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--eleven-timeout-secs", type=int, default=45)
    parser.add_argument("--inworld-api-key", default=None)
    parser.add_argument("--inworld-tts-api-base", default=None)
    parser.add_argument("--inworld-tts-model", default=os.getenv("INWORLD_TTS_MODEL", DEFAULT_INWORLD_TTS_MODEL))
    parser.add_argument("--inworld-seller-voice", default=os.getenv("INWORLD_TTS_SELLER_VOICE", DEFAULT_INWORLD_SELLER_VOICE))
    parser.add_argument("--inworld-client-voice", default=os.getenv("INWORLD_TTS_CLIENT_VOICE", DEFAULT_INWORLD_CLIENT_VOICE))
    parser.add_argument("--inworld-language", default=os.getenv("INWORLD_TTS_LANGUAGE", "ru-RU"))
    parser.add_argument("--inworld-tts-timeout-secs", type=int, default=45)
    parser.add_argument("--style-prompt", default=(
        "Seller is warm, calm, confident and concise. Client sounds like a real "
        "Russian entrepreneur on a phone call: natural, skeptical, not theatrical. "
        "Do not read labels aloud."
    ))
    parser.add_argument("--env-file", type=Path, action="append", default=[Path(".env"), Path(".env.iac")])
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--vertex", action="store_true")
    parser.add_argument("--project", default=None)
    parser.add_argument("--location", default=None)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--no-stream", dest="stream", action="store_false")
    parser.set_defaults(stream=False)
    parser.add_argument("--chunk-timeout-secs", type=int, default=180)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--turn-pause-ms", type=int, default=650)
    parser.add_argument("--skip-seller-audio", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--play", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    args.output_dir = args.output_dir.resolve()
    for env_file in args.env_file:
        load_env_file(env_file)
    fill_vertex_project_from_gcloud(args)

    scripts = extract_scripts(
        args.input.read_text(encoding="utf-8"),
        client_name="Алексей",
        seller_name="Ирина",
    )
    selected = parse_script_filter(args.script)
    script = select_script(scripts, selected)
    if args.coach_source == "rust-ui":
        steps = run_roleplay_from_rust_ui(script, args)
        write_roleplay_manifest(script, steps, args.output_dir, args)
        print(f"Wrote manifest: {args.output_dir / 'roleplay_manifest.json'}", flush=True)
        return 0

    steps = run_roleplay(script, args)

    print(f"[roleplay] script {script.number}: {script.title}", flush=True)
    for step in steps:
        if step.client_text:
            print(f"\nClient: {step.client_text}", flush=True)
        print(
            f"Seller[{step.stage or '-'} {step.readiness or '-'} {step.model or '-'}]: "
            f"{step.seller_text}",
            flush=True,
        )

    if not args.dry_run:
        client = None if args.tts_provider != "gemini" else create_client(args)
        config = None if args.tts_provider != "gemini" else make_tts_config(args)
        full_pcm = bytearray()
        pause = silence_pcm(args.turn_pause_ms)
        for step in steps:
            pcm = render_step_audio(
                client=client,
                args=args,
                config=config,
                script=script,
                step=step,
            )
            path = args.output_dir / f"roleplay_script_{script.number:02d}_turn_{step.index:03d}.wav"
            write_wav(path, pcm)
            step.audio_path = path
            full_pcm.extend(pcm)
            full_pcm.extend(pause)
            print(f"  wrote {path}", flush=True)
            if args.play:
                play_audio(path)
        write_wav(args.output_dir / f"roleplay_script_{script.number:02d}_full.wav", bytes(full_pcm))

    write_roleplay_manifest(script, steps, args.output_dir, args)
    print(f"Wrote manifest: {args.output_dir / 'roleplay_manifest.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
