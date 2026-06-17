from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import httpx

from .config import Settings
from .orchestrator import LlmOrchestrator
from .providers import ProviderError
from .scorecard import fallback_next_action, is_speakable_next_action
from .schemas import StageRequest
from .stage_assets import (
    CURRENT_STAGE_AGENDA_PROMPT,
    STAGE_AGENDA_BY_TAG,
    clamp_stage_forward,
    extract_json_object,
    normalize_stage,
    parse_stage_detection,
    stage_detection_system_prompt,
)


DEFAULT_INPUT = Path("sales_scripts/glubina_kazan_10_call_scripts_v1.md")
DEFAULT_OUTPUT_DIR = Path("sales_scripts/paper_roleplays")
DEFAULT_TURN_PAIRS = 30
DEFAULT_MAX_REPLIES = 60
DEFAULT_HISTORY_LINES = 24

EVENT_FACTS_FALLBACK = (
    "glubina.core, Казань, 7-10 июля 2026, 4 офлайн дня, 160 участников, "
    "19 менторов, группы по 8 предпринимателей одного уровня, живые разборы, "
    "цели, психологи, личная декларация, цели на 90 дней, группа на связи, "
    "возможность вступить в community, стоимость 99 000 руб."
)

SELLER_RULES = """
Ты один и тот же продавец glubina.core во всех симуляциях.

Как работает продавец:
- Он не импровизирует вне правил: текущий ход приходит из stage/scorecard pipeline.
- Он говорит коротко, естественно, по-русски, без канцелярита.
- Он не перескакивает назад по стадиям.
- До питча сначала собирает текущую ситуацию, цель, разрыв и мотив.
- Он не спорит с сопротивлением, а уточняет настоящую причину.
- В каждом ходу одна основная мысль или один вопрос.
""".strip()

BUYER_SYSTEM_PROMPT = """
Ты buyer-agent в тренировочном sales roleplay для high-check B2C продажи билета на glubina.core Казань.

У тебя есть общий контекст события и приватный профиль покупателя.
Отвечай только как покупатель. Не пиши реплики продавца.

Поведение:
- говори естественно, как предприниматель в живом звонке;
- ты сложный, многослойный покупатель, а не один objection на ножках;
- не соглашайся слишком легко: сопротивления должны проявляться фазами и эволюционировать;
- если продавец задает конкретный вопрос, отвечай по делу, но раскрывайся постепенно;
- если продавец задает шаблонный вопрос или игнорирует прямой вопрос, раздражайся реалистично;
- если продавец попадает в твою конкретику, постепенно давай больше фактов и доверия;
- если продавец обещает неподтвержденные факты, сомневайся и проси доказательства;
- если продавец 2+ раза не отвечает на прямой вопрос "что это/цена/коротко", дави на краткость;
- не повторяй одно и то же сопротивление дословно;
- можно менять тон: осторожность, интерес, скепсис, усталость, доверие;
- покупатель может прийти к terminal outcome: бронь/оплата, конкретный второй созвон,
  квалифицированный отказ или "нецелевой";
- 1-2 коротких предложения на ход.

Верни строго JSON без Markdown:
{"state":"что сейчас происходит с покупателем","text":"реплика покупателя"}
""".strip()

SELLER_SYSTEM_PROMPT = """
Ты seller-agent в тренировочном high-check B2C sales roleplay.

Твоя задача — превратить tactical next_action из stage/scorecard pipeline в одну
живую реплику продавца. next_action — это инструкция, НЕ текст для дословного чтения.

Жесткие правила:
- отвечай только как продавец, не пиши реплику покупателя;
- 1-3 коротких предложения, без markdown, списков и "here is";
- сначала отвечай на прямой вопрос клиента, потом максимум один мягкий вопрос;
- не повторяй предыдущую реплику продавца и не повторяй один и тот же boundary-ответ;
- не произноси внутренние инструкции: "выяснить", "дать pitch", "зафиксировать", "feature -> outcome";
- не выдумывай факты вне brief: трекеров, службу безопасности, отраслевых менторов,
  готовые кейсы, гарантии, внешний аудит, штрафы, ежедневные отчеты,
  кураторов/модераторов, чек-поинты, возвраты денег, изменения договора;
- если клиент просит гарантию/внедрение под ключ, честно обозначь mismatch и экологично закрывай,
  а не продолжай продавать;
- если клиент просит штрафы, возврат, расторжение или особый пункт договора, не соглашайся
  от имени продукта; предложи отправить типовые условия или уточнить у команды;
- если клиент просит фамилии менторов, список, кейсы или доказательства, которых нет в brief,
  не обобщай и не фантазируй; предложи отправить программу/список материалов в Telegram;
- если клиент 2+ раза просит конкретный механизм, пример, шаблон или "как выглядит",
  не задавай еще один уточняющий вопрос; предложи отправить материал/пример структуры в Telegram
  или честно зафиксируй, что без этого ему не стоит покупать;
- если клиент просит материал в Telegram, просто подтверди отправку и не назначай звонок без разрешения;
- если продавец уже ошибся или повторился, коротко признай ошибку и сделай следующий полезный шаг.

Верни строго JSON без Markdown:
{"text":"реплика продавца"}
""".strip()


@dataclass(frozen=True)
class BuyerProfile:
    number: int
    title: str
    persona: str
    seed_client_lines: tuple[str, ...]

    @property
    def slug(self) -> str:
        raw = re.sub(r"[^a-zA-Z0-9]+", "-", self.title.lower()).strip("-")
        return raw or f"scenario-{self.number:02d}"

    @property
    def public_brief(self) -> str:
        complexity = complex_buyer_profile(self)
        public_line = complexity.splitlines()[0] if complexity else ""
        return f"Сценарий {self.number}: {self.title}. {self.persona} {public_line}".strip()

    @property
    def private_brief(self) -> str:
        lines = "\n".join(f"- {line}" for line in self.seed_client_lines[:6])
        seed = f"\n\nПримеры интонации из seed-script:\n{lines}" if lines else ""
        complexity = complex_buyer_profile(self)
        return f"{self.public_brief}\n\nСложный профиль покупателя:\n{complexity}{seed}"


@dataclass(frozen=True)
class RoleplayConfig:
    turn_pairs: int = DEFAULT_TURN_PAIRS
    max_replies: int = DEFAULT_MAX_REPLIES
    stop_on_terminal: bool = True
    buyer_model: str | None = None
    buyer_temperature: float = 0.85
    buyer_max_tokens: int = 320
    seller_model: str | None = None
    seller_temperature: float = 0.45
    seller_max_tokens: int = 260
    use_seller_agent: bool = True
    history_lines: int = DEFAULT_HISTORY_LINES
    seller_name: str = "Ирина"
    buyer_name: str = "Алексей"
    run_id_prefix: str = "paper-roleplay"
    concurrency: int = 1
    stage_provider: str = "auto"


@dataclass(frozen=True)
class StageSnapshot:
    stage: str
    title: str
    agenda: str
    emotion: str
    step: str
    provider: str
    model: str
    confidence: float | None
    readiness: str
    readiness_label: str
    score: float | None
    summary: str
    next_action: str
    checks: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True)
