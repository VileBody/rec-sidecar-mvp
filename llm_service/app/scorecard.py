from __future__ import annotations

import json
import re
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
    quote: str = ""


class RawScoreCheck(BaseModel):
    id: str
    result: Literal["hit", "miss", "pending", "uncertain", "na"]
    reason: str = Field(default="", min_length=1)
    evidence: list[RawScoreEvidence] = []


class RawScorecard(BaseModel):
    summary: str = Field(default="", min_length=1)
    next_action: str = Field(default="", min_length=1)
    checks: list[RawScoreCheck] = []


ALLOWED_TRANSITIONS: dict[str, str] = {
    "S2.1": "S2.2",
    "S2.2": "S2.3",
    "S2.3": "S2.4",
    "S2.4": "S2.5",
    "S2.5": "S3.1",
    "S3.1": "S3.2",
    "S3.2": "S3.3",
}

STICKY_TEMPLATE_MARKERS = (
    "очень крутая цель",
    "это реально сделать",
    "приятно слышать",
    "вы абсолютно правы",
    "я как раз помогаю с этим",
    "цена или ценность продукта",
    "хочу рассказать подробнее про свой формат",
)

UNVERIFIED_FACT_MARKERS = (
    "менторы-строители",
    "юрист-практик",
    "юрист практик",
    "факторинг",
    "оборот от",
    "гарантирован",
    "гарантируем",
    "точно внедр",
    "подберем контакты",
    "готовые скрипты переговоров",
    "еженедельно трекать",
    "еженедельно трека",
    "еженедельно отслеживать",
    "еженедельные отчеты",
    "еженедельные отчёты",
    "внешний совет директоров",
    "жесткая обратная связь",
    "жесткую обратную связь",
    "чек-лист отчетности",
    "цена слова",
    "внешний аудитор",
    "личный трекер",
    "трекер",
    "службу безопасности",
    "служба безопасности",
    "стресс-интервью",
    "стресс интервью",
    "ментор-практик по найму",
    "ментором по найму",
    "ментор по найму",
    "ментором-практиком",
    "ментор-практик",
    "проверку рекомендаций",
    "перекрестной проверки",
    "перекрестная проверка",
    "матрице верификации",
    "матрица верификации",
    "маржинальности",
    "триггеры лжи",
    "чек-лист верификации",
    "внешний аудит",
    "внешнего аудита",
    "внешнего аудитора",
    "внешним ассистентом",
    "ассистентом за",
    "ежедневный аудит",
    "ежедневным аудитом",
    "штраф из своего бонуса",
    "общий штраф",
    "штрафы",
    "кейс агентства недвижимости",
    "кейсе агентства",
    "старшему риелтору",
    "менторы-предприниматели",
    "построили автономные компании",
    "агентские сети",
    "строил агентства",
    "кейс нашего ментора",
)

SCORECARD_BLOCK_START = "<<<SCORECARD>>>"
SCORECARD_BLOCK_END = "<<<END_SCORECARD>>>"
SCORECARD_FIELD_RE = re.compile(r"^([A-Za-z_][\w.]*)\s*(?::|-\s)\s*(.+?)\s*$")

