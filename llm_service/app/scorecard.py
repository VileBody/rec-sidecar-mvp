from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .schemas import (
    StageScoreCheck,
    StageScoreEvidence,
    StageScoreSignal,
    StageScorecard,
)
from .stage_assets import StageAgenda


SignalId = Literal["balance", "dialogue", "pain", "specificity", "trust", "focus"]
CheckSignalId = Literal[
    "balance",
    "dialogue",
    "pain",
    "specificity",
    "trust",
    "focus",
    "transition",
]


@dataclass(frozen=True)
class ScoreCheckDefinition:
    id: str
    label: str
    level: Literal["core", "quality", "hygiene"]
    signal: CheckSignalId
    hit: str
    miss: str


class RawScoreEvidence(BaseModel):
    speaker: str | None = None
    quote: str = Field(default="", min_length=1)


class RawScoreCheck(BaseModel):
    id: str
    result: Literal["hit", "miss", "pending", "uncertain", "na"]
    reason: str = Field(default="", min_length=1)
    evidence: list[RawScoreEvidence] = []


class RawScorecard(BaseModel):
    summary: str = Field(default="", min_length=1)
    next_action: str = Field(default="", min_length=1)
    checks: list[RawScoreCheck] = []


SIGNAL_LABELS: dict[SignalId, str] = {
    "balance": "Баланс",
    "dialogue": "Диалог",
    "pain": "Боль",
    "specificity": "Конкретика",
    "trust": "Доверие",
    "focus": "Фокус",
}

SIGNAL_ORDER: tuple[SignalId, ...] = (
    "balance",
    "dialogue",
    "pain",
    "specificity",
    "trust",
    "focus",
)