class RoleplayTurn:
    index: int
    stage: StageSnapshot
    seller_text: str
    buyer_text: str
    buyer_state: str
    terminal_outcome: str | None
    elapsed_ms: int


@dataclass(frozen=True)
class RoleplayResult:
    profile: BuyerProfile
    turns: tuple[RoleplayTurn, ...]
    event_facts: str
    run_id: str
    terminal_outcome: str
    terminal_reason: str
    elapsed_secs: float


def strip_markdown(text: str) -> str:
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = text.replace("—", "-")
    return re.sub(r"\s+", " ", text).strip()


def load_seed_markdown(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def extract_event_facts(markdown: str) -> str:
    head = markdown.split("\n## Скрипт", 1)[0].strip()
    if not head:
        return EVENT_FACTS_FALLBACK
    lines = [
        strip_markdown(line)
        for line in head.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return "\n".join(lines) or EVENT_FACTS_FALLBACK


def extract_buyer_profiles(markdown: str) -> list[BuyerProfile]:
    parts = re.split(r"(?m)^## Скрипт\s+(\d+)\.\s*(.+)$", markdown)
    profiles: list[BuyerProfile] = []
    for index in range(1, len(parts), 3):
        number = int(parts[index])
        title = strip_markdown(parts[index + 1])
        body = parts[index + 2]
        persona_match = re.search(r"(?m)^\*\*Персона:\*\*\s*(.+)$", body)
        persona = strip_markdown(persona_match.group(1)) if persona_match else title
        client_lines = tuple(
            strip_markdown(match.group(1))
            for match in re.finditer(r"(?m)^\*\*Клиент:\*\*\s*(.+)$", body)
        )
        profiles.append(
            BuyerProfile(
                number=number,
                title=title,
                persona=persona,
                seed_client_lines=client_lines,
            )
        )
    return profiles


COMPLEX_PROFILE_BY_NUMBER: dict[int, str] = {
    1: """
Публично: "нет 4 дней, всё на мне".
Скрытый мотив: боится признать, что команда не автономна из-за его контроля.
Прошлый опыт: покупал консалтинг, получил красивые схемы без внедрения.
Decision constraints: должен оставить старшего менеджера, но не доверяет ему.
Red lines: не принимает фразы "просто делегируйте" и обещания легкого выхода.
Resistance phases: время -> риск развала команды -> недоверие к практичности -> цена.
Switching condition: продавец помогает разложить 4-дневное отсутствие как управленческий тест с owner/action.
Terminal outcomes: бронь после плана подготовки команды или второй созвон с операционным.
""",
    2: """
Публично: "я уже был на курсах, везде вода".
Скрытый мотив: стыдно, что после прошлых покупок ничего не внедрил.
Прошлый опыт: мастермайнд с сильными обещаниями, слабой группой и нулевым follow-up.
Decision constraints: согласится только если увидит отличие от лекций и механизм внедрения.
Red lines: "абсолютно реально", "без воды", "у нас не курс" без диагностики прошлого провала.
Resistance phases: недоверие к формату -> проблема с кадрами -> применимость к агентству -> цена времени.
Switching condition: продавец точно разбирает, где ломается делегирование в агентстве недвижимости.
Terminal outcomes: согласие на pitch после buyer-specific hypothesis или отказ "это опять формат ради формата".
""",
    3: """
Публично: "кассовый разрыв, 99 000 сейчас больно".
Скрытый мотив: боится выглядеть слабым перед командой и сорвать зарплаты.
Прошлый опыт: платил за обучение, но оно не решило дебиторку.
Decision constraints: физически нет свободных денег; нужен понятный next step без магии.
Red lines: обещания юристов/факторинга/строительных менторов без фактуры.
Resistance phases: нет денег -> окупаемость -> юридическая применимость -> срок результата.
Switching condition: продавец отделяет "не верю в ценность" от "нет cash" и предлагает безопасный следующий шаг.
Terminal outcomes: второй созвон по платежу/документам или отказ до стабилизации кассы.
""",
    4: """
Публично: успешен, но личное и выгорание влияют на бизнес.
Скрытый мотив: хочет вернуть контроль над жизнью, но боится психологии.
Прошлый опыт: ретриты/коучи дали эмоции без бизнес-решений.
Decision constraints: не хочет выглядеть уязвимым, требует конфиденциальности.
Red lines: давление на семью, драматизация боли, "просто отпустить контроль".
Resistance phases: отрицание проблемы -> страх публичного разбора -> сомнение в практичности -> цена.
Switching condition: продавец связывает личный мотив с управленческим решением без терапии на витрине.
Terminal outcomes: готовность к конфиденциальному разбору или второй звонок с уточнением формата.
""",
    5: """
Публично: ищет сильную среду, боится попасть "не к своему уровню".
Скрытый мотив: хочет статусного подтверждения и партнерства, но боится оказаться выше группы.
Прошлый опыт: бизнес-клубы с нерелевантными участниками.
Decision constraints: нужны критерии подбора группы, но без пустых гарантий.
Red lines: обещания оборотов группы, "подберем точно твоих" без доказательств.
Resistance phases: уровень участников -> конфиденциальность -> ROI связей -> цена.
Switching condition: продавец честно объясняет механизм matching и что можно/нельзя гарантировать.
Terminal outcomes: бронь после проверки состава или follow-up с материалами о группе.
""",
    6: """
Публично: из другого города, хочет вход в Татарстан.
Скрытый мотив: боится провалить региональную экспансию и потерять год.
Прошлый опыт: холодные интро не конвертились в доверие.
Decision constraints: поездка окупится только если появится понятный план контактов.
Red lines: гарантированные партнеры/договоры за 4 дня.
Resistance phases: логистика -> окупаемость -> доверие в регионе -> конкретные встречи.
Switching condition: продавец переводит событие в гипотезу "ускорить доверие", не обещая сделки.
Terminal outcomes: второй звонок по целевым контактам или отказ из-за слабой региональной релевантности.
""",
    7: """
Публично: "мне нужны инструменты, не психологи".
Скрытый мотив: боится, что плато связано с ним как руководителем.
Прошлый опыт: психологизированные программы раздражали, инструментальные давали краткий эффект.
Decision constraints: хочет алгоритм внедрения и цифры, не эмоциональную исповедь.
Red lines: "а что вы чувствуете", давление на личную боль, абстрактная трансформация.
Resistance phases: анти-психология -> скепсис к внедрению -> срок окупаемости -> команда.
Switching condition: продавец говорит языком метрик, системы и внедрения, не споря с неприятием психологии.
Terminal outcomes: согласие на pitch через business case или отказ "слишком мягко".
""",
    8: """
Публично: нужно обсудить с партнером/женой/командой.
Скрытый мотив: сам заинтересован, но боится взять ответственность за решение.
Прошлый опыт: решения без партнера создавали конфликт.
Decision constraints: нужен stakeholder path: кто, когда, что должен понять.
Red lines: "решайте сами", давление купить без партнера, обесценивание второго лица.
Resistance phases: не решаю один -> партнер занят -> нужна выгода для партнера -> перенос.
Switching condition: продавец строит next step вокруг stakeholder, не теряя ценность оффера.
Terminal outcomes: трехсторонний звонок с agenda или отказ без вовлечения партнера.
""",
    9: """
Публично: горячий лид, много мелких тревог.
Скрытый мотив: хочет купить, но боится выглядеть наивным и попасть в "секту".
Прошлый опыт: был в комьюнити с токсичной атмосферой и upsell pressure.
Decision constraints: нужны правила участия, ожидания, что будет если не понравится.
Red lines: размытые ответы, эзотерика, давление "решайтесь сейчас".
Resistance phases: формат -> безопасность среды -> оплата -> что взять/как подготовиться.
Switching condition: продавец спокойно закрывает тревоги и фиксирует практический next step.
Terminal outcomes: бронь/оплата или запрос договора/условий в Telegram.
""",
    10: """
Публично: холодный лид по рекомендации, просит коротко.
Скрытый мотив: возможно релевантен, но ненавидит sales discovery без ответа на прямой вопрос.
Прошлый опыт: продавцы тянули время вопросами и потом присылали презентации.
Decision constraints: нужно за 60 секунд понять тему, для кого, цену и зачем ему/партнерам.
Red lines: повторные вопросы вместо ответа, "давайте сначала разберем", психологизация.
Resistance phases: что это -> кому переслать -> почему мне -> отстаньте/скиньте в Telegram.
Switching condition: продавец сначала отвечает кратко, затем спрашивает разрешение на 1 уточнение.
Terminal outcomes: разрешение на продолжение, Telegram follow-up или жесткий отказ.
""",
}


def complex_buyer_profile(profile: BuyerProfile) -> str:
    return (
        COMPLEX_PROFILE_BY_NUMBER.get(profile.number)
        or f"""
Публично: {profile.persona}
Скрытый мотив: клиент проверяет, понимает ли продавец его конкретный контекст.
Прошлый опыт: были покупки, где обещания не превратились во внедрение.
Decision constraints: нужно увидеть конкретный next step и безопасный риск.
Red lines: generic pitch, давление, неподтвержденные обещания.
Resistance phases: доверие -> применимость -> деньги/время -> decision process.
Switching condition: продавец точно связывает формат с buyer-specific задачей.
Terminal outcomes: бронь, конкретный второй звонок, follow-up или квалифицированный отказ.
"""
    ).strip()


def dialogue_text(history: Iterable[tuple[str, str]], *, limit: int | None = None) -> str:
    items = list(history)
    if limit is not None:
        items = items[-limit:]
    if not items:
        return "(диалог только начинается)"
    return "\n".join(f"{speaker}: {text}" for speaker, text in items)


def build_stage_context(
    *,
    event_facts: str,
    profile: BuyerProfile,
    history: list[tuple[str, str]],
    history_lines: int,
) -> str:
    return (
        "--- Событие / продукт ---\n"
        f"{event_facts}\n\n"
        "--- Pre-call brief ---\n"
        f"{profile.public_brief}\n\n"
        "--- Диалог ---\n"
        f"{dialogue_text(history, limit=history_lines)}\n"
    )


def build_buyer_prompt(
    *,
    event_facts: str,
    profile: BuyerProfile,
    history: list[tuple[str, str]],
    stage: StageSnapshot,
    seller_text: str,
    history_lines: int,
) -> str:
    return (
        "--- Общая agenda / stage map ---\n"
        f"{CURRENT_STAGE_AGENDA_PROMPT}\n\n"
        "--- Событие / продукт ---\n"
        f"{event_facts}\n\n"
        "--- Приватный профиль покупателя ---\n"
        f"{profile.private_brief}\n\n"
        "--- Текущая стадия продавца ---\n"
        f"{stage.stage} {stage.title}\n"
        f"Agenda: {stage.agenda}\n"
        f"Readiness: {stage.readiness_label}\n"
        f"Seller intent: {stage.next_action or stage.step}\n\n"
        "--- Последний диалог ---\n"
        f"{dialogue_text(history, limit=history_lines)}\n\n"
        "--- Последняя реплика продавца ---\n"
        f"{seller_text}\n\n"
        "--- Terminal behavior ---\n"
        "Если продавец реально выполнил твои switching conditions, можно дать terminal signal: "
        "готов оплатить/забронировать, назначить второй звонок с конкретным временем, попросить счет/ссылку, "
        "или честно отказаться с причиной. Если условия не выполнены, продолжай сопротивляться реалистично.\n\n"
        "Ответь как покупатель на последнюю реплику продавца."
    )


def build_seller_prompt(
    *,
    event_facts: str,
    profile: BuyerProfile,
    history: list[tuple[str, str]],
    stage: StageSnapshot,
    history_lines: int,
) -> str:
    checks = "\n".join(f"- {check}" for check in stage.checks[:8]) or "(нет)"
    return (
        "--- Правила продавца ---\n"
        f"{SELLER_RULES}\n\n"
        "--- Событие / подтвержденная фактура ---\n"
        f"{event_facts}\n\n"
        "--- Pre-call brief ---\n"
        f"{profile.public_brief}\n\n"
        "--- Текущий stage / scorecard ---\n"
        f"Stage: {stage.stage} {stage.title}\n"
        f"Agenda: {stage.agenda}\n"
        f"Readiness: {stage.readiness_label}\n"
        f"Scorecard summary: {stage.summary or 'n/a'}\n"
        f"Checks:\n{checks}\n\n"
        "--- Tactical next_action от pipeline ---\n"
        f"{stage.next_action or stage.step}\n\n"
        "--- Последний диалог ---\n"
        f"{dialogue_text(history, limit=history_lines)}\n\n"
        "--- Задача ---\n"
        "Сгенерируй следующую реплику продавца. Не копируй next_action дословно; "
        "сделай ее живой, контекстной и безопасной по фактам."
    )


def fallback_stage_snapshot(current_stage: str | None, error: str | None = None) -> StageSnapshot:
    stage = normalize_stage(current_stage or "") or "S2.1"
    if stage not in STAGE_AGENDA_BY_TAG:
        stage = "S2.1"
    agenda = STAGE_AGENDA_BY_TAG[stage]
    return StageSnapshot(
        stage=agenda.stage,
        title=agenda.title,
        agenda=agenda.agenda,
        emotion=agenda.emotion,
        step=agenda.step,
        provider="fallback",
        model="stage-map",
        confidence=0.0,
        readiness="pending",
        readiness_label="Мало данных",
        score=None,
        summary="Stage fallback: нет ответа от stage/scorecard pipeline.",
        next_action=fallback_next_action(),
        checks=(),
        error=error,
    )


def heuristic_stage_snapshot(
    current_stage: str | None,
    history: list[tuple[str, str]],
    error: str | None = None,
) -> StageSnapshot:
    pair_index = len(history) // 2
    last_buyer = ""
    for speaker, text in reversed(history):
        if speaker == "Клиент":
            last_buyer = normalize_reply_for_compare(text)
            break

    proposed = heuristic_stage_tag(current_stage, pair_index, last_buyer)
    stage = clamp_stage_forward(current_stage, proposed)
    agenda = STAGE_AGENDA_BY_TAG[stage]
    return StageSnapshot(
        stage=agenda.stage,
        title=agenda.title,
        agenda=agenda.agenda,
        emotion=agenda.emotion,
        step=agenda.step,
        provider="heuristic",
        model="paper-stage-map",
        confidence=0.5,
        readiness="pending",
        readiness_label="Мало данных",
        score=None,
        summary="Heuristic paper-roleplay stage fallback: external stage provider unavailable or disabled.",
        next_action=heuristic_next_action(agenda.step, last_buyer),
        checks=(),
        error=error,
    )


def heuristic_stage_tag(current_stage: str | None, pair_index: int, last_buyer: str) -> str:
    in_sales_stage = (current_stage or "") in {"S3.1", "S3.2", "S3.3", "S3.4a", "S3.4b", "S3.5"}
    if any(marker in last_buyer for marker in ("telegram", "телеграм", "материал", "скиньте", "пришлите", "ссылк")):
        return "S3.5"
    if (pair_index >= 5 or in_sales_stage) and any(marker in last_buyer for marker in ("партнер", "жена", "команд", "обсудить", "созвон", "завтра", "четверг")):
        return "S3.4b"
    if (pair_index >= 5 or in_sales_stage) and any(
        marker in last_buyer
        for marker in ("дорого", "не готов", "сомнева", "гарант", "возврат", "как именно", "механик", "докаж", "покаж")
    ):
        return "S3.4a"
    if any(marker in last_buyer for marker in ("сколько стоит", "цена", "стоимость", "условия оплаты", "оплата")):
        return "S3.3"
    if pair_index >= 5 and any(marker in last_buyer for marker in ("ценность", "подходит", "интересно", "звучит")):
        return "S3.2"
    if pair_index >= 5 and any(marker in last_buyer for marker in ("расскаж", "формат", "что будет", "что делаем", "4 дня")):
        return "S3.1"

    schedule = (
        (0, "S2.1"),
        (1, "S2.2"),
        (2, "S2.3"),
        (4, "S2.4"),
        (6, "S2.5"),
        (7, "S3.1"),
        (9, "S3.2"),
        (10, "S3.3"),
        (11, "S3.4a"),
    )
    proposed = current_stage or "S2.1"
    for threshold, stage in schedule:
        if pair_index >= threshold:
            proposed = stage
    return proposed


def heuristic_next_action(default_step: str, last_buyer: str) -> str:
    if any(marker in last_buyer for marker in ("для собственников", "для топов", "кому переслать", "пересылать")):
        return "Ответить прямо: формат в первую очередь для собственников или партнеров decision maker, топам можно переслать только как контекст."
    if any(marker in last_buyer for marker in ("сколько стоит", "цена", "стоимость", "условия оплаты")):
        return "Ответить прямо: участие стоит 99 000 рублей; затем проверить, что важнее клиенту понять перед решением."
    if any(marker in last_buyer for marker in ("что они там реально будут делать", "что заберут", "что делают 4 дня", "что я делаю")):
        return "Ответить по шагам: 4 дня разборов в группе, работа с менторами и психологами, на выходе личная декларация и цели на 90 дней."
    if any(marker in last_buyer for marker in ("как именно", "механик", "контрол", "дисциплин", "гарант")):
        return "Ответить честно: 100% гарантий поведения нет; есть конкретное правило, критерий проверки и внешняя рамка на 90 дней."
    if any(marker in last_buyer for marker in ("telegram", "телеграм", "материал", "скиньте", "пришлите", "ссылк")):
        return "Подтвердить отправку материалов в Telegram и не тянуть клиента лишними вопросами."
    if seller_text_is_meta(default_step):
        return "Показать формат простыми словами, связать его с задачей клиента и проверить отклик."
    return default_step


def stage_snapshot_from_response(response: Any, current_stage: str | None) -> StageSnapshot:
    stage = clamp_stage_forward(current_stage, response.stage)
    agenda = STAGE_AGENDA_BY_TAG.get(stage)
    if agenda is None:
        return fallback_stage_snapshot(current_stage, f"unknown stage: {response.stage}")

    scorecard = response.scorecard
    checks: list[str] = []
    if scorecard:
        for check in scorecard.checks:
            checks.append(f"{check.signal}/{check.result}: {check.reason}")

    return StageSnapshot(
        stage=agenda.stage,
        title=response.title or agenda.title,
        agenda=response.agenda or agenda.agenda,
        emotion=response.emotion or agenda.emotion,
        step=response.step or agenda.step,
        provider=response.provider,
        model=response.model,
        confidence=response.confidence,
        readiness=scorecard.readiness if scorecard else "pending",
        readiness_label=scorecard.readiness_label if scorecard else "Мало данных",
        score=scorecard.score if scorecard else None,
        summary=scorecard.summary if scorecard else "",
        next_action=scorecard.next_action if scorecard else response.step,
        checks=tuple(checks),
    )


def speakable_seller_text(stage: StageSnapshot) -> str:
    text = stage.next_action or stage.step or stage.emotion
    text = re.sub(
        r"^\s*(?:совет|следующий ход|уточнить|переход|сказать|спросить|шаг)\s*[:：-]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = text.strip().strip("\"'«»“”").strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        text = stage.step or stage.emotion
    return text


def parse_seller_response(text: str) -> str:
    value = extract_json_object(text)
    if isinstance(value, dict):
        reply = str(value.get("text") or "").strip()
        if reply:
            return sanitize_seller_reply(reply)
    return sanitize_seller_reply(text)


def sanitize_seller_reply(text: str) -> str:
    value = strip_markdown(text)
    value = value.strip().strip("\"'`«»“”").strip()
    value = re.sub(r"^(?:продавец|seller|sales)\s*[:：-]\s*", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def is_repeated_seller_text(candidate: str, history: list[tuple[str, str]]) -> bool:
    normalized = normalize_reply_for_compare(candidate)
    if not normalized:
        return False
    previous = [
        normalize_reply_for_compare(text)
        for speaker, text in history
        if speaker == "Продавец"
    ][-4:]
    for prior in previous:
        if not prior:
            continue
        if normalized == prior:
            return True
        if len(normalized) >= 80 and (normalized in prior or prior in normalized):
            return True
    boundary_markers = (
        "подтвержденный кейс",
        "внешний аудит",
        "не тот формат",
        "неподходящее решение",
        "готовую гарантию",
    )
    return any(marker in normalized for marker in boundary_markers) and any(
        marker in prior for prior in previous for marker in boundary_markers
    )


def normalize_reply_for_compare(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip()


def seller_reply_is_usable(candidate: str, history: list[tuple[str, str]]) -> bool:
    if not candidate or len(candidate) < 12:
        return False
    if candidate.startswith("[") and candidate.endswith("]"):
        return False
    if re.search(r"\b(?:feature\s*->|выяснить|зафиксировать|дать короткий|next_action)\b", candidate, re.I):
        return False
    risky_promises = (
        r"\bNDA\b",
        r"\bодин\s+из\s+участник\w*\s+смог\b",
        r"\bдругой\s*-\s*выстроил\b",
        r"\bреальн\w+\s+кейс\w*\b",
        r"\bточно\s+будет\b",
        r"\bподробн\w+\s+информац\w+\s+о\s+бэкграунд\w*\b",
        r"\bментор[а-я-]*юрист\w*\b",
        r"\bментор[а-я-]*производственник\w*\b",
        r"\bкоординатор\w*\s+по\s+сет\w*\b",
        r"\bверн[уе]м\b",
        r"\bвозврат(?:им|а|у|ом)?\b",
        r"\bштрафн\w*\b",
        r"\bрасторжен\w*\b",
        r"\bперезаключен\w*\b",
        r"\bобязуемся\s+заменить\b",
        r"\bзаменим\s+куратор\w*\b",
        r"\bпропишем\s+в\s+договор\w*\b",
        r"\bзафиксируем\s+в\s+договор\w*\b",
        r"\bкажд(?:ые|ую)\s+две\s+недел\w*\b",
    )
    if any(re.search(pattern, candidate, re.I) for pattern in risky_promises):
        return False
    if buyer_repeated_concrete_request(history) and re.search(
        r"\b(?:что\s+для\s+вас|что\s+именно|правильно\s+ли|вы\s+хотите|сомнева[её]тесь|помогает\s+ли)\b",
        candidate,
        re.I,
    ):
        return False
    if not is_speakable_next_action(candidate):
        return False
    if is_repeated_seller_text(candidate, history):
        return False
    return True


def buyer_repeated_concrete_request(history: list[tuple[str, str]]) -> bool:
    count = 0
    for speaker, text in history[-12:]:
        if speaker != "Клиент":
            continue
        normalized = normalize_reply_for_compare(text)
        if any(
            marker in normalized
            for marker in (
                "как именно",
                "механизм",
                "конкретн",
                "пример",
                "шаблон",
                "как выглядит",
                "что в него входит",
            )
        ):
            count += 1
    return count >= 2


def seller_fallback_reply(stage: StageSnapshot, history: list[tuple[str, str]]) -> str:
    last_buyer = ""
    for speaker, text in reversed(history):
        if speaker == "Клиент":
            last_buyer = text.lower().replace("ё", "е")
            break

    if any(marker in last_buyer for marker in ("для собственников", "для топов", "кому переслать", "пересылать")):
        return "В первую очередь это формат для собственника или партнера, который принимает стратегические решения; топам можно переслать только как контекст, но участником должен быть decision maker."
    if any(marker in last_buyer for marker in ("что они там реально будут делать", "что заберут", "что делают 4 дня", "что я делаю", "что именно я делаю")):
        return "За 4 дня участник проходит разборы в группе, работает с менторами и психологами, а на выходе забирает личную декларацию и цели на 90 дней."
    if any(marker in last_buyer for marker in ("кто эти менторы", "фамилии", "список менторов", "кейсы", "кто конкретно")):
        return (
            "Фамилии и кейсы на слух выдумывать не буду. "
            "Отправлю вам в Telegram программу и список менторов, чтобы вы спокойно проверили релевантность."
        )
    if buyer_repeated_concrete_request(history):
        return (
            "Вы правы, я уже начал ходить вокруг да около. "
            "Не буду додумывать на слух: отправлю в Telegram программу и пример структуры 90-дневного плана, а если там не будет нужной конкретики, честно закроем вопрос."
        )
    if any(marker in last_buyer for marker in ("вернет мне деньги", "возврат денег", "кассу", "долг", "дебитор")):
        return (
            "Деньги за вас группа не вернет и коллекторской услуги здесь нет; можно разобрать стратегию переговоров и план давления на должника. "
            "Какой шаг в возврате сейчас самый тупиковый?"
        )
    if any(marker in last_buyer for marker in ("как именно", "механик", "контрол", "дисциплин", "не вернусь", "не сорвусь", "выполнял этот план")):
        return (
            "Сто процентов поведения после выезда я не обещаю; механика в том, чтобы зафиксировать конкретное правило, критерий проверки и внешнюю рамку на 90 дней. "
            "Какой один сигнал в бизнесе покажет вам, что эта рамка реально работает?"
        )
    if any(marker in last_buyer for marker in ("telegram", "телеграм", "скиньте", "пришлите", "материал", "ссылк")):
        return "Да, отправляю материал в Telegram прямо сейчас и дальше без звонков, пока вы сами не вернетесь."
    if any(marker in last_buyer for marker in ("сам дам знать", "не пишите", "не звоните", "потом сам")):
        return "Принял, больше не тревожу; материал отправил, дальше инициатива за вами."
    if any(
        marker in last_buyer
        for marker in (
            "внедрение под ключ",
            "внешний аудит",
            "готовое решение",
            "возврат",
            "вернете",
            "вернуть",
            "штраф",
            "расторж",
            "перезаключ",
            "пункт в договор",
            "если нет",
            "не мой формат",
            "гарантия результата",
            "гарантированный результат",
        )
    ):
        return (
            "Понял вас: договорные штрафы или возврат за бизнес-результат я сейчас обещать не могу. "
            "Могу отправить типовые условия и отдельно уточнить у команды, есть ли фиксируемый формат поддержки."
        )
    if stage.stage == "S3.5":
        return "Понял причину отказа и не буду давить; какой один материал был бы полезен вам после разговора?"
    candidate = speakable_seller_text(stage)
    if seller_text_is_meta(candidate):
        return safe_stage_fallback(stage)
    return candidate


def seller_text_is_meta(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:выяснить|добрать|классифицировать|дать короткий|feature\s*->|check-in|next_action)\b",
            text,
            re.I,
        )
    )


def safe_stage_fallback(stage: StageSnapshot) -> str:
    if stage.stage == "S2.1":
        return "Коротко обозначу формат и задам один вопрос, чтобы не тратить ваше время."
    if stage.stage == "S2.2":
        return "Подскажите, какая текущая бизнес-задача сейчас сильнее всего требует внимания?"
    if stage.stage == "S2.3":
        return "Какой результат за ближайшие 90 дней для вас был бы доказательством, что разговор был полезен?"
    if stage.stage == "S2.4":
        return "Почему вы вообще решили рассмотреть такой формат именно сейчас?"
    if stage.stage == "S2.5":
        return "Если коротко резюмировать ваш запрос, можно я покажу, как формат выезда может под него лечь?"
    if stage.stage == "S3.1":
        return "Покажу формат коротко: 4 дня в группе вашего уровня, живые разборы, личная декларация и цели на 90 дней."
    if stage.stage == "S3.2":
        return "Насколько такой формат в принципе попадает в вашу задачу, если не обсуждать цену?"
    if stage.stage == "S3.3":
        return "Участие стоит 99 000 рублей; как вам такая стоимость относительно задачи?"
    if stage.stage == "S3.4b":
        return "Давайте без давления: можем поставить короткий второй созвон и спокойно разобрать оставшиеся вопросы."
    return "Понял вас; давайте зафиксируем следующий конкретный шаг без давления."


def parse_buyer_response(text: str) -> tuple[str, str]:
    value = extract_json_object(text)
    if isinstance(value, dict):
        state = str(value.get("state") or "").strip()
        reply = str(value.get("text") or "").strip()
        if reply:
            return state or "answered", sanitize_reply(reply)
    return "raw", sanitize_reply(text)


def sanitize_reply(text: str) -> str:
    value = strip_markdown(text)
    value = value.strip().strip("\"'`«»“”").strip()
    value = re.sub(r"^(?:клиент|покупатель|client|buyer)\s*[:：-]\s*", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def fallback_buyer_reply(*, profile: BuyerProfile, turn_index: int, error: str) -> str:
    seed_lines = [
        line
        for line in profile.seed_client_lines
        if not re.fullmatch(r"\[[^\]]+\]\.?", line.strip())
    ]
    if seed_lines:
        seed = seed_lines[min(turn_index // 2, len(seed_lines) - 1)]
        return sanitize_reply(seed)
    return (
        "Понял, но я пока не до конца уверен, что это решает именно мою задачу. "
        f"(buyer fallback: {error[:160]})"
    )


def detect_terminal_outcome(
    *,
    buyer_text: str,
    seller_text: str,
    stage: StageSnapshot,
    turn_index: int,
    max_pairs: int,
) -> tuple[str | None, str]:
    text = buyer_text.lower().replace("ё", "е")
    seller = seller_text.lower().replace("ё", "е")

    won_markers = (
        "готов оплатить",
        "готов участвовать",
        "давайте оформ",
        "давай оформ",
        "давайте переходить к оплат",
        "перейдем к оплат",
        "брониру",
        "бронь",
        "для брони",
        "что нужно для брони",
        "фиксируем место",
        "кидайте ссылку",
        "пришлите счет",
        "счет на оплат",
        "к оплате",
        "оплачу",
    )
    if any(marker in text for marker in won_markers):
        return "won_payment_intent", "Buyer gave payment/booking intent."

    scheduled_markers = (
        "договорились",
        "созвон",
        "встреч",
        "в среду",
        "в четверг",
        "завтра",
        "перешлю",
        "скиньте в telegram",
        "скиньте в телеграм",
        "пришлите условия",
        "пришлите договор",
        "жду материал",
        "номер в профиле",
        "сам напишу",
        "сама напишу",
        "теперь тихо",
        "до связи",
        "увидимся",
    )
    if stage.stage in {"S3.4b", "S3.5"} and any(marker in text for marker in scheduled_markers):
        return "next_step_scheduled", "Buyer accepted concrete follow-up or stakeholder step."

    concrete_followup_markers = (
        "договорились",
        "до связи",
        "до четверг",
        "до встречи",
        "увидимся",
        "жду ссыл",
        "жду материал",
        "буду ждать",
        "жду именно",
        "жду в telegram",
        "жду в телеграм",
        "когда появится",
        "присылайте",
        "посмотрю",
        "скиньте в telegram",
        "скиньте в телеграм",
        "пришлите условия",
        "пришлите договор",
        "так и договорились",
        "сам напишу",
        "сама напишу",
    )
    seller_followup_markers = (
        "созвон",
        "встреч",
        "четверг",
        "завтра",
        "telegram",
        "телеграм",
        "ссыл",
        "материал",
        "приглаш",
        "отправ",
        "до встречи",
    )
    terse_channel_accept = text.strip(" .!?,;:") in {"telegram", "телеграм"}
    if (terse_channel_accept or any(marker in text for marker in concrete_followup_markers)) and any(
        marker in seller for marker in seller_followup_markers
    ):
        return "next_step_scheduled", "Buyer accepted concrete follow-up or stakeholder step."

    polite_close_markers = ("пока", "хорошо, спасибо", "ок, увидел", "спасибо за контакты")
    followup_seller_markers = ("telegram", "телеграм", "контакт", "материал", "ссылк")
    if (
        stage.stage == "S3.5"
        and any(marker in text for marker in polite_close_markers)
        and any(marker in seller for marker in followup_seller_markers)
    ):
        return "next_step_scheduled", "Buyer accepted concrete follow-up or stakeholder step."

    silent_end_markers = ("гудки", "тишина")
    if stage.stage == "S3.5" and any(marker in text for marker in silent_end_markers):
        return "next_step_scheduled", "Call ended after follow-up handoff."

    qualified_refusal_markers = (
        "ищу решение, а не семинар",
        "это не мой формат",
        "не тот формат",
        "не подходит, я ищу",
        "мне это не подходит",
        "нам не по пути",
    )
    if any(marker in text for marker in qualified_refusal_markers) and not is_active_objection(text):
        return "qualified_refusal", "Buyer declined after clarifying a value/fit gap."

    lost_markers = (
        "точно нет",
        "вешаю трубку",
        "закончим",
        "не тратьте время",
        "до свидания",
        "не звоните",
    )
    if any(marker in text for marker in lost_markers) and not is_active_objection(text):
        return "closed_lost", "Buyer gave explicit refusal or ended the call."

    soft_lost_markers = (
        "не интересно",
        "не подходит",
        "не буду",
        "не готов",
    )
    if any(marker in text for marker in soft_lost_markers) and not is_active_objection(text):
        return "closed_lost", "Buyer gave explicit refusal or ended the call."

    if turn_index >= max_pairs:
        if any(
            marker in seller
            for marker in (
                "оплат",
                "счет",
                "ссылка",
                "договор",
                "созвон",
                "telegram",
                "телеграм",
                "контакт",
                "материал",
            )
        ):
            return "unresolved_with_next_step_attempt", "Reply limit reached after seller attempted a close."
        return "max_replies_reached", "Reply limit reached without terminal buyer signal."

    return None, ""


def is_active_objection(text: str) -> bool:
    if "?" in text:
        return True
    markers = (
        "если",
        "только если",
        "но",
        "как именно",
        "механизм",
        "объясните",
        "докажите",
        "покажите",
    )
    return any(marker in text for marker in markers)


class PaperRoleplayGenerator:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ):
        self.orchestrator = LlmOrchestrator(settings, client=client)
        self.settings = settings

    async def aclose(self) -> None:
        await self.orchestrator.aclose()

    async def generate_many(
        self,
        *,
        profiles: list[BuyerProfile],
        event_facts: str,
        config: RoleplayConfig,
    ) -> list[RoleplayResult]:
        semaphore = asyncio.Semaphore(max(config.concurrency, 1))

        async def run_one(profile: BuyerProfile) -> RoleplayResult:
            async with semaphore:
                return await self.generate_one(
                    profile=profile,
                    event_facts=event_facts,
                    config=config,
                )

        return list(await asyncio.gather(*(run_one(profile) for profile in profiles)))

    async def generate_one(
        self,
        *,
        profile: BuyerProfile,
        event_facts: str,
        config: RoleplayConfig,
    ) -> RoleplayResult:
        started_at = time.monotonic()
        run_id = f"{config.run_id_prefix}-{profile.number:02d}-{int(time.time())}"
        history: list[tuple[str, str]] = []
        turns: list[RoleplayTurn] = []
        current_stage: str | None = None
        terminal_outcome = "max_replies_reached"
        terminal_reason = f"Reached {config.max_replies} reply limit without terminal buyer signal."
        max_pairs = min(config.turn_pairs, max(config.max_replies // 2, 1))

        for turn_index in range(1, max_pairs + 1):
            turn_started_at = time.monotonic()
            stage = await self._stage_decision(
                run_id=run_id,
                event_facts=event_facts,
                profile=profile,
                history=history,
                current_stage=current_stage,
                history_lines=config.history_lines,
                stage_provider=config.stage_provider,
            )
            current_stage = stage.stage
            seller_text = await self._seller_turn(
                run_id=run_id,
                event_facts=event_facts,
                profile=profile,
                history=history,
                stage=stage,
                config=config,
            )
            history.append(("Продавец", seller_text))

            buyer_state, buyer_text = await self._buyer_turn(
                run_id=run_id,
                event_facts=event_facts,
                profile=profile,
                history=history,
                stage=stage,
                seller_text=seller_text,
                config=config,
            )
            history.append(("Клиент", buyer_text))
            detected_outcome, detected_reason = detect_terminal_outcome(
                buyer_text=buyer_text,
                seller_text=seller_text,
                stage=stage,
                turn_index=turn_index,
                max_pairs=max_pairs,
            )
            if detected_outcome:
                terminal_outcome = detected_outcome
                terminal_reason = detected_reason

            turns.append(
                RoleplayTurn(
                    index=turn_index,
                    stage=stage,
                    seller_text=seller_text,
                    buyer_text=buyer_text,
                    buyer_state=buyer_state,
                    terminal_outcome=detected_outcome,
                    elapsed_ms=int((time.monotonic() - turn_started_at) * 1000),
                )
            )
            if config.stop_on_terminal and detected_outcome:
                break

        return RoleplayResult(
            profile=profile,
            turns=tuple(turns),
            event_facts=event_facts,
            run_id=run_id,
            terminal_outcome=terminal_outcome,
            terminal_reason=terminal_reason,
            elapsed_secs=time.monotonic() - started_at,
        )

    async def _stage_decision(
        self,
        *,
        run_id: str,
        event_facts: str,
        profile: BuyerProfile,
        history: list[tuple[str, str]],
        current_stage: str | None,
        history_lines: int,
        stage_provider: str = "auto",
    ) -> StageSnapshot:
        if stage_provider in {"local", "heuristic"}:
            return heuristic_stage_snapshot(current_stage, history)
        context = build_stage_context(
            event_facts=event_facts,
            profile=profile,
            history=history,
            history_lines=history_lines,
        )
        try:
            if stage_provider == "cerebras" and self.orchestrator.cerebras.configured():
                return await self._stage_decision_cerebras(
                    run_id=run_id,
                    context=context,
                    current_stage=current_stage,
                )
            response = await self.orchestrator.stage_agenda(
                StageRequest(
                    run_id=run_id,
                    context=context,
                    current_stage=current_stage,
                )
            )
        except (ProviderError, ValueError, json.JSONDecodeError, httpx.HTTPError) as exc:
            return heuristic_stage_snapshot(current_stage, history, str(exc))
        if response is None:
            return heuristic_stage_snapshot(current_stage, history, "stage pipeline returned no update")
        return stage_snapshot_from_response(response, current_stage)

    async def _stage_decision_cerebras(
        self,
        *,
        run_id: str,
        context: str,
        current_stage: str | None,
    ) -> StageSnapshot:
        request = StageRequest(
            run_id=run_id,
            context=context,
            current_stage=current_stage,
        )
        model = self.settings.help_opener_secondary_model
        started_at = time.monotonic()
        user_content = (
            f"{context}\n\n"
            f"--- Текущий stage из предыдущего шага ---\n"
            f"{current_stage or '(пока неизвестен)'}\n"
        )
        text = await self.orchestrator.cerebras.text(
            model=model,
            system_prompt=stage_detection_system_prompt(),
            user_content=user_content,
            temperature=0.1,
            prompt_cache_key=f"rec-sidecar-paper-stage-detect-v1-{run_id}",
        )
        stage, confidence = parse_stage_detection(text)
        stage = self.orchestrator._clamp_detected_stage(request, stage, model)
        response = await self.orchestrator._stage_response(
            request=request,
            stage=stage,
            confidence=confidence,
            provider="cerebras",
            model=model,
            detect_elapsed_ms=int((time.monotonic() - started_at) * 1000),
        )
        return stage_snapshot_from_response(response, current_stage)

    async def _seller_turn(
        self,
        *,
        run_id: str,
        event_facts: str,
        profile: BuyerProfile,
        history: list[tuple[str, str]],
        stage: StageSnapshot,
        config: RoleplayConfig,
    ) -> str:
        if not config.use_seller_agent:
            return speakable_seller_text(stage)
        user_content = build_seller_prompt(
            event_facts=event_facts,
            profile=profile,
            history=history,
            stage=stage,
            history_lines=config.history_lines,
        )
        try:
            text = await self._text(
                model=config.seller_model or self.settings.cerebras_model,
                system_prompt=SELLER_SYSTEM_PROMPT,
                user_content=user_content,
                temperature=config.seller_temperature,
                max_tokens=config.seller_max_tokens,
                prompt_cache_key=f"rec-sidecar-paper-seller-v1-{run_id}",
            )
            candidate = parse_seller_response(text)
            if seller_reply_is_usable(candidate, history):
                return candidate
            return seller_fallback_reply(stage, history)
        except Exception:
            return seller_fallback_reply(stage, history)

    async def _buyer_turn(
        self,
        *,
        run_id: str,
        event_facts: str,
        profile: BuyerProfile,
        history: list[tuple[str, str]],
        stage: StageSnapshot,
        seller_text: str,
        config: RoleplayConfig,
    ) -> tuple[str, str]:
        user_content = build_buyer_prompt(
            event_facts=event_facts,
            profile=profile,
            history=history,
            stage=stage,
            seller_text=seller_text,
            history_lines=config.history_lines,
        )
        try:
            text = await self._text(
                model=config.buyer_model or self.settings.cerebras_model,
                system_prompt=BUYER_SYSTEM_PROMPT,
                user_content=user_content,
                temperature=config.buyer_temperature,
                max_tokens=config.buyer_max_tokens,
                prompt_cache_key=f"rec-sidecar-paper-buyer-v1-{run_id}",
            )
            return parse_buyer_response(text)
        except Exception as exc:
            return (
                "buyer fallback",
                fallback_buyer_reply(profile=profile, turn_index=len(history), error=str(exc)),
            )

    async def _text(
        self,
        *,
        model: str,
        system_prompt: str,
        user_content: str,
        temperature: float,
        max_tokens: int,
        prompt_cache_key: str | None,
    ) -> str:
        if self.settings.provider in {"vertex", "gemini", "google"} and self.orchestrator.vertex.configured():
            return await self._vertex_text(
                system_prompt=system_prompt,
                user_content=user_content,
                temperature=temperature,
            )
        if self.orchestrator.cerebras.configured():
            try:
                return await self.orchestrator.cerebras.text(
                    model=model,
                    system_prompt=system_prompt,
                    user_content=user_content,
                    temperature=temperature,
                    prompt_cache_key=prompt_cache_key,
                    max_tokens=max_tokens,
                )
            except ProviderError as exc:
                if exc.is_rate_limit and self.orchestrator.vertex.configured():
                    return await self._vertex_text(
                        system_prompt=system_prompt,
                        user_content=user_content,
                        temperature=temperature,
                    )
                raise
        if self.orchestrator.vertex.configured():
            return await self._vertex_text(
                system_prompt=system_prompt,
                user_content=user_content,
                temperature=temperature,
            )
        raise ProviderError("service", "no LLM provider configured for paper roleplay")

    async def _vertex_text(
        self,
        *,
        system_prompt: str,
        user_content: str,
        temperature: float,
    ) -> str:
        parts: list[str] = []
        async for delta in self.orchestrator.vertex.stream_text(
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
            thinking_level=self.settings.vertex_thinking_level,
        ):
            parts.append(delta)
        text = "".join(parts).strip()
        if text:
            return text
        raise ProviderError("vertex", "empty paper roleplay text response")


def render_roleplay_markdown(result: RoleplayResult) -> str:
    lines: list[str] = [
        f"# Paper roleplay {result.profile.number:02d}: {result.profile.title}",
        "",
        f"- Run id: `{result.run_id}`",
        f"- Buyer persona: {result.profile.persona}",
        f"- Terminal outcome: `{result.terminal_outcome}`",
        f"- Terminal reason: {result.terminal_reason}",
        f"- Reply count: {len(result.turns) * 2}",
        f"- Elapsed: {result.elapsed_secs:.1f}s",
        "",
        "## Complex Buyer Profile",
        "",
        complex_buyer_profile(result.profile),
        "",
        "## Shared Agenda",
        "",
        result.event_facts,
        "",
        "## Dialogue",
        "",
    ]
    for turn in result.turns:
        stage = turn.stage
        score = "" if stage.score is None else f" · score {stage.score:.2f}"
        lines.extend(
            [
                f"### Turn {turn.index:02d} · {stage.stage} {stage.title}",
                "",
                (
                    f"- Stage: `{stage.stage}` · {stage.readiness_label}"
                    f"{score} · {stage.provider}/{stage.model}"
                ),
                f"- Agenda: {stage.agenda}",
                f"- Scorecard: {stage.summary or 'n/a'}",
                f"- UI next_action: {stage.next_action or stage.step}",
            ]
        )
        if stage.error:
            lines.append(f"- Error: {stage.error}")
        if stage.checks:
            lines.append("- Checks:")
            for check in stage.checks[:8]:
                lines.append(f"  - {check}")
        lines.extend(
            [
                "",
                f"**Продавец:** {turn.seller_text}",
                "",
                f"**Покупатель:** {turn.buyer_text}",
                "",
                f"_Buyer state: {turn.buyer_state}; turn elapsed: {turn.elapsed_ms} ms_",
                "",
            ]
        )
        if turn.terminal_outcome:
            lines.extend(
                [
                    f"_Terminal outcome: {turn.terminal_outcome}_",
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def write_roleplay_outputs(results: list[RoleplayResult], output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for result in results:
        path = output_dir / f"scenario_{result.profile.number:02d}_{result.profile.slug}.md"
        path.write_text(render_roleplay_markdown(result), encoding="utf-8")
        paths.append(path)
    index_path = output_dir / "index.md"
    index_path.write_text(render_index_markdown(results), encoding="utf-8")
    paths.append(index_path)
    return paths


def render_index_markdown(results: list[RoleplayResult]) -> str:
    lines = ["# Paper roleplay index", "", "## Metrics", ""]
    lines.extend(render_metrics_lines(results))
    lines.extend(["", "## Scenarios", ""])
    for result in results:
        final_stage = result.turns[-1].stage if result.turns else None
        final_label = f"{final_stage.stage} {final_stage.readiness_label}" if final_stage else "no turns"
        filename = f"scenario_{result.profile.number:02d}_{result.profile.slug}.md"
        lines.append(
            f"- [{result.profile.number:02d}. {result.profile.title}]({filename}) "
            f"- {len(result.turns)} turns/{len(result.turns) * 2} replies, "
            f"final {final_label}, outcome `{result.terminal_outcome}`, {result.elapsed_secs:.1f}s"
        )
    return "\n".join(lines).rstrip() + "\n"


def render_metrics_lines(results: list[RoleplayResult]) -> list[str]:
    if not results:
        return ["- No results."]
    outcome_counts: dict[str, int] = {}
    readiness_counts: dict[str, int] = {}
    hit_count = 0
    miss_count = 0
    incomplete_count = 0
    total_checks = 0
    total_turns = 0
    terminal_success = {"won_payment_intent", "next_step_scheduled"}

    for result in results:
        outcome_counts[result.terminal_outcome] = outcome_counts.get(result.terminal_outcome, 0) + 1
        total_turns += len(result.turns)
        for turn in result.turns:
            label = turn.stage.readiness_label
            readiness_counts[label] = readiness_counts.get(label, 0) + 1
            for check in turn.stage.checks:
                total_checks += 1
                if "/hit:" in check:
                    hit_count += 1
                elif "/miss:" in check:
                    miss_count += 1
                elif "/pending:" in check or "/uncertain:" in check:
                    incomplete_count += 1

    success_count = sum(outcome_counts.get(outcome, 0) for outcome in terminal_success)
    avg_replies = round((total_turns * 2) / len(results), 1)
    hit_ratio = (
        round(hit_count / (hit_count + miss_count), 2)
        if hit_count + miss_count
        else None
    )
    outcomes = ", ".join(
        f"`{key}`={value}" for key, value in sorted(outcome_counts.items())
    )
    readiness = ", ".join(
        f"`{key}`={value}" for key, value in sorted(readiness_counts.items())
    )
    lines = [
        f"- Scenarios: {len(results)}",
        f"- Dozhim outcomes: {success_count}/{len(results)}",
        f"- Average replies: {avg_replies}",
        f"- Outcomes: {outcomes or 'n/a'}",
        f"- Readiness labels: {readiness or 'n/a'}",
        f"- Check hit/(hit+miss): {hit_ratio if hit_ratio is not None else 'n/a'} "
        f"({hit_count}/{hit_count + miss_count})",
        f"- Incomplete checks: {incomplete_count}",
        f"- Total scored checks: {total_checks}",
    ]
    return lines