DIRECT_QUESTION_MARKERS = (
    "что это",
    "что за",
    "как именно",
    "как работает",
    "как устроен",
    "как устроена",
    "как устроено",
    "каким образом",
    "механизм",
    "контроль какой",
    "какой контроль",
    "кто гарант",
    "гаранти",
    "гарантиру",
    "гарантия",
    "гарантии",
    "давайте факты",
    "факты",
    "есть кейс",
    "кейс",
    "внешний аудит",
    "внешнего аудита",
    "докаж",
    "по шагам",
    "кто провер",
    "что происходит при",
    "коротко",
    "по сути",
    "цена",
    "сколько стоит",
    "скиньте",
    "в telegram",
    "в телеграм",
    "одну минуту",
    "ровно минуту",
    "у меня мало времени",
)

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
            "Клиент дал явный интерес, согласие или уточнение по применению без открытого proof-вопроса.",
            "Клиент отвечает нейтрально/условно: «теоретически», «звучит логично, но», «если это не болтовня» или просит доказать механизм.",
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
            "value_active_objection_handled",
            "Активное сомнение закрыто",
            "quality",
            "focus",
            "Если клиент задал proof-вопрос, продавец дал короткий ответ по механике или честно признал границу обещания.",
            "Продавец игнорирует вопрос «как именно/механизм какой» или снова обещает показать позже.",
        ),
    ),
    "S3.3": (
        ScoreCheckDefinition(
            "bank_value_before_price",
            "Value до цены",
            "core",
            "transition",
            "Цена/условия обсуждаются после подтверждения ценности или явного интереса.",
            "Цена/условия предложены до value test.",
        ),
        ScoreCheckDefinition(
            "bank_clear_terms",
            "Условия ясны",
            "core",
            "specificity",
            "Стоимость, формат участия и ближайший шаг объяснены конкретно.",
            "Цена/условия участия мутные или клиент их не понял.",
        ),
        ScoreCheckDefinition(
            "bank_reaction_check",
            "Реакция на оплату",
            "core",
            "dialogue",
            "Продавец спокойно спросил реакцию на стоимость/условия.",
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
    allowed_transition = ALLOWED_TRANSITIONS.get(stage, "следующая уместная стадия")
    return f"""Ты realtime evaluator и tactical sales coach для high-ticket B2C sales call на русском.
Оцени только текущую стадию {stage} — {agenda.title}.

Agenda стадии: {agenda.agenda}
Intent эмоциональной реакции, НЕ фраза для копирования: {agenda.emotion}
Intent следующего шага, НЕ фраза для копирования: {agenda.step}
Allowed transition при green: {stage} -> {allowed_transition}

Правила:
- Верни только текстовый блок без JSON, Markdown и пояснений.
- Формат ответа строго такой:
  {SCORECARD_BLOCK_START}
  summary: <короткий вывод>
  next_action: <одна короткая фраза продавца, с префиксом Уточнить: или Переход:>
  check: <id> | <result> | <короткая причина> | <необязательная короткая цитата>
  check: <id> | <result> | <короткая причина> |
  {SCORECARD_BLOCK_END}
- Для каждого check id из списка верни ровно одну строку `check:`.
- Каждый check оцени как hit, miss, pending, uncertain или na.
- hit/miss ставь только при наличии evidence из диалога.
- Не награждай продавца за красивые слова без buyer evidence.
- Если данных ещё мало, ставь pending, а не miss.
- Любой core check со статусом miss/pending/uncertain означает, что стадия НЕ готова.
- Для miss/hit дай короткую причину и 0-2 короткие цитаты evidence.
- next_action — НЕ пересказывай пример/intent статично; дай живую фразу продавца по текущему диалогу.
- next_action обязан зацепиться за buyer_anchor: конкретное слово, факт или формулировку клиента из последних реплик.
- Нельзя использовать больше 4 подряд слов из intent эмоциональной реакции или intent следующего шага.
- Если readiness по смыслу red/yellow: next_action начинается с "Уточнить:" и после префикса содержит одну готовую фразу-вопрос клиенту.
- Если readiness по смыслу green: next_action начинается с "Переход:" и после префикса содержит готовую фразу перехода только в allowed transition.
- S2.2 green ведет только к S2.3; S2.3 green ведет только к S2.4; запрещено питчить/презентовать из S2.2 или S2.3.
- Если клиент прямо спрашивает о продукте/цене/формате или просит "коротко/по сути/что это/скиньте": сначала дай 1 короткий прямой ответ, затем максимум 1 мягкий вопрос.
- Если клиент спрашивает "как именно", "механизм какой", "как работает контроль", "кто гарантирует": это direct-answer debt. Нельзя отвечать только вопросом или "давайте покажу"; сначала дай 1 короткую механику из подтвержденной фактуры, затем 1 проверочный вопрос.
- Для скептичного клиента после курсов minimum proof chain: диагностика/разбор его ситуации -> конкретный артефакт/правило -> кто участвует -> что остается после выезда. Не выдумывай частоту контроля, гарантии или аудиторов.
- S3.2 green запрещен на условных фразах клиента "теоретически", "звучит логично, но", "если это не болтовня" или при незакрытом proof-вопросе.
- Если S3.2 действительно green: next_action обязан двигать в S3.3 и назвать цену/условия участия или формат оплаты; нельзя снова писать "давайте покажу, как устроено".
- Если клиент раздражен вопросами ("я просил", "хватит", "время теряю", "вешаю трубку"), readiness не может быть green; нужно восстановить доверие или коротко ответить.
- Не обещай факты, которых нет в brief/диалоге: отраслевых менторов, трекеров, юристов, службу безопасности, стресс-интервью, факторинг, оборот группы, гарантированные контакты, договоры или результат.
- После префикса пиши именно слова продавца клиенту от первого лица, пригодные для чтения вслух прямо сейчас.
- Не пиши мета-инструкции вроде "спроси", "уточни", "скажи клиенту", "дай аргумент", не делай список 1/2/3.
- Не пиши длинный разбор; next_action должен быть коротким, читаемым с экрана и пригодным сказать вслух.
- Хорошие форматы: "Уточнить: Вы сказали, что люди сливаются; на каком этапе это чаще всего происходит?" или "Переход: Вы описали хаос в аккаунтинге и цель 10 часов операционки; покажу, как формат разбора работает именно с этим?"
- Не считай секунды, проценты и talk ratio сам; оцени смысловые признаки из текста.

Checks текущей стадии:
{checks}
"""


def scorecard_advice_prompt(stage: str, agenda: StageAgenda) -> str:
    allowed_transition = ALLOWED_TRANSITIONS.get(stage, "следующая уместная стадия")
    return f"""Ты tactical sales coach для high-ticket B2C sales call на русском.
Стадия: {stage} — {agenda.title}
Agenda: {agenda.agenda}
Intent следующего шага, НЕ копировать дословно: {agenda.step}
Allowed transition при green: {stage} -> {allowed_transition}

Верни только одну короткую строку next_action, без JSON и markdown.
Если данных не хватает или рано переходить: начни с "Уточнить:" и дай одну готовую фразу-вопрос клиенту.
Если можно двигаться дальше: начни с "Переход:" и дай готовую фразу перехода на следующую стадию.
S2.2/S2.3 не могут переходить в pitch. Если клиент просит "коротко/что это/цена/по сути", сначала ответь прямо одной фразой, потом один мягкий вопрос.
Если клиент спрашивает "как именно/механизм/контроль/кто гарантирует", сначала дай короткую механику из фактуры, а не обещание "показать позже".
Если стадия S3.2 готова к переходу, next_action должен перейти к цене/условиям участия, а не снова доказывать ценность.
Фраза обязана содержать buyer_anchor: конкретное слово или факт клиента. Не обещай факты, которых нет в brief/диалоге.
Запрещено выдумывать трекеров, службу безопасности, стресс-интервью, отраслевых менторов, гарантии и закрытые методики, если этого нет в brief.
После префикса пиши именно слова продавца клиенту от первого лица, пригодные для чтения вслух прямо сейчас; не пиши "спроси", "уточни", "скажи клиенту" и не делай список.
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
    contract_value = parse_scorecard_contract(text)
    if contract_value is not None:
        return RawScorecard.model_validate(coerce_raw_scorecard_value(contract_value))

    value = _extract_json_object(text)
    if not isinstance(value, dict):
        raise ValueError(f"scorecard response is not JSON object: {text!r}")
    return RawScorecard.model_validate(coerce_raw_scorecard_value(value))


def parse_scorecard_contract(text: str) -> dict[str, Any] | None:
    block = _extract_contract_block(text, SCORECARD_BLOCK_START, SCORECARD_BLOCK_END)
    candidate = block if block is not None else text.strip()
    if not candidate:
        return None

    summary = ""
    next_action = ""
    checks: list[dict[str, Any]] = []
    saw_contract_field = False

    for raw_line in candidate.splitlines():
        line = raw_line.strip().strip("`")
        if not line:
            continue
        match = SCORECARD_FIELD_RE.match(line)
        if not match:
            continue

        key = match.group(1).lower()
        value = match.group(2).strip()
        if key in {"summary", "next_action", "check"} or key.startswith("check."):
            saw_contract_field = True

        if key == "summary":
            summary = value
            continue
        if key in {"next_action", "advice", "recommendation"}:
            next_action = value
            continue
        if key == "check":
            checks.append(parse_contract_check(value))
            continue
        if key.startswith("check."):
            checks.append(parse_contract_check(value, check_id=key.split(".", 1)[1]))

    if not saw_contract_field:
        return None
    return {"summary": summary, "next_action": next_action, "checks": checks}


def parse_contract_check(value: str, *, check_id: str | None = None) -> dict[str, Any]:
    parts = [segment.strip() for segment in value.split("|")]
    if check_id is None:
        raw_id = parts[0] if parts else "unknown"
        raw_result = parts[1] if len(parts) > 1 else ""
        raw_reason = parts[2] if len(parts) > 2 else ""
        raw_evidence = parts[3:] if len(parts) > 3 else []
    else:
        raw_id = check_id
        raw_result = parts[0] if parts else ""
        raw_reason = parts[1] if len(parts) > 1 else ""
        raw_evidence = parts[2:] if len(parts) > 2 else []

    evidence = [{"quote": quote} for quote in split_contract_evidence(raw_evidence) if quote]
    return {
        "id": raw_id or "unknown",
        "result": normalize_check_result(raw_result) or "uncertain",
        "reason": raw_reason or "Оценено моделью без подробной причины.",
        "evidence": evidence,
    }


def split_contract_evidence(parts: list[str]) -> list[str]:
    if not parts:
        return []

    evidence: list[str] = []
    for part in parts:
        for quote in part.split("||"):
            cleaned = quote.strip().strip('"')
            if cleaned:
                evidence.append(cleaned)
    return evidence


def coerce_raw_scorecard_value(value: dict[str, Any]) -> dict[str, Any]:
    scorecard = value.get("scorecard")
    if isinstance(scorecard, dict):
        value = {**value, **scorecard}

    checks_value = value.get("checks")
    if isinstance(checks_value, dict):
        checks = [
            coerce_raw_check_value(check_id, check_value)
            for check_id, check_value in checks_value.items()
        ]
    elif isinstance(checks_value, list):
        checks = [coerce_raw_check_value(None, check_value) for check_value in checks_value]
    else:
        checks = []

    if not checks:
        skipped = {"summary", "next_action", "checks", "scorecard", "stage", "confidence"}
        for check_id, check_value in value.items():
            if check_id in skipped:
                continue
            if is_check_like_value(check_value):
                checks.append(coerce_raw_check_value(check_id, check_value))

    summary = (
        text_field(value.get("summary"))
        or text_field(value.get("reason"))
        or "Оценка по чеклисту."
    )
    next_action = (
        text_field(value.get("next_action"))
        or text_field(value.get("advice"))
        or text_field(value.get("recommendation"))
        or "Уточнить: добрать недостающие факты по текущей стадии."
    )
    return {"summary": summary, "next_action": next_action, "checks": checks}


def is_check_like_value(value: Any) -> bool:
    if isinstance(value, str):
        return normalize_check_result(value) is not None
    if not isinstance(value, dict):
        return False
    return any(key in value for key in ("result", "status", "state", "decision", "hit"))


def coerce_raw_check_value(check_id: str | None, value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        result = normalize_check_result(value) or "uncertain"
        return {
            "id": check_id or "unknown",
            "result": result,
            "reason": "Оценено моделью без подробной причины.",
            "evidence": [],
        }
    if not isinstance(value, dict):
        return {
            "id": check_id or "unknown",
            "result": "uncertain",
            "reason": "Модель вернула неполную оценку.",
            "evidence": [],
        }

    result = (
        normalize_check_result(value.get("result"))
        or normalize_check_result(value.get("status"))
        or normalize_check_result(value.get("state"))
        or normalize_check_result(value.get("decision"))
        or normalize_check_result(value.get("hit"))
        or "uncertain"
    )
    reason = (
        text_field(value.get("reason"))
        or text_field(value.get("rationale"))
        or text_field(value.get("explanation"))
        or "Оценено по диалогу."
    )
    return {
        "id": text_field(value.get("id")) or check_id or "unknown",
        "result": result,
        "reason": reason,
        "evidence": coerce_evidence(value.get("evidence"))
        or coerce_evidence(value.get("quote"))
        or coerce_evidence(value.get("text")),
    }


def normalize_check_result(value: Any) -> str | None:
    if isinstance(value, bool):
        return "hit" if value else "miss"
    if value is None:
        return None
    normalized = str(value).strip().lower().replace("-", "_")
    aliases = {
        "yes": "hit",
        "true": "hit",
        "passed": "hit",
        "pass": "hit",
        "green": "hit",
        "done": "hit",
        "ok": "hit",
        "no": "miss",
        "false": "miss",
        "failed": "miss",
        "fail": "miss",
        "red": "miss",
        "yellow": "uncertain",
        "unknown": "uncertain",
        "not_enough_data": "pending",
        "not enough data": "pending",
        "missing": "pending",
        "not_applicable": "na",
        "n/a": "na",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized in {"hit", "miss", "pending", "uncertain", "na"}:
        return normalized
    return None


def coerce_evidence(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        return [{"quote": value}]
    if isinstance(value, dict):
        quote = text_field(value.get("quote")) or text_field(value.get("text"))
        speaker = text_field(value.get("speaker"))
        return [{"speaker": speaker or None, "quote": quote}] if quote else []
    if not isinstance(value, list):
        return []

    items: list[dict[str, Any]] = []
    for item in value[:2]:
        if isinstance(item, str):
            if item.strip():
                items.append({"quote": item.strip()})
        elif isinstance(item, dict):
            quote = text_field(item.get("quote")) or text_field(item.get("text"))
            if quote:
                speaker = text_field(item.get("speaker"))
                items.append({"speaker": speaker or None, "quote": quote})
    return items


def text_field(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def normalize_scorecard(
    *,
    stage: str,
    agenda: StageAgenda,
    raw: RawScorecard | None,
    fallback_reason: str | None = None,
    context: str | None = None,
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
    core_incomplete = any(
        check.level == "core" and check.result in {"pending", "uncertain"}
        for check in checks
    )
    hard_red = core_miss
    summary = (raw.summary if raw else "").strip()
    summary_blocks_green = text_blocks_green(summary)
    last_buyer_text = extract_last_buyer_text(context or "")
    direct_answer_debt = has_direct_answer_debt(last_buyer_text)
    conditional_value_objection = (
        stage == "S3.2" and has_conditional_value_objection(last_buyer_text)
    )

    if total_count == 0:
        readiness: Literal["green", "yellow", "red", "pending"] = "pending"
    elif hard_red or (score is not None and score < 0.45):
        readiness = "red"
    elif (
        score is not None
        and score >= 0.70
        and not core_miss
        and not core_incomplete
        and not summary_blocks_green
        and not direct_answer_debt
        and not conditional_value_objection
    ):
        readiness = "green"
    else:
        readiness = "yellow"

    readiness_label = {
        "green": "Готово",
        "yellow": "Почти",
        "red": "Рано",
        "pending": "Мало данных",
    }[readiness]

    next_action = normalize_next_action(
        raw.next_action if raw else "",
        agenda=agenda,
        readiness=readiness,
        stage=stage,
        context=context,
    )
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


def fallback_scorecard(
    stage: str,
    agenda: StageAgenda,
    reason: str | None = None,
    next_action: str | None = None,
    context: str | None = None,
) -> StageScorecard:
    public_reason = public_fallback_reason(reason)
    raw = RawScorecard(
        summary=public_reason,
        next_action=next_action or fallback_next_action(),
        checks=[],
    )
    return normalize_scorecard(
        stage=stage,
        agenda=agenda,
        raw=raw,
        fallback_reason=public_reason,
        context=context,
    )


def normalize_next_action(
    text: str | None,
    *,
    agenda: StageAgenda,
    readiness: str,
    stage: str | None = None,
    context: str | None = None,
) -> str:
    fallback_prefix = "Переход:" if readiness == "green" else "Уточнить:"
    raw = " ".join((text or "").strip().split())
    prefix, body = split_next_action_prefix(raw)
    prefix = prefix or fallback_prefix
    if not body or not is_speakable_next_action(body):
        body = fallback_next_action_body(readiness)
    last_buyer_text = extract_last_buyer_text(context or "")
    if has_direct_answer_debt(last_buyer_text) and not body_answers_direct_question(body):
        prefix = "Уточнить:"
        body = direct_answer_fallback_body(context or "")
    elif (
        stage in {"S2.2", "S2.3"}
        and readiness == "green"
        and body_looks_premature_pitch(body)
    ):
        prefix = "Переход:"
        body = discovery_transition_body(stage, context or "")
    elif stage == "S3.2" and readiness == "green" and not is_price_transition_body(body):
        prefix = "Переход:"
        body = s3_price_transition_body(context or "")
    return f"{prefix} {body}".strip()


def split_next_action_prefix(text: str) -> tuple[str | None, str]:
    for prefix in ("Уточнить:", "Переход:"):
        if text.startswith(prefix):
            return prefix, text[len(prefix) :].strip()
    return None, text.strip()


def is_speakable_next_action(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    lower = normalized.lower().replace("ё", "е")
    if re.match(r"^\d+[.)]\s*", normalized) or re.search(r"(?:^|[;\s])\d+[.)]\s*", normalized):
        return False
    meta_verbs = (
        "начать",
        "начните",
        "обозначить",
        "обозначьте",
        "получить",
        "получите",
        "предложить",
        "предложите",
        "дать",
        "спроси",
        "спросите",
        "уточни",
        "уточните",
        "задай",
        "задайте",
        "выяснить",
        "выясни",
        "выясните",
        "дай",
        "дайте",
        "сформулировать",
        "сформулируй",
        "сформулируйте",
        "перейти",
        "переведи",
        "переведите",
        "объяснить",
    )
    if re.match(rf"^(?:{'|'.join(meta_verbs)})\b[\s,:-]*", lower):
        return False
    if re.match(r"^(?:скажи|скажите)\s+клиент[ау]?\b", lower):
        return False
    if any(marker in lower for marker in STICKY_TEMPLATE_MARKERS):
        return False
    if any(marker in lower for marker in UNVERIFIED_FACT_MARKERS):
        return False
    if "..." in normalized or "…" in normalized:
        return False
    return True


def fallback_next_action() -> str:
    return f"Уточнить: {fallback_next_action_body('pending')}"


def fallback_next_action_body(readiness: str) -> str:
    if readiness == "green":
        return "Правильно понял вашу задачу и могу коротко показать, как формат с этим связан?"
    return "Что для вас сейчас важнее: быстро понять формат или сначала проверить, подходит ли он под вашу задачу?"


def extract_last_buyer_text(context: str) -> str:
    buyer_prefixes = ("клиент", "покупатель", "client", "buyer")
    diarized_prefixes = ("спикер", "канал")
    for line in reversed(context.splitlines()):
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        speaker, text = stripped.split(":", 1)
        speaker_normalized = speaker.strip().lower()
        if speaker_normalized in buyer_prefixes:
            return text.strip()
        if speaker_normalized.startswith(diarized_prefixes):
            return text.strip()
    return ""


def has_direct_answer_debt(text: str) -> bool:
    lower = text.lower().replace("ё", "е")
    if not lower:
        return False
    return any(marker in lower for marker in DIRECT_QUESTION_MARKERS)


def has_conditional_value_objection(text: str) -> bool:
    lower = text.lower().replace("ё", "е")
    if not lower:
        return False
    markers = (
        "теоретически",
        "звучит логично, но",
        "звучит уже конкретно",
        "если это не",
        "если там просто",
        "не просто болтов",
        "просто болтов",
        "но как",
        "но кто",
        "но механизм",
    )
    return any(marker in lower for marker in markers)


def body_answers_direct_question(text: str) -> bool:
    lower = text.lower().replace("ё", "е")
    if not lower:
        return False
    if lower.count("?") >= 1 and len(lower.split("?")[0].split()) < 6:
        return False
    if re.search(r"\b(?:давайте|давай)\s+(?:я\s+)?покаж", lower):
        return False
    if "покажу" in lower and not has_mechanic_marker(lower):
        return False
    return has_mechanic_marker(lower) or is_price_transition_body(text)


def has_mechanic_marker(lower_text: str) -> bool:
    markers = (
        "разбор",
        "диагност",
        "ментор",
        "психолог",
        "декларац",
        "90",
        "группа",
        "на связи",
        "план",
        "правил",
        "артефакт",
        "фиксир",
        "следующий шаг",
    )
    return any(marker in lower_text for marker in markers)


def direct_answer_fallback_body(context: str) -> str:
    lower = context.lower().replace("ё", "е")
    repeated_proof_boundary = (
        lower.count("подтвержденный кейс") >= 1
        or lower.count("это не тот формат") >= 1
    )
    if repeated_proof_boundary:
        return (
            "Похоже, ваш критерий - готовая гарантия или внешнее внедрение, а я не могу "
            "честно заявить это в нашем формате; лучше остановиться, чем продавать вам "
            "неподходящее решение."
        )
    wants_proof_or_guarantee = any(
        marker in lower
        for marker in (
            "давайте факты",
            "есть кейс",
            "кейс",
            "гарантия",
            "гарантии",
            "внешний аудит",
            "внешнего аудит",
            "включен в стоимость",
        )
    )
    if wants_proof_or_guarantee:
        if "агентств" not in lower and "внешний аудит" not in lower and "аудит" not in lower:
            return (
                "Честно: готовую гарантию результата или подтвержденный кейс под вашу "
                "точную ситуацию я не заявляю; можем разобрать вашу задачу и собрать план, "
                "но если нужен внедренный результат силами команды сервиса, это не тот формат."
            )
        return (
            "Честно: подтвержденный кейс агентства или внешний аудит в стоимость я не заявляю; "
            "на выезде можно разобрать вашу структуру и собрать план контроля, но если вам нужен "
            "готовый аудит с гарантией результата, это не тот формат."
        )
    repeated_mechanic = lower.count("механика такая") >= 1
    employee_change_question = any(
        marker in lower
        for marker in (
            "отношение сотруд",
            "менять их отношение",
            "регламенты они игнорируют",
            "механизм изменения",
            "саботир",
        )
    )
    if employee_change_question or repeated_mechanic:
        return (
            "Честно: за 4 дня мы не обещаем поменять сотрудников за вас; работа в том, "
            "чтобы разобрать, где вы возвращаете контроль себе, зафиксировать новое правило "
            "постановки и проверки задачи и вынести это в план на 90 дней - если нужен "
            "прямой тренинг команды, это другой формат."
        )
    if "glubina" in lower or "глубина" in lower or "казан" in lower:
        return (
            "Механика такая: на разборе с ментором и психологом разбираем вашу "
            "ситуацию, фиксируем личную декларацию и цели на 90 дней, а группа "
            "остается на связи после выезда; какой элемент контроля вам важно проверить?"
        )
    return (
        "Механика такая: сначала разбираем вашу ситуацию, затем фиксируем конкретный "
        "план и следующий проверочный шаг; какая часть для вас сейчас выглядит рискованной?"
    )


def is_price_transition_body(text: str) -> bool:
    lower = text.lower().replace("ё", "е")
    markers = ("стоим", "цена", "руб", "₽", "оплат", "услов", "бюджет", "99 000", "99000")
    return any(marker in lower for marker in markers)


def body_looks_premature_pitch(text: str) -> bool:
    lower = text.lower().replace("ё", "е")
    markers = (
        "давайте покаж",
        "покажу",
        "практическ",
        "разбор",
        "глубин",
        "казан",
        "программ",
        "формат",
        "участи",
        "мероприят",
    )
    return any(marker in lower for marker in markers)


def discovery_transition_body(stage: str | None, context: str) -> str:
    if stage == "S2.2":
        return (
            "Вы описали, что прошлый формат не дал реального внедрения; какой "
            "конкретный результат должен появиться после участия, чтобы вы сказали: "
            "это наконец сработало?"
        )
    if stage == "S2.3":
        return (
            "Вы описали разрыв между ручным контролем и нормальным делегированием; "
            "если это не закрыть ближайшие месяцы, чем это ударит по вам и бизнесу?"
        )
    return fallback_next_action_body("green")


def s3_price_transition_body(context: str) -> str:
    price = extract_price_from_context(context)
    if price:
        return (
            f"Вы сказали, что рабочими выглядят план и контроль; участие стоит {price}, "
            "дальше можно забронировать место или обсудить оплату - как вам такой вариант?"
        )
    return (
        "Вы сказали, что ценность в плане и контроле понятна; тогда перейду к условиям "
        "участия и проверю, насколько это подходит вам по бюджету."
    )


def extract_price_from_context(context: str) -> str:
    compact = " ".join(context.split())
    match = re.search(r"(\d[\d\s]{1,12})\s*(?:руб\.?|₽)", compact, flags=re.I)
    if not match:
        return ""
    amount = re.sub(r"\s+", " ", match.group(1)).strip()
    return f"{amount} руб."


def text_blocks_green(text: str) -> bool:
    normalized = text.lower().replace("ё", "е")
    markers = (
        "нужно",
        "требуется",
        "осталось",
        "не раскрыт",
        "не раскрыта",
        "не раскрыто",
        "не хватает",
        "рано",
        "преждевременно",
        "без получения",
        "нет явного",
        "необходимо",
        "пока не",
    )
    return any(marker in normalized for marker in markers)


def public_fallback_reason(reason: str | None) -> str:
    if not reason:
        return "Оценка не успела: пока лучше добрать buyer evidence и не переходить дальше."
    text = reason.lower()
    if "not configured" in text or "disabled" in text:
        return "Оценка временно недоступна: держись цели стадии и добери факты."
    return "Оценка не успела: пока лучше добрать buyer evidence и не переходить дальше."


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


def _extract_contract_block(text: str, start_marker: str, end_marker: str) -> str | None:
    start = text.find(start_marker)
    if start == -1:
        return None
    start += len(start_marker)
    end = text.find(end_marker, start)
    block = text[start:end] if end != -1 else text[start:]
    return block.strip()


def safe_parse_scorecard(text: str) -> RawScorecard:
    try:
        return parse_raw_scorecard(text)
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid scorecard response: {exc}") from exc