STAGE_SCORECARD_DEFINITIONS: dict[str, tuple[ScoreCheckDefinition, ...]] = {
    "S2.1": (
        ScoreCheckDefinition(
            "frame_agenda",
            "Рамка звонка",
            "core",
            "focus",
            "Продавец кратко обозначил цель, формат и логику разговора.",
            "Формат звонка не задан или клиент не понимает, что сейчас происходит.",
        ),
        ScoreCheckDefinition(
            "frame_permission",
            "Разрешение на вопросы",
            "core",
            "trust",
            "Клиент явно или неявно согласился на вопросы и продолжение.",
            "Нет согласия на формат, клиент сопротивляется или просит объяснить заново.",
        ),
        ScoreCheckDefinition(
            "frame_no_pitch",
            "Без раннего питча",
            "core",
            "focus",
            "Продавец не уходит в презентацию до диагностики.",
            "Продавец начал продавать/презентовать до получения контекста клиента.",
        ),
        ScoreCheckDefinition(
            "frame_buyer_turn",
            "Клиент включился",
            "quality",
            "dialogue",
            "Клиент начал отвечать содержательно, а не только слушать.",
            "Клиент почти не говорит, продавец держит эфир.",
        ),
        ScoreCheckDefinition(
            "frame_warmth",
            "Тёплый старт",
            "quality",
            "trust",
            "Тон спокойный, уважительный, без давления.",
            "Старт звучит холодно, резко или как скриптовой допрос.",
        ),
    ),
    "S2.2": (
        ScoreCheckDefinition(
            "current_problem_cluster",
            "Проблема найдена",
            "core",
            "pain",
            "Есть хотя бы один реальный problem cluster текущей ситуации.",
            "Текущая проблема не выявлена или звучит слишком поверхностно.",
        ),
        ScoreCheckDefinition(
            "current_buyer_facts",
            "Факты клиента",
            "core",
            "specificity",
            "Клиент дал конкретные факты: события, деньги, сроки, людей, последствия.",
            "Ответы общие: «сложно», «хочу лучше», без деталей.",
        ),
        ScoreCheckDefinition(
            "current_open_question",
            "Открытый вопрос",
            "quality",
            "dialogue",
            "Продавец задал открытый вопрос про текущую реальность.",
            "Продавец ведёт closed-question допрос или сам заполняет ответы.",
        ),
        ScoreCheckDefinition(
            "current_listening_balance",
            "Клиент говорит достаточно",
            "hygiene",
            "balance",
            "Клиент получает достаточно пространства для ответа.",
            "Продавец монологизирует или перебивает discovery.",
        ),
        ScoreCheckDefinition(
            "current_focus",
            "Фокус на текущей ситуации",
            "quality",
            "focus",
            "Разговор не скачет, текущая реальность собирается последовательно.",
            "Продавец прыгает между темами до закрытия текущей нитки.",
        ),
    ),
    "S2.3": (
        ScoreCheckDefinition(
            "target_captured",
            "Цель клиента",
            "core",
            "pain",
            "Клиент сам описал желаемое состояние или результат.",
            "Цель сформулировал продавец или она осталась абстрактной.",
        ),
        ScoreCheckDefinition(
            "gap_captured",
            "Разрыв",
            "core",
            "specificity",
            "Понятен разрыв между текущей ситуацией и желаемым результатом.",
            "Нет явной связки «сейчас -> хочу».",
        ),
        ScoreCheckDefinition(
            "target_specific",
            "Критерий успеха",
            "quality",
            "specificity",
            "Есть срок, сумма, критерий, пример или проверка успеха.",
            "Цель звучит как «лучше/больше/разобраться» без меры.",
        ),
        ScoreCheckDefinition(
            "gap_buyer_confirmation",
            "Подтверждение клиента",
            "core",
            "dialogue",
            "Клиент подтвердил резюме разрыва своими словами.",
            "Gap проговорил только продавец, buyer evidence нет.",
        ),
        ScoreCheckDefinition(
            "gap_summary",
            "Короткое резюме",
            "quality",
            "focus",
            "Продавец коротко зафиксировал current + target.",
            "Нет резюме, разговор расползается.",
        ),
    ),
    "S2.4": (
        ScoreCheckDefinition(
            "motive_why_now",
            "Почему сейчас",
            "core",
            "pain",
            "Выяснено, почему вопрос актуален именно сейчас.",
            "Нет причины действовать сейчас.",
        ),
        ScoreCheckDefinition(
            "motive_personal",
            "Личный мотив",
            "core",
            "trust",
            "Клиент назвал личный, семейный, статусный или эмоциональный мотив.",
            "Мотив остался рациональным и плоским.",
        ),
        ScoreCheckDefinition(
            "motive_inaction_cost",
            "Цена бездействия",
            "core",
            "specificity",
            "Понятно, что будет, если ничего не менять.",
            "Последствия бездействия не раскрыты.",
        ),
        ScoreCheckDefinition(
            "motive_safe_tone",
            "Без давления",
            "quality",
            "trust",
            "Продавец валидирует и не драматизирует боль.",
            "Продавец давит на боль или стыдит клиента.",
        ),
        ScoreCheckDefinition(
            "motive_energy",
            "Есть энергия решения",
            "quality",
            "dialogue",
            "Клиент звучит вовлечённо, не просто «посмотреть».",
            "Клиент не показывает срочности или значимости.",
        ),
    ),
    "S2.5": (
        ScoreCheckDefinition(
            "pivot_summary",
            "Bridge summary",
            "core",
            "focus",
            "Продавец резюмировал ситуацию, цель, gap и мотив.",
            "Переход к офферу резкий, без диагностики.",
        ),
        ScoreCheckDefinition(
            "pivot_link",
            "Связь с решением",
            "core",
            "specificity",
            "Переход привязан к 1-2 словам/проблемам клиента.",
            "Питч generic и не связан с discovery.",
        ),
        ScoreCheckDefinition(
            "pivot_permission",
            "Разрешение на оффер",
            "core",
            "transition",
            "Клиент дал permission слушать предложение.",
            "Продавец перешёл в презентацию без согласия.",
        ),
        ScoreCheckDefinition(
            "pivot_buyer_accepts",
            "Клиент принял диагноз",
            "quality",
            "dialogue",
            "Клиент подтвердил резюме или интерес к продолжению.",
            "Клиент молчит, спорит с диагнозом или поправляет его.",
        ),
    ),
    "S3.1": (
        ScoreCheckDefinition(
            "pitch_personalized",
            "Персональный pitch",
            "core",
            "specificity",
            "Pitch привязан к выявленным фактам, боли или цели клиента.",
            "Pitch звучит как универсальная презентация.",
        ),
        ScoreCheckDefinition(
            "pitch_outcome_mapping",
            "Feature -> outcome",
            "core",
            "pain",
            "Формат/фича связаны с outcome клиента.",
            "Продавец перечисляет фичи без связи с изменением ситуации.",
        ),
        ScoreCheckDefinition(
            "pitch_concise",
            "Короткие блоки",
            "hygiene",
            "balance",
            "Pitch идёт короткими блоками с паузами/check-in.",
            "Длинная лекция без включения клиента.",
        ),
        ScoreCheckDefinition(
            "pitch_checkin",
            "Проверка понимания",
            "quality",
            "dialogue",
            "После смыслового блока продавец проверил отклик.",
            "Продавец не проверяет, понял ли клиент ценность.",
        ),
        ScoreCheckDefinition(
            "pitch_trust",
            "Экспертность без продавливания",
            "quality",
            "trust",
            "Тон компетентный, но не агрессивный.",
            "Pitch звучит давяще или защитно.",
        ),
    ),
    "S3.2": (
        ScoreCheckDefinition(
            "value_question",
            "Проверка ценности",
            "core",
            "dialogue",
            "Продавец спросил, что клиент видит ценным/подходящим.",
            "После pitch продавец сразу идёт в цену/закрытие.",
        ),
        ScoreCheckDefinition(
            "value_buyer_signal",
            "Buyer buy-in",
            "core",
            "trust",
            "Клиент дал явный интерес, согласие или уточнение по применению.",
            "Клиент отвечает нейтрально: «понятно», «интересно» без buy-in.",
        ),
        ScoreCheckDefinition(
            "value_restates",
            "Ценность словами клиента",
            "quality",
            "specificity",
            "Клиент своими словами назвал пользу для себя.",
            "Ценность сформулировал только продавец.",
        ),
        ScoreCheckDefinition(
            "value_objections_before_money",
            "Сомнения до денег",
            "quality",
            "focus",
            "Сомнения выявлены до перехода к оплате.",
            "Продавец игнорирует сомнения и форсирует цену.",
        ),
    ),
    "S3.3": (
        ScoreCheckDefinition(
            "bank_value_before_price",
            "Value до цены",
            "core",
            "transition",
            "Оплата обсуждается после подтверждения ценности.",
            "Цена/банк предложены до value test.",
        ),
        ScoreCheckDefinition(
            "bank_clear_terms",
            "Условия ясны",
            "core",
            "specificity",
            "Цена, банк/рассрочка и шаги объяснены конкретно.",
            "Условия оплаты мутные или клиент их не понял.",
        ),
        ScoreCheckDefinition(
            "bank_reaction_check",
            "Реакция на оплату",
            "core",
            "dialogue",
            "Продавец спокойно спросил реакцию на вариант оплаты.",
            "Продавец тараторит после цены или оправдывает её заранее.",
        ),
        ScoreCheckDefinition(
            "bank_dignity",
            "Достоинство клиента",
            "quality",
            "trust",
            "Деньги обсуждаются без стыда и давления.",
            "Клиента стыдят, додавливают или обесценивают сомнения.",
        ),
    ),
    "S3.4a": (
        ScoreCheckDefinition(
            "objection_detected",
            "Возражение услышано",
            "core",
            "dialogue",
            "Продавец признал сопротивление клиента.",
            "Возражение проигнорировано.",
        ),
        ScoreCheckDefinition(
            "objection_clarified",
            "Уточнение до ответа",
            "core",
            "specificity",
            "Продавец задал уточняющий вопрос до контраргумента.",
            "Продавец сразу спорит, скидкует или защищается.",
        ),
        ScoreCheckDefinition(
            "objection_type",
            "Тип возражения",
            "core",
            "focus",
            "Понятен root type: цена, доверие, время, семья, риск, value.",
            "Неясно, что на самом деле мешает клиенту.",
        ),
        ScoreCheckDefinition(
            "objection_root_reason",
            "Root reason клиента",
            "quality",
            "pain",
            "Клиент дал уточнённую причину своими словами.",
            "Остались общие слова: «дорого», «подумаю», «не уверен».",
        ),
        ScoreCheckDefinition(
            "objection_answer_fit",
            "Ответ по сути",
            "quality",
            "trust",
            "Ответ попал именно в root reason и снял напряжение.",
            "Ответ не соответствует истинному возражению.",
        ),
    ),
    "S3.4b": (
        ScoreCheckDefinition(
            "second_zoom_reason",
            "Причина второго звонка",
            "core",
            "focus",
            "Понятно, зачем нужен второй созвон.",
            "Second zoom используется как слив «подумаете».",
        ),
        ScoreCheckDefinition(
            "second_zoom_datetime",
            "Дата и время",
            "core",
            "specificity",
            "Есть конкретная дата/время.",
            "Нет конкретного слота.",
        ),
        ScoreCheckDefinition(
            "second_zoom_agenda",
            "Agenda решения",
            "core",
            "transition",
            "Зафиксировано, что должно решиться на втором звонке.",
            "Неясно, какой outcome у следующего контакта.",
        ),
        ScoreCheckDefinition(
            "second_zoom_stakeholder",
            "Кто участвует",
            "quality",
            "trust",
            "Понятно, нужен ли партнёр/супруг/эксперт/документы.",
            "Decision context не раскрыт.",
        ),
    ),
    "S3.5": (
        ScoreCheckDefinition(
            "followup_recap",
            "Персональный recap",
            "core",
            "specificity",
            "Follow-up/резюме привязан к боли, цели, мотиву и решению.",
            "Follow-up шаблонный и не отражает разговор.",
        ),
        ScoreCheckDefinition(
            "followup_owner_action",
            "Owner + action",
            "core",
            "transition",
            "Понятно, кто и что делает следующим шагом.",
            "Следующий шаг не назначен владельцу.",
        ),
        ScoreCheckDefinition(
            "followup_deadline",
            "Срок",
            "core",
            "specificity",
            "Есть deadline или время следующего контакта.",
            "Нет срока.",
        ),
        ScoreCheckDefinition(
            "followup_channel",
            "Канал",
            "quality",
            "focus",
            "Понятно, куда отправить материалы/оплату/договор.",
            "Канал коммуникации не подтверждён.",
        ),
        ScoreCheckDefinition(
            "followup_tone",
            "Тёплое закрытие",
            "quality",
            "trust",
            "Тон тёплый и уверенный, без давления.",
            "Закрытие звучит давяще или расплывчато.",
        ),
    ),
}


