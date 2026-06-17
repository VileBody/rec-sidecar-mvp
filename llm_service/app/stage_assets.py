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
CONTRACT_FIELD_RE = re.compile(r"^([A-Za-z_][\w.]*)\s*(?::|-\s)\s*(.+?)\s*$")
STAGE_BLOCK_START = "<<<STAGE>>>"
STAGE_BLOCK_END = "<<<END_STAGE>>>"


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
        "Верни только короткий текстовый блок без JSON, Markdown и пояснений.\n"
        f"Допустимые stage: {stages}.\n"
        f"Формат:\n{STAGE_BLOCK_START}\nstage: S2.3\nconfidence: 0.70\n{STAGE_BLOCK_END}\n"
        "Пиши `stage:` и `confidence:` каждый с новой строки.\n"
        "Если данных мало, выбери ближайший stage по текущему моменту разговора."
    )


def parse_stage_detection(text: str) -> tuple[str, float | None]:
    contract_stage = extract_stage_contract(text)
    if contract_stage is not None:
        return contract_stage

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

    inferred = infer_stage_from_text(text)
    if inferred:
        return inferred, None

    raise ValueError(f"unknown stage detection response: {text!r}")


def extract_stage_contract(text: str) -> tuple[str, float | None] | None:
    block = extract_contract_block(text, STAGE_BLOCK_START, STAGE_BLOCK_END)
    candidate = block if block is not None else text.strip()
    if not candidate:
        return None

    stage_value: str | None = None
    confidence_value: float | None = None
    saw_stage_field = False

    for raw_line in candidate.splitlines():
        line = raw_line.strip().strip("`")
        if not line:
            continue
        match = CONTRACT_FIELD_RE.match(line)
        if not match:
            continue
        key = match.group(1).lower()
        value = match.group(2).strip()
        if key == "stage":
            saw_stage_field = True
            normalized = normalize_stage(value)
            if normalized in STAGE_AGENDA_BY_TAG:
                stage_value = normalized
        elif key == "confidence":
            confidence_value = parse_optional_float(value)

    if saw_stage_field and stage_value:
        return stage_value, confidence_value
    return None


def infer_stage_from_text(text: str) -> str | None:
    normalized = text.lower().replace("ё", "е")
    normalized = re.sub(r"\b(bos|eos)\b", " ", normalized)

    hints: tuple[tuple[str, tuple[str, ...]], ...] = (
        (
            "S3.5",
            (
                "follow-up",
                "потеряш",
                "следующего контакта",
                "причину отказа",
            ),
        ),
        (
            "S3.4b",
            (
                "второй созвон",
                "second zoom",
                "запланировать второй",
                "вышлю договор",
            ),
        ),
        (
            "S3.4a",
            (
                "цена или ценность",
                "истинную причину возражения",
                "обjection clarifier",
                "надо подумать",
            ),
        ),
        (
            "S3.3",
            (
                "вариант через банк",
                "денег нет",
                "платеж 15к",
                "downsell",
            ),
        ),
        (
            "S3.2",
            (
                "видит ли клиент ценность",
                "value test",
                "понятна ценность",
                "проверить ценность",
            ),
        ),
        (
            "S3.1",
            (
                "pitch",
                "оффер",
                "презентаци",
                "рассказать про продукт",
                "500 000",
            ),
        ),
        (
            "S2.5",
            (
                "разрешение на переход",
                "переход к офферу",
                "pivot",
                "давай расскажу",
            ),
        ),
        (
            "S2.4",
            (
                "истинный мотив",
                "зачем клиент",
                "что зацепило",
                "уровень доверия",
            ),
        ),
        (
            "S2.3",
            (
                "target",
                "gap",
                "точка б",
                "желаемый результат",
                "цель",
                "разрыв",
            ),
        ),
        (
            "S2.2",
            (
                "сбор текущей реальности",
                "сбору текущей реальности",
                "текущая реальность",
                "текущей ситуации",
                "текущую ситуацию",
                "сбор фактов",
                "операционн",
                "выявило основную боль",
                "нет времени на развитие",
            ),
        ),
        (
            "S2.1",
            (
                "фрейм",
                "рамк",
                "custdev",
                "касдев",
                "правила звонка",
            ),
        ),
    )
    for stage, markers in hints:
        if any(marker in normalized for marker in markers):
            return stage
    return None


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


def extract_contract_block(text: str, start_marker: str, end_marker: str) -> str | None:
    start = text.find(start_marker)
    if start == -1:
        return None
    start += len(start_marker)
    end = text.find(end_marker, start)
    block = text[start:end] if end != -1 else text[start:]
    return block.strip()


def parse_optional_float(value: str) -> float | None:
    normalized = value.strip().replace(",", ".").rstrip(".;)")
    try:
        return float(normalized)
    except ValueError:
        return None


def normalize_stage(value: str) -> str:
    match = re.search(r"S(\d+)\.(\d+)([a-z]?)", value.strip(), re.IGNORECASE)
    if not match:
        return ""
    suffix = match.group(3).lower()
    return f"S{match.group(1)}.{match.group(2)}{suffix}"


STAGE_PROGRESS_ORDER = (
    "S2.1",
    "S2.2",
    "S2.3",
    "S2.4",
    "S2.5",
    "S3.1",
    "S3.2",
    "S3.3",
    "S3.4a",
    "S3.4b",
    "S3.5",
)


def stage_progress_rank(stage: str | None) -> int | None:
    normalized = normalize_stage(stage or "")
    try:
        return STAGE_PROGRESS_ORDER.index(normalized)
    except ValueError:
        return None


def stage_is_backward(current_stage: str | None, proposed_stage: str | None) -> bool:
    current_rank = stage_progress_rank(current_stage)
    proposed_rank = stage_progress_rank(proposed_stage)
    return current_rank is not None and proposed_rank is not None and proposed_rank < current_rank


def clamp_stage_forward(current_stage: str | None, proposed_stage: str) -> str:
    current = normalize_stage(current_stage or "")
    proposed = normalize_stage(proposed_stage)
    if current and stage_is_backward(current, proposed):
        return current
    return proposed


def strip_outer_quotes(value: str) -> str:
    return value.strip().strip("\"'«»“”").strip()


DETECT_WHERE_WE_ARE_PROMPT = read_prompt_asset(DETECT_PROMPT_PATH)
CURRENT_STAGE_AGENDA_PROMPT = read_prompt_asset(AGENDA_PROMPT_PATH)
STAGE_AGENDA_BY_TAG = parse_stage_agenda(CURRENT_STAGE_AGENDA_PROMPT)
KNOWN_STAGES = tuple(STAGE_AGENDA_BY_TAG.keys())
