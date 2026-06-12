from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROMPT_ASSETS_DIR = Path(__file__).with_name("prompt_assets")
DETECT_PROMPT_PATH = PROMPT_ASSETS_DIR / "1_detect_where_we_are.md"
AGENDA_PROMPT_PATH = PROMPT_ASSETS_DIR / "3_current_stage_agenda.md"

STAGE_RE = re.compile(r"^(S\d+\.\d+[a-z]?)(?:\s+—\s+(.+))?$", re.IGNORECASE)
FIELD_RE = re.compile(r"^-\s+(Agenda|Эмоц\. реакция|Шаг):\s*(.+)$")


@dataclass(frozen=True)
class StageAgenda:
    stage: str
    title: str
    agenda: str
    emotion: str
    step: str


def read_prompt_asset(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_stage_agenda(markdown: str) -> dict[str, StageAgenda]:
    mapping: dict[str, dict[str, str]] = {}
    current_stage: str | None = None

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        stage_match = STAGE_RE.match(line)
        if stage_match:
            current_stage = normalize_stage(stage_match.group(1))
            mapping[current_stage] = {
                "stage": current_stage,
                "title": stage_match.group(2) or current_stage,
                "agenda": "",
                "emotion": "",
                "step": "",
            }
            continue

        field_match = FIELD_RE.match(line)
        if not field_match or current_stage is None:
            continue

        field_name = field_match.group(1)
        value = strip_outer_quotes(field_match.group(2).strip())
        if field_name == "Agenda":
            mapping[current_stage]["agenda"] = value
        elif field_name == "Эмоц. реакция":
            mapping[current_stage]["emotion"] = value
        elif field_name == "Шаг":
            mapping[current_stage]["step"] = value

    return {
        stage: StageAgenda(
            stage=values["stage"],
            title=values["title"],
            agenda=values["agenda"],
            emotion=values["emotion"],
            step=values["step"],
        )
        for stage, values in mapping.items()
        if values["agenda"] and values["emotion"] and values["step"]
    }


def stage_detection_system_prompt() -> str:
    stages = ", ".join(KNOWN_STAGES)
    return (
        f"{DETECT_WHERE_WE_ARE_PROMPT}\n\n"
        "Верни строго JSON без Markdown и без пояснений.\n"
        f"Допустимые stage: {stages}.\n"
        'Формат: {"stage":"S2.3","confidence":0.7}\n'
        "Если данных мало, выбери ближайший stage по текущему моменту разговора."
    )


def parse_stage_detection(text: str) -> tuple[str, float | None]:
    value = extract_json_object(text)
    if isinstance(value, dict):
        stage = normalize_stage(str(value.get("stage", "")))
        confidence = value.get("confidence")
        if stage in STAGE_AGENDA_BY_TAG:
            return stage, confidence if isinstance(confidence, (int, float)) else None

    stage = normalize_stage(text)
    if stage in STAGE_AGENDA_BY_TAG:
        return stage, None

    match = re.search(r"S\d+\.\d+[a-z]?", text, re.IGNORECASE)
    if match:
        stage = normalize_stage(match.group(0))
        if stage in STAGE_AGENDA_BY_TAG:
            return stage, None

    raise ValueError(f"unknown stage detection response: {text!r}")


def extract_json_object(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    try:
        return json.loads(stripped)
    except ValueError:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and start < end:
        return json.loads(stripped[start : end + 1])
    return None


def normalize_stage(value: str) -> str:
    match = re.search(r"S(\d+)\.(\d+)([a-z]?)", value.strip(), re.IGNORECASE)
    if not match:
        return ""
    suffix = match.group(3).lower()
    return f"S{match.group(1)}.{match.group(2)}{suffix}"


def strip_outer_quotes(value: str) -> str:
    return value.strip().strip("\"'«»“”").strip()


DETECT_WHERE_WE_ARE_PROMPT = read_prompt_asset(DETECT_PROMPT_PATH)
CURRENT_STAGE_AGENDA_PROMPT = read_prompt_asset(AGENDA_PROMPT_PATH)
STAGE_AGENDA_BY_TAG = parse_stage_agenda(CURRENT_STAGE_AGENDA_PROMPT)
KNOWN_STAGES = tuple(STAGE_AGENDA_BY_TAG.keys())