def scorecard_system_prompt(stage: str, agenda: StageAgenda) -> str:
    definitions = STAGE_SCORECARD_DEFINITIONS.get(stage, ())
    checks = "\n".join(
        (
            f"- {check.id} [{check.level}/{check.signal}] {check.label}\n"
            f"  HIT: {check.hit}\n"
            f"  MISS: {check.miss}"
        )
        for check in definitions
    )
    return f"""Ты realtime evaluator и tactical sales coach для high-ticket B2C sales call на русском.
Оцени только текущую стадию {stage} — {agenda.title}.

Agenda стадии: {agenda.agenda}
Каноническая эмоциональная реакция: {agenda.emotion}
Канонический следующий шаг: {agenda.step}

Правила:
- Возвращай только JSON по схеме.
- Каждый check оцени как hit, miss, pending, uncertain или na.
- hit/miss ставь только при наличии evidence из диалога.
- Не награждай продавца за красивые слова без buyer evidence.
- Если данных ещё мало, ставь pending, а не miss.
- Для miss/hit дай короткую причину и 0-2 короткие цитаты evidence.
- next_action — НЕ пересказывай канонический шаг статично; дай живой совет продавцу по текущему диалогу.
- Если readiness по смыслу red/yellow: next_action начинается с "Уточнить:" и дает 1-3 коротких вопроса или микрошагов, что ещё добрать.
- Если readiness по смыслу green: next_action начинается с "Переход:" и дает готовую фразу перехода на следующую стадию, вопросом или мягким оффером.
- Не пиши длинный разбор; next_action должен быть коротким, читаемым с экрана и пригодным сказать вслух.
- Хорошие форматы: "Уточнить: 1) ... 2) ... 3) ..." или "Переход: Давайте я расскажу про формат, который как раз закрывает этот разрыв..."
- Не считай секунды, проценты и talk ratio сам; оцени смысловые признаки из текста.

Checks текущей стадии:
{checks}
"""


def vertex_scorecard_response_schema() -> dict[str, Any]:
    return {
        "type": "OBJECT",
        "properties": {
            "summary": {"type": "STRING"},
            "next_action": {"type": "STRING"},
            "checks": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "id": {"type": "STRING"},
                        "result": {
                            "type": "STRING",
                            "enum": ["hit", "miss", "pending", "uncertain", "na"],
                        },
                        "reason": {"type": "STRING"},
                        "evidence": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "speaker": {"type": "STRING"},
                                    "quote": {"type": "STRING"},
                                },
                                "required": ["quote"],
                            },
                        },
                    },
                    "required": ["id", "result", "reason"],
                },
            },
        },
        "required": ["summary", "next_action", "checks"],
    }


def parse_raw_scorecard(text: str) -> RawScorecard:
    value = _extract_json_object(text)
    if not isinstance(value, dict):
        raise ValueError(f"scorecard response is not JSON object: {text!r}")
    return RawScorecard.model_validate(value)


def normalize_scorecard(
    *,
    stage: str,
    agenda: StageAgenda,
    raw: RawScorecard | None,
    fallback_reason: str | None = None,
) -> StageScorecard:
    definitions = STAGE_SCORECARD_DEFINITIONS.get(stage, ())
    raw_by_id = {check.id: check for check in (raw.checks if raw else [])}
    checks: list[StageScoreCheck] = []

    for definition in definitions:
        raw_check = raw_by_id.get(definition.id)
        if raw_check is None:
            result: Literal["hit", "miss", "pending", "uncertain", "na"] = "pending"
            reason = fallback_reason or "Пока не хватает evidence по этому критерию."
            evidence: list[StageScoreEvidence] = []
        else:
            result = raw_check.result
            reason = raw_check.reason.strip() or "Оценено по диалогу."
            evidence = [
                StageScoreEvidence(
                    speaker=item.speaker.strip() if item.speaker else None,
                    quote=item.quote.strip(),
                )
                for item in raw_check.evidence[:2]
                if item.quote.strip()
            ]
        checks.append(
            StageScoreCheck(
                id=definition.id,
                label=definition.label,
                level=definition.level,
                result=result,
                signal=definition.signal,
                reason=reason,
                evidence=evidence,
            )
        )

    hit_count = sum(1 for check in checks if check.result == "hit")
    miss_count = sum(1 for check in checks if check.result == "miss")
    total_count = hit_count + miss_count
    score = round(hit_count / total_count, 2) if total_count else None
    core_miss = any(check.level == "core" and check.result == "miss" for check in checks)
    hard_red = core_miss

    if total_count == 0:
        readiness: Literal["green", "yellow", "red", "pending"] = "pending"
    elif hard_red or (score is not None and score < 0.45):
        readiness = "red"
    elif score is not None and score >= 0.70 and not core_miss:
        readiness = "green"
    else:
        readiness = "yellow"

    readiness_label = {
        "green": "Готово",
        "yellow": "Почти",
        "red": "Рано",
        "pending": "Мало данных",
    }[readiness]

    next_action = (raw.next_action if raw else "").strip()
    if not next_action:
        next_action = agenda.step
    summary = (raw.summary if raw else "").strip()
    if not summary:
        summary = fallback_reason or "Ожидаю больше buyer evidence по текущей стадии."

    return StageScorecard(
        readiness=readiness,
        readiness_label=readiness_label,
        score=score,
        hit_count=hit_count,
        miss_count=miss_count,
        total_count=total_count,
        hard_red=hard_red,
        ready_to_advance=readiness == "green",
        next_action=next_action,
        summary=summary,
        checks=checks,
        signals=scorecard_signals(checks),
    )


def fallback_scorecard(stage: str, agenda: StageAgenda, reason: str | None = None) -> StageScorecard:
    return normalize_scorecard(
        stage=stage,
        agenda=agenda,
        raw=None,
        fallback_reason=reason or "Scorecard evaluator временно недоступен.",
    )


def scorecard_signals(checks: list[StageScoreCheck]) -> list[StageScoreSignal]:
    signals: list[StageScoreSignal] = []
    for signal_id in SIGNAL_ORDER:
        related = [
            check
            for check in checks
            if _signal_group(check.signal) == signal_id and check.result != "na"
        ]
        state, detail = _signal_state(related)
        signals.append(
            StageScoreSignal(
                id=signal_id,
                label=SIGNAL_LABELS[signal_id],
                state=state,
                detail=detail,
            )
        )
    return signals


def _signal_group(signal: CheckSignalId) -> SignalId:
    if signal == "transition":
        return "focus"
    return signal


def _signal_state(
    checks: list[StageScoreCheck],
) -> tuple[Literal["green", "yellow", "red", "gray"], str]:
    eligible = [check for check in checks if check.result in {"hit", "miss"}]
    if not eligible:
        return "gray", "Мало данных"
    core_miss = next((check for check in eligible if check.level == "core" and check.result == "miss"), None)
    if core_miss:
        return "red", core_miss.reason
    hit_count = sum(1 for check in eligible if check.result == "hit")
    ratio = hit_count / len(eligible)
    first_miss = next((check for check in eligible if check.result == "miss"), None)
    if ratio >= 0.70:
        return "green", "Ок"
    if first_miss:
        return "yellow", first_miss.reason
    return "yellow", "Частично"


def _extract_json_object(text: str) -> Any:
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


def safe_parse_scorecard(text: str) -> RawScorecard:
    try:
        return parse_raw_scorecard(text)
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid scorecard response: {exc}") from exc
