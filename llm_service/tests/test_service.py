import asyncio
import json
from typing import AsyncIterator

import httpx
import pytest

from llm_service.app.config import Settings
from llm_service.app.live_asr import (
    extract_live_asr_transcripts,
    live_asr_tool_responses,
)
from llm_service.app.live_intelligence import (
    LiveIntelligenceResult,
    LiveIntelligenceNoUpdate,
    live_intelligence_system_prompt,
    parse_live_intelligence_response,
)
from llm_service.app.live_stage_audio import (
    live_stage_audio_stage_response,
    live_stage_audio_system_prompt,
    live_stage_audio_tool_calls,
    live_stage_audio_tool_responses,
)
from llm_service.app.orchestrator import (
    ConstructivePrefixStripper,
    LlmOrchestrator,
    OpenerCandidate,
    sse_event,
    strip_constructive_prefix,
)
from llm_service.app.prompts import (
    SALES_COACH_HELP_CONSTRUCTIVE_SYSTEM_PROMPT,
    SALES_COACH_HELP_OPENER_SYSTEM_PROMPT,
    SALES_COACH_LIVE_GENERATOR_SYSTEM_PROMPT,
    SALES_COACH_LIVE_VALIDATOR_SYSTEM_PROMPT,
)
from llm_service.app.providers import (
    CerebrasClient,
    parse_bos_eos_text,
    parse_json_suggestion,
    pop_vertex_stream_value,
    vertex_function_call_args,
    vertex_live_response_text,
    vertex_live_turn_complete,
)
from llm_service.app.scorecard import (
    RawScoreCheck,
    RawScorecard,
    normalize_scorecard,
    safe_parse_scorecard,
    scorecard_system_prompt,
)
from llm_service.app.schemas import HelpRequest, LiveRequest, StageRequest
from llm_service.app.stage_assets import (
    KNOWN_STAGES,
    STAGE_AGENDA_BY_TAG,
    clamp_stage_forward,
    parse_stage_detection,
    stage_is_backward,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_settings(**overrides):
    values = {
        "provider": "auto",
        "service_token": None,
        "outbound_proxy": None,
        "timeout_secs": 30.0,
        "rate_limit_backoff_ms": 15_000,
        "help_opener_timeout_ms": 4_000,
        "intelligence_transport": "rest",
        "cerebras_api_key": "test-key",
        "cerebras_api_base": "https://cerebras.test/v1",
        "cerebras_model": "zai-glm-4.7",
        "cerebras_stage_model": "zai-glm-4.7",
        "help_opener_primary_model": "primary-model",
        "help_opener_secondary_model": "secondary-model",
        "cerebras_reasoning_effort": "none",
        "cerebras_prompt_cache_key": True,
        "vertex_project": None,
        "vertex_location": "global",
        "vertex_model": "gemini-3.5-flash",
        "vertex_stage_model": "gemini-3.5-flash",
        "vertex_scorecard_model": "gemini-3.5-flash",
        "vertex_live_model": "gemini-2.0-flash-live-preview-04-09",
        "vertex_live_timeout_secs": 20.0,
        "vertex_live_asr_model": "gemini-live-2.5-flash-native-audio",
        "vertex_live_asr_location": "us-central1",
        "vertex_live_asr_timeout_secs": 20.0,
        "vertex_live_stage_model": "gemini-live-2.5-flash-native-audio",
        "vertex_live_stage_location": "us-central1",
        "vertex_live_stage_timeout_secs": 20.0,
        "vertex_api_base": "https://aiplatform.googleapis.com",
        "vertex_access_token": None,
        "vertex_adc_credentials_path": None,
        "vertex_quota_project_id": None,
        "vertex_thinking_level": "low",
        "vertex_scorecard_thinking_level": "minimal",
    }
    values.update(overrides)
    return Settings(**values)


def test_suggestion_parsers():
    assert parse_json_suggestion('{"action":"suggest","text":"Спроси про цель."}') == {
        "action": "suggest",
        "text": "Спроси про цель.",
    }
    assert parse_bos_eos_text("EOS") == {"action": "skip", "text": ""}
    assert parse_bos_eos_text("BOS Уточни бюджет.") == {
        "action": "suggest",
        "text": "Уточни бюджет.",
    }


def test_help_prompts_force_single_sentence():
    assert "Ровно одно предложение" in SALES_COACH_HELP_OPENER_SYSTEM_PROMPT
    assert "не генерируй следующий вопрос, оффер" in SALES_COACH_HELP_OPENER_SYSTEM_PROMPT
    assert "Только эмоционально присоединись" in SALES_COACH_HELP_OPENER_SYSTEM_PROMPT
    assert "Дай ровно одно предложение" in SALES_COACH_HELP_CONSTRUCTIVE_SYSTEM_PROMPT
    assert "строго следуя текущему stage -> agenda mapping" in SALES_COACH_HELP_CONSTRUCTIVE_SYSTEM_PROMPT
    assert "только actionable next step" in SALES_COACH_HELP_CONSTRUCTIVE_SYSTEM_PROMPT
    assert "Не используй" in SALES_COACH_HELP_CONSTRUCTIVE_SYSTEM_PROMPT
    assert "_Комментарий:_" in SALES_COACH_HELP_CONSTRUCTIVE_SYSTEM_PROMPT
    assert "оставить текущую реплику" in SALES_COACH_LIVE_VALIDATOR_SYSTEM_PROMPT
    assert 'action = "skip"' in SALES_COACH_LIVE_VALIDATOR_SYSTEM_PROMPT
    assert "готовую к зачитыванию реплику продавца" in SALES_COACH_LIVE_GENERATOR_SYSTEM_PROMPT
    assert "ровно одну короткую реплику" in SALES_COACH_LIVE_GENERATOR_SYSTEM_PROMPT


def test_stage_agenda_assets_cover_detector_tags():
    assert set(KNOWN_STAGES) == {
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
    }
    assert STAGE_AGENDA_BY_TAG["S3.4a"].agenda.startswith("выяснить")
    assert STAGE_AGENDA_BY_TAG["S3.5"].step.endswith("следующего контакта.")


def test_scorecard_prompt_requires_tactical_next_action():
    prompt = scorecard_system_prompt("S2.2", STAGE_AGENDA_BY_TAG["S2.2"])

    assert "<<<SCORECARD>>>" in prompt
    assert "check: <id> | <result>" in prompt
    assert "НЕ пересказывай пример/intent статично" in prompt
    assert 'next_action начинается с "Уточнить:"' in prompt
    assert 'next_action начинается с "Переход:"' in prompt
    assert "слова продавца клиенту" in prompt
    assert "не делай список" in prompt
    assert "buyer_anchor" in prompt
    assert "S2.2 green ведет только к S2.3" in prompt
    assert "Не обещай факты, которых нет" in prompt
    assert "direct-answer debt" in prompt
    assert "S3.2 green запрещен" in prompt


def test_stage_progress_helpers_prevent_backtracking():
    assert stage_is_backward("S2.3", "S2.2") is True
    assert stage_is_backward("S2.3", "S2.3") is False
    assert stage_is_backward("S2.3", "S2.4") is False
    assert clamp_stage_forward("S2.3", "S2.2") == "S2.3"
    assert clamp_stage_forward("S2.3", "S2.4") == "S2.4"


def test_scorecard_normalizes_meta_next_action_to_read_aloud_phrase():
    agenda = STAGE_AGENDA_BY_TAG["S2.1"]
    scorecard = normalize_scorecard(
        stage="S2.1",
        agenda=agenda,
        raw=RawScorecard(
            summary="Рамка еще не поставлена.",
            next_action=(
                "Уточнить: 1) быстро обозначьте цель звонка; "
                "2) спросите, удобно ли задавать вопросы."
            ),
            checks=[],
        ),
    )

    assert scorecard.next_action == (
        "Уточнить: Что для вас сейчас важнее: быстро понять формат "
        "или сначала проверить, подходит ли он под вашу задачу?"
    )


def test_scorecard_core_pending_blocks_green_readiness():
    agenda = STAGE_AGENDA_BY_TAG["S2.3"]
    scorecard = normalize_scorecard(
        stage="S2.3",
        agenda=agenda,
        raw=RawScorecard(
            summary="Цель есть, но подтверждение клиента еще нужно.",
            next_action="Переход: Давайте я расскажу, как формат закрывает этот разрыв?",
            checks=[
                RawScoreCheck(
                    id="target_captured",
                    result="hit",
                    reason="Клиент назвал цель.",
                ),
                RawScoreCheck(
                    id="gap_captured",
                    result="hit",
                    reason="Разрыв понятен.",
                ),
                RawScoreCheck(
                    id="target_specific",
                    result="hit",
                    reason="Есть метрика.",
                ),
                RawScoreCheck(
                    id="gap_buyer_confirmation",
                    result="pending",
                    reason="Клиент еще не подтвердил резюме.",
                ),
                RawScoreCheck(
                    id="gap_summary",
                    result="hit",
                    reason="Резюме есть.",
                ),
            ],
        ),
    )

    assert scorecard.readiness == "yellow"
    assert scorecard.ready_to_advance is False


def test_scorecard_blocks_green_on_unanswered_mechanism_question():
    agenda = STAGE_AGENDA_BY_TAG["S3.2"]
    scorecard = normalize_scorecard(
        stage="S3.2",
        agenda=agenda,
        raw=RawScorecard(
            summary="Клиенту ценность почти понятна.",
            next_action="Переход: Давайте покажу, как устроена группа?",
            checks=[
                RawScoreCheck(id="value_question", result="hit", reason="Спросили ценность."),
                RawScoreCheck(id="value_buyer_signal", result="hit", reason="Есть интерес."),
                RawScoreCheck(id="value_restates", result="hit", reason="Клиент назвал пользу."),
                RawScoreCheck(
                    id="value_active_objection_handled",
                    result="hit",
                    reason="Модель сочла сомнение закрытым.",
                ),
            ],
        ),
        context=(
            "--- Событие / продукт ---\n"
            "glubina.core, Казань, личная декларация, цели на 90 дней, группа на связи\n\n"
            "--- Диалог ---\n"
            "Клиент: Если там просто красивые презентации, мне не интересно. Механизм контроля какой?\n"
        ),
    )

    assert scorecard.readiness == "yellow"
    assert scorecard.next_action.startswith("Уточнить: Механика такая:")
    assert "декларацию" in scorecard.next_action


def test_scorecard_s32_green_moves_to_price_terms():
    agenda = STAGE_AGENDA_BY_TAG["S3.2"]
    scorecard = normalize_scorecard(
        stage="S3.2",
        agenda=agenda,
        raw=RawScorecard(
            summary="Ценность подтверждена.",
            next_action="Переход: Давайте покажу, как устроена группа?",
            checks=[
                RawScoreCheck(id="value_question", result="hit", reason="Спросили ценность."),
                RawScoreCheck(id="value_buyer_signal", result="hit", reason="Есть интерес."),
                RawScoreCheck(id="value_restates", result="hit", reason="Клиент назвал пользу."),
                RawScoreCheck(
                    id="value_active_objection_handled",
                    result="hit",
                    reason="Активных сомнений нет.",
                ),
            ],
        ),
        context=(
            "--- Событие / продукт ---\n"
            "glubina.core, Казань, стоимость 99 000 руб.\n\n"
            "--- Диалог ---\n"
            "Клиент: Окей, если есть план и контроль, тема интересная.\n"
        ),
    )

    assert scorecard.readiness == "green"
    assert scorecard.next_action.startswith("Переход:")
    assert "99 000 руб." in scorecard.next_action


def test_scorecard_s22_green_does_not_pitch_product():
    agenda = STAGE_AGENDA_BY_TAG["S2.2"]
    scorecard = normalize_scorecard(
        stage="S2.2",
        agenda=agenda,
        raw=RawScorecard(
            summary="Текущая ситуация собрана.",
            next_action="Переход: Давайте покажу, как на Глубине в Казани решают делегирование?",
            checks=[
                RawScoreCheck(id="current_problem_cluster", result="hit", reason="Есть боль."),
                RawScoreCheck(id="current_buyer_facts", result="hit", reason="Есть факты."),
                RawScoreCheck(id="current_open_question", result="hit", reason="Вопрос был."),
                RawScoreCheck(id="current_listening_balance", result="hit", reason="Баланс ок."),
                RawScoreCheck(id="current_focus", result="hit", reason="Фокус ок."),
            ],
        ),
        context="Клиент: Команда 12 человек, все равно все тащу сам.\n",
    )

    assert scorecard.readiness == "green"
    assert scorecard.next_action.startswith("Переход:")
    assert "какой конкретный результат" in scorecard.next_action
    assert "Глубине" not in scorecard.next_action
    assert "12 человек" not in scorecard.next_action


def test_scorecard_rejects_unverified_mechanics_in_next_action():
    agenda = STAGE_AGENDA_BY_TAG["S2.4"]
    scorecard = normalize_scorecard(
        stage="S2.4",
        agenda=agenda,
        raw=RawScorecard(
            summary="Мотив почти раскрыт.",
            next_action=(
                "Уточнить: На выезде личный трекер и служба безопасности проверят "
                "кандидатов через стресс-интервью; как вам такой механизм?"
            ),
            checks=[
                RawScoreCheck(id="motive_why_now", result="hit", reason="why now есть."),
                RawScoreCheck(id="motive_personal", result="hit", reason="мотив есть."),
                RawScoreCheck(id="motive_inaction_cost", result="hit", reason="цена есть."),
                RawScoreCheck(id="motive_safe_tone", result="hit", reason="тон ок."),
                RawScoreCheck(id="motive_energy", result="hit", reason="энергия есть."),
            ],
        ),
        context="Клиент: Я ищу управляющего и не хочу очередную теорию.\n",
    )

    assert "трекер" not in scorecard.next_action.lower()
    assert "стресс" not in scorecard.next_action.lower()


def test_scorecard_direct_answer_disqualifies_unverified_guarantee_request():
    agenda = STAGE_AGENDA_BY_TAG["S2.4"]
    scorecard = normalize_scorecard(
        stage="S2.4",
        agenda=agenda,
        raw=RawScorecard(
            summary="Клиент просит факты.",
            next_action="Уточнить: Давайте я покажу кейс агентства недвижимости?",
            checks=[
                RawScoreCheck(id="motive_why_now", result="hit", reason="why now есть."),
                RawScoreCheck(id="motive_personal", result="hit", reason="мотив есть."),
                RawScoreCheck(id="motive_inaction_cost", result="hit", reason="цена есть."),
                RawScoreCheck(id="motive_safe_tone", result="hit", reason="тон ок."),
                RawScoreCheck(id="motive_energy", result="hit", reason="энергия есть."),
            ],
        ),
        context=(
            "Клиент: Если у вас есть кейс агентства и внешний аудит включен в стоимость, "
            "давайте факты. Если нет, это не мой формат.\n"
        ),
    )

    assert "не тот формат" in scorecard.next_action
    assert "в стоимость я не заявляю" in scorecard.next_action


def test_scorecard_repeated_guarantee_boundary_stops_selling():
    agenda = STAGE_AGENDA_BY_TAG["S3.4a"]
    scorecard = normalize_scorecard(
        stage="S3.4a",
        agenda=agenda,
        raw=RawScorecard(
            summary="Клиент повторно просит гарантию.",
            next_action="Уточнить: Давайте еще раз обсудим ценность формата?",
            checks=[
                RawScoreCheck(id="objection_detected", result="hit", reason="Есть возражение."),
                RawScoreCheck(id="objection_clarified", result="miss", reason="Не уточнили."),
                RawScoreCheck(id="objection_type", result="hit", reason="Риск."),
                RawScoreCheck(id="objection_root_reason", result="hit", reason="Нужна гарантия."),
                RawScoreCheck(id="objection_answer_fit", result="miss", reason="Не попали."),
            ],
        ),
        context=(
            "Продавец: Честно: подтвержденный кейс агентства или внешний аудит в стоимость я не заявляю; "
            "на выезде можно разобрать вашу структуру и собрать план контроля, но если вам нужен "
            "готовый аудит с гарантией результата, это не тот формат.\n"
            "Клиент: Я снова спрашиваю про гарантию результата. Если ее нет, зачем продолжать?\n"
        ),
    )

    assert "лучше остановиться" in scorecard.next_action
    assert "неподходящее решение" in scorecard.next_action


def test_safe_parse_scorecard_accepts_dict_checks_and_status_aliases():
    raw = safe_parse_scorecard(
        json.dumps(
            {
                "summary": "Рамка почти задана.",
                "advice": "Уточнить: договориться на вопросы.",
                "checks": {
                    "frame_agenda": {
                        "status": "hit",
                        "reason": "Продавец объяснил формат.",
                        "evidence": "Сейчас провожу касдевы.",
                    },
                    "frame_permission": "miss",
                    "frame_no_pitch": {"state": "green"},
                },
            },
            ensure_ascii=False,
        )
    )

    assert raw.next_action == "Уточнить: договориться на вопросы."
    assert [check.id for check in raw.checks] == [
        "frame_agenda",
        "frame_permission",
        "frame_no_pitch",
    ]
    assert [check.result for check in raw.checks] == ["hit", "miss", "hit"]
    assert raw.checks[0].evidence[0].quote == "Сейчас провожу касдевы."


def test_safe_parse_scorecard_accepts_top_level_keyed_checks():
    raw = safe_parse_scorecard(
        json.dumps(
            {
                "current_problem_cluster": {
                    "status": "hit",
                    "reason": "Клиент назвал нехватку денег.",
                },
                "current_buyer_facts": {
                    "status": "pending",
                    "evidence": [{"speaker": "Клиент", "text": "денег мало"}],
                },
            },
            ensure_ascii=False,
        )
    )

    assert raw.summary == "Оценка по чеклисту."
    assert raw.checks[0].id == "current_problem_cluster"
    assert raw.checks[0].result == "hit"
    assert raw.checks[1].id == "current_buyer_facts"
    assert raw.checks[1].result == "pending"


def test_safe_parse_scorecard_accepts_text_contract():
    raw = safe_parse_scorecard(
        """
        <<<SCORECARD>>>
        summary: Клиент назвал текущую боль, но деталей пока мало.
        next_action: Уточнить: Вы сказали, что деньги застревают; в какой точке это чаще всего происходит?
        check: current_problem_cluster | hit | Клиент сам назвал проблему | Денег не хватает на развитие.
        check: current_buyer_facts | pending | Не хватает цифр и примеров |
        <<<END_SCORECARD>>>
        """
    )

    assert raw.summary == "Клиент назвал текущую боль, но деталей пока мало."
    assert raw.next_action.startswith("Уточнить:")
    assert [check.id for check in raw.checks] == [
        "current_problem_cluster",
        "current_buyer_facts",
    ]
    assert raw.checks[0].result == "hit"
    assert raw.checks[0].evidence[0].quote == "Денег не хватает на развитие."
    assert raw.checks[1].result == "pending"


def test_parse_stage_detection_accepts_json_and_text_contract():
    assert parse_stage_detection('{"stage":"S3.4A","confidence":0.8}') == (
        "S3.4a",
        0.8,
    )
    assert parse_stage_detection(
        "<<<STAGE>>>\nstage: S2.3\nconfidence: 0.61\n<<<END_STAGE>>>"
    ) == ("S2.3", 0.61)
    assert parse_stage_detection("Сейчас мы в S2.3") == ("S2.3", None)


def test_parse_stage_detection_infers_natural_language_stage():
    assert parse_stage_detection(
        "BOS\nМы перешли от установки фрейма к сбору текущей реальности."
    ) == ("S2.2", None)
    assert parse_stage_detection(
        "EOS\nКлиент сказал, что надо подумать; нужно понять, цена или ценность."
    ) == ("S3.4a", None)


def test_live_intelligence_prompt_contains_stage_and_scorecard_contract():
    prompt = live_intelligence_system_prompt()

    assert "realtime intelligence engine" in prompt
    assert "Stage -> agenda mapping" in prompt
    assert "S3.4a — Возражение: уточняющий вопрос" in prompt
    assert "objection_detected" in prompt
    assert '"stage": "S2.2"' in prompt
    assert '"next_action": "Уточнить: ..."' in prompt
    assert "Правило тишины" in prompt
    assert "не пиши ничего" in prompt


def test_parse_live_intelligence_response_normalizes_nested_scorecard():
    result = parse_live_intelligence_response(
        json.dumps(
            {
                "stage": "s2.2",
                "confidence": 0.71,
                "scorecard": {
                    "summary": "Есть текущая боль, но мало конкретики.",
                    "next_action": "Уточнить: где именно проседают деньги и сроки?",
                    "checks": [
                        {
                            "id": "current_problem_cluster",
                            "result": "hit",
                            "reason": "Клиент назвал проблему.",
                            "evidence": [
                                {
                                    "speaker": "Клиент",
                                    "quote": "Денег не хватает на развитие.",
                                }
                            ],
                        }
                    ],
                },
            },
            ensure_ascii=False,
        )
    )

    assert result.stage == "S2.2"
    assert result.confidence == 0.71
    assert result.scorecard.summary == "Есть текущая боль, но мало конкретики."
    assert result.scorecard.checks[0].id == "current_problem_cluster"


def test_parse_live_intelligence_response_accepts_no_update_silence():
    with pytest.raises(LiveIntelligenceNoUpdate):
        parse_live_intelligence_response("   ")


def test_vertex_live_response_helpers_accept_camel_case_frames():
    value = {
        "serverContent": {
            "modelTurn": {"parts": [{"text": '{"stage":"S2.2"'}]},
            "turnComplete": True,
        }
    }

    assert vertex_live_response_text(value) == '{"stage":"S2.2"'
    assert vertex_live_turn_complete(value) is True


def test_vertex_function_call_args_extracts_tool_args():
    value = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "functionCall": {
                                "name": "submit_scorecard",
                                "args": {"summary": "ok", "checks": []},
                            }
                        }
                    ]
                }
            }
        ]
    }

    assert vertex_function_call_args(value, "submit_scorecard") == {
        "summary": "ok",
        "checks": [],
    }


def test_vertex_function_call_args_extracts_stage_detection_args():
    value = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "functionCall": {
                                "name": "submit_stage_detection",
                                "args": {"stage": "S2.3", "confidence": 0.74},
                            }
                        }
                    ]
                }
            }
        ]
    }

    assert vertex_function_call_args(value, "submit_stage_detection") == {
        "stage": "S2.3",
        "confidence": 0.74,
    }


def test_live_asr_extracts_input_transcription_and_tool_call():
    input_frame = {
        "serverContent": {
            "inputTranscription": {
                "text": "Клиент говорит про деньги.",
            }
        }
    }
    tool_frame = {
        "toolCall": {
            "functionCalls": [
                {
                    "id": "call-1",
                    "name": "report_transcript",
                    "args": {"text": "Нужно подумать.", "is_final": True},
                }
            ]
        }
    }

    input_transcripts = extract_live_asr_transcripts(input_frame)
    tool_transcripts = extract_live_asr_transcripts(tool_frame)

    assert input_transcripts[0].text == "Клиент говорит про деньги."
    assert input_transcripts[0].is_final is False
    assert input_transcripts[0].source == "inputTranscription"
    assert tool_transcripts[0].text == "Нужно подумать."
    assert tool_transcripts[0].is_final is True
    assert tool_transcripts[0].source == "toolCall"
    assert live_asr_tool_responses(tool_frame) == [
        {
            "id": "call-1",
            "name": "report_transcript",
            "response": {"ok": True},
        }
    ]


def test_live_stage_audio_prompt_and_tool_call_contract():
    prompt = live_stage_audio_system_prompt()

    assert "submit_stage_scorecard" in prompt
    assert "S2.1" in prompt
    assert "Stage -> agenda mapping" in prompt
    assert "Никогда не возвращай stage назад" in prompt
    assert "готовой фразой продавца клиенту" in prompt

    frame = {
        "toolCall": {
            "functionCalls": [
                {
                    "id": "stage-call-1",
                    "name": "submit_stage_scorecard",
                    "args": {
                        "stage": "S2.2",
                        "confidence": 0.8,
                        "summary": "Клиент описывает текущую ситуацию.",
                        "next_action": "Уточнить: где сейчас сильнее всего болит?",
                        "checks": [
                            {
                                "id": "current_problem_cluster",
                                "result": "hit",
                                "reason": "Клиент назвал проблему.",
                                "evidence": [{"quote": "денег почти нет"}],
                            }
                        ],
                    },
                }
            ]
        }
    }

    calls = live_stage_audio_tool_calls(frame)

    assert len(calls) == 1
    assert calls[0].call_id == "stage-call-1"
    assert calls[0].args["stage"] == "S2.2"
    assert live_stage_audio_tool_responses(frame) == [
        {
            "id": "stage-call-1",
            "name": "submit_stage_scorecard",
            "response": {"ok": True},
        }
    ]


def test_live_stage_audio_stage_response_normalizes_scorecard():
    response = live_stage_audio_stage_response(
        {
            "stage": "S2.2",
            "confidence": 0.8,
            "summary": "Клиент описывает текущую ситуацию.",
            "next_action": "Уточнить: есть ли долги или кассовые разрывы?",
            "checks": [
                {
                    "id": "current_problem_cluster",
                    "result": "hit",
                    "reason": "Клиент назвал нехватку денег.",
                    "evidence": [{"quote": "свободных денег почти нет"}],
                }
            ],
        },
        model="gemini-live-2.5-flash-native-audio",
    )

    assert response.provider == "vertex-live-audio"
    assert response.stage == "S2.2"
    assert response.model == "gemini-live-2.5-flash-native-audio"
    assert response.scorecard is not None
    assert response.scorecard.next_action.startswith("Уточнить:")
    assert response.scorecard.checks[0].result == "hit"


def test_live_stage_audio_stage_response_clamps_backward_stage():
    response = live_stage_audio_stage_response(
        {
            "stage": "S2.2",
            "confidence": 0.8,
            "summary": "Модель услышала старую тему.",
            "next_action": "Уточнить: Какие критерии успеха будут для вас доказательством?",
            "checks": [],
        },
        model="gemini-live-2.5-flash-native-audio",
        last_stage="S2.3",
    )

    assert response.stage == "S2.3"
    assert response.title == STAGE_AGENDA_BY_TAG["S2.3"].title


def test_constructive_prefix_stripper_removes_ui_heading():
    assert strip_constructive_prefix("**Следующий ход:** Спроси про цель.") == (
        "Спроси про цель."
    )

    stripper = ConstructivePrefixStripper()
    assert stripper.feed("**След") == ""
    assert stripper.feed("ующий ход:** Спроси про цель.") == "Спроси про цель."
    assert stripper.feed(" Еще текст.") == " Еще текст."


@pytest.mark.anyio
async def test_cerebras_prompt_cache_retry():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if "prompt_cache_key" in payload:
            return httpx.Response(400, text="prompt_cache_key unsupported")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        cerebras = CerebrasClient(make_settings(), client)

        text = await cerebras.text(
            model="model",
            system_prompt="system",
            user_content="user",
            temperature=0.2,
            prompt_cache_key="cache-key",
        )

        assert text == "ok"
        assert len(calls) == 2
        assert "prompt_cache_key" in calls[0]
        assert "prompt_cache_key" not in calls[1]
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_live_uses_zai_validator_to_keep_current_reply():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    try:
        orchestrator = LlmOrchestrator(
            make_settings(
                vertex_project="project-id",
                vertex_access_token="token",
            ),
            client,
        )
        validator_calls = []
        generator_calls = []

        async def fake_validator(request: LiveRequest) -> dict[str, str]:
            validator_calls.append(request.current_text)
            return {"action": "skip", "text": ""}

        async def fake_generator(_request: LiveRequest) -> dict[str, str]:
            generator_calls.append(True)
            return {"action": "suggest", "text": "Этот текст не должен понадобиться."}

        orchestrator._cerebras_live_validator = fake_validator
        orchestrator._vertex_live_generate = fake_generator

        response = await orchestrator.live(
            LiveRequest(
                run_id="run",
                content="Клиент все еще раскрывает ту же проблему.",
                current_text="Какой именно результат для вас был бы доказательством успеха?",
            )
        )

        assert response.action == "skip"
        assert response.text == ""
        assert response.provider == "cerebras"
        assert response.model == "zai-glm-4.7"
        assert validator_calls == [
            "Какой именно результат для вас был бы доказательством успеха?"
        ]
        assert generator_calls == []
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_live_uses_gemini_generator_when_zai_marks_reply_stale():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    try:
        orchestrator = LlmOrchestrator(
            make_settings(
                vertex_project="project-id",
                vertex_access_token="token",
            ),
            client,
        )
        validator_calls = []
        generator_calls = []

        async def fake_validator(request: LiveRequest) -> dict[str, str]:
            validator_calls.append(request.current_text)
            return {"action": "suggest", "text": ""}

        async def fake_generator(request: LiveRequest) -> dict[str, str]:
            generator_calls.append(request.current_text)
            return {
                "action": "suggest",
                "text": "Что для вас должно измениться в ближайшие три месяца, чтобы вы назвали это реальным сдвигом?",
            }

        orchestrator._cerebras_live_validator = fake_validator
        orchestrator._vertex_live_generate = fake_generator

        response = await orchestrator.live(
            LiveRequest(
                run_id="run",
                content="Клиент уже ответил на прошлый вопрос и ушел в новую тему.",
                current_text="Почему пока не получается сделать это самостоятельно?",
            )
        )

        assert response.action == "suggest"
        assert response.provider == "vertex"
        assert response.model == "gemini-3.5-flash"
        assert response.text.startswith("Что для вас должно измениться")
        assert validator_calls == ["Почему пока не получается сделать это самостоятельно?"]
        assert generator_calls == ["Почему пока не получается сделать это самостоятельно?"]
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_stage_agenda_uses_cerebras_stage_detection_and_vertex_scorecard_minimal():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append((request, payload))
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": '{"stage":"S3.4a","confidence":0.82}'
                            }
                        }
                    ]
                },
            )
        if len(calls) == 2:
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": (
                                            "<<<SCORECARD>>>\n"
                                            "summary: Возражение услышано и уточняется.\n"
                                            "next_action: Спроси, проблема в цене или ценности?\n"
                                            "check: objection_detected | hit | Клиент выразил сомнение. | Мне надо подумать.\n"
                                            "check: objection_clarified | hit | Продавец уточнил причину. |\n"
                                            "check: objection_type | hit | Тип возражения понятен. |\n"
                                            "check: objection_root_reason | hit | Root reason назван. |\n"
                                            "check: objection_answer_fit | hit | Ответ попал по сути. |\n"
                                            "<<<END_SCORECARD>>>"
                                        )
                                    }
                                ]
                            }
                        }
                    ]
                },
            )
        return httpx.Response(500, text="unexpected request")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        orchestrator = LlmOrchestrator(
            make_settings(
                vertex_project="project-id",
                vertex_access_token="token",
            ),
            client,
        )

        response = await orchestrator.stage_agenda(
            StageRequest(run_id="run", context="dialogue", current_stage=None)
        )

        assert response.stage == "S3.4a"
        assert response.title == "Возражение: уточняющий вопрос"
        assert response.agenda == 'выяснить истинную причину возражения ("цена или ценность?")'
        assert response.provider == "cerebras"
        assert response.model == "zai-glm-4.7"
        assert response.confidence == 0.82
        request, payload = calls[0]
        assert payload["temperature"] == 0.0
        assert payload["model"] == "zai-glm-4.7"
        assert payload["response_format"]["json_schema"]["name"] == "sales_stage_detection"
        assert "Prompt 1: Detect Where We Are" in payload["messages"][0]["content"]
        assert str(request.url) == "https://cerebras.test/v1/chat/completions"
        assert response.scorecard is not None
        assert response.scorecard.readiness == "green"
        assert response.scorecard.ready_to_advance is True
        assert response.scorecard.hit_count == 5
        assert response.scorecard.miss_count == 0
        assert response.scorecard.score == 1.0
        assert response.scorecard.next_action == (
            "Переход: Правильно понял вашу задачу и могу коротко показать, "
            "как формат с этим связан?"
        )
        assert len(calls) == 2
        scorecard_payload = calls[1][1]
        assert "tools" not in scorecard_payload
        assert "toolConfig" not in scorecard_payload
        assert scorecard_payload["generationConfig"]["thinkingConfig"] == {
            "thinkingLevel": "minimal"
        }
        assert "<<<SCORECARD>>>" in scorecard_payload["systemInstruction"]["parts"][0]["text"]
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_stage_agenda_can_skip_scorecard_for_candidate():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append((request, payload))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"stage":"S2.2","confidence":0.73}'
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        orchestrator = LlmOrchestrator(
            make_settings(
                vertex_project="project-id",
                vertex_access_token="token",
            ),
            client,
        )

        response = await orchestrator.stage_agenda(
            StageRequest(
                run_id="run",
                context="dialogue",
                current_stage="S2.1",
                include_scorecard=False,
            )
        )

        assert response.stage == "S2.2"
        assert response.scorecard is None
        assert len(calls) == 1
        request, payload = calls[0]
        assert str(request.url) == "https://cerebras.test/v1/chat/completions"
        assert payload["response_format"]["json_schema"]["name"] == "sales_stage_detection"
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_stage_agenda_uses_cerebras_text_contract_for_stage_fallback():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "<<<STAGE>>>\n"
                                "stage: S2.3\n"
                                "confidence: 0.77\n"
                                "<<<END_STAGE>>>"
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        orchestrator = LlmOrchestrator(
            make_settings(vertex_project=None),
            client,
        )

        response = await orchestrator.stage_agenda(
            StageRequest(run_id="run", context="dialogue", current_stage="S2.2")
        )

        assert response.stage == "S2.3"
        assert response.provider == "cerebras"
        assert response.confidence == 0.77
        assert len(calls) == 1
        assert calls[0]["response_format"]["json_schema"]["name"] == "sales_stage_detection"
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_stage_agenda_returns_none_when_stage_is_unchanged_without_scorecard():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"stage":"S2.2","confidence":0.91}'
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        orchestrator = LlmOrchestrator(make_settings(), client)

        response = await orchestrator.stage_agenda(
            StageRequest(
                run_id="run",
                context="dialogue",
                current_stage="S2.2",
                include_scorecard=False,
            )
        )

        assert response is None
        assert len(calls) == 1
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_stage_agenda_returns_scorecard_when_stage_is_unchanged_but_committed():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"stage":"S2.2","confidence":0.91}'
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        orchestrator = LlmOrchestrator(
            make_settings(vertex_project=None),
            client,
        )

        response = await orchestrator.stage_agenda(
            StageRequest(run_id="run", context="dialogue", current_stage="S2.2")
        )

        assert response is not None
        assert response.stage == "S2.2"
        assert response.scorecard is not None
        assert response.scorecard.readiness == "pending"
        assert len(calls) == 1
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_stage_agenda_uses_live_intelligence_transport():
    rest_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        rest_calls.append(request)
        return httpx.Response(500, text="REST should not be used")

    class FakeLiveSession:
        def __init__(self) -> None:
            self.calls = []

        async def analyze(self, *, context: str, current_stage: str | None):
            self.calls.append((context, current_stage))
            return LiveIntelligenceResult(
                stage="S2.2",
                confidence=0.86,
                scorecard=RawScorecard(
                    summary="Клиент описал текущую финансовую боль.",
                    next_action="Уточнить: где именно не хватает денег и какой срок критичен?",
                    checks=[
                        RawScoreCheck(
                            id="current_problem_cluster",
                            result="hit",
                            reason="Клиент назвал проблему.",
                            evidence=[],
                        )
                    ],
                ),
                raw_text="{}",
            )

        async def aclose(self) -> None:
            return None

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        orchestrator = LlmOrchestrator(
            make_settings(
                cerebras_api_key=None,
                intelligence_transport="live",
                vertex_project="project-id",
                vertex_access_token="token",
            ),
            client,
        )
        fake = FakeLiveSession()
        orchestrator._live_intelligence_sessions["run"] = fake

        response = await orchestrator.stage_agenda(
            StageRequest(
                run_id="run",
                context="Клиент: денег не хватает на развитие.",
                current_stage="S2.1",
            )
        )

        assert response.stage == "S2.2"
        assert response.provider == "vertex-live"
        assert response.model == "gemini-2.0-flash-live-preview-04-09"
        assert response.confidence == 0.86
        assert response.scorecard is not None
        assert response.scorecard.next_action.startswith("Уточнить:")
        assert fake.calls == [("Клиент: денег не хватает на развитие.", "S2.1")]
        assert rest_calls == []
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_stage_agenda_live_clamps_backward_stage():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="REST should not be used")

    class FakeLiveSession:
        async def analyze(self, *, context: str, current_stage: str | None):
            return LiveIntelligenceResult(
                stage="S2.2",
                confidence=0.7,
                scorecard=RawScorecard(
                    summary="Снова всплыла текущая ситуация.",
                    next_action="Уточнить: Какие критерии успеха будут для вас доказательством?",
                    checks=[],
                ),
                raw_text="{}",
            )

        async def aclose(self) -> None:
            return None

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        orchestrator = LlmOrchestrator(
            make_settings(
                cerebras_api_key=None,
                intelligence_transport="live",
                vertex_project="project-id",
                vertex_access_token="token",
            ),
            client,
        )
        orchestrator._live_intelligence_sessions["run"] = FakeLiveSession()

        response = await orchestrator.stage_agenda(
            StageRequest(
                run_id="run",
                context="Клиент снова говорит про нехватку денег.",
                current_stage="S2.3",
            )
        )

        assert response.stage == "S2.3"
        assert response.title == STAGE_AGENDA_BY_TAG["S2.3"].title
        assert response.scorecard is not None
        assert response.scorecard.next_action.startswith("Уточнить:")
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_stage_agenda_live_no_update_returns_none_without_rest_fallback():
    rest_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        rest_calls.append(request)
        return httpx.Response(500, text="REST should not be used")

    class FakeLiveSession:
        async def analyze(self, *, context: str, current_stage: str | None):
            raise LiveIntelligenceNoUpdate("unchanged")

        async def aclose(self) -> None:
            return None

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        orchestrator = LlmOrchestrator(
            make_settings(
                cerebras_api_key=None,
                intelligence_transport="live",
                vertex_project="project-id",
                vertex_access_token="token",
            ),
            client,
        )
        orchestrator._live_intelligence_sessions["run"] = FakeLiveSession()

        response = await orchestrator.stage_agenda(
            StageRequest(
                run_id="run",
                context="Клиент повторяет тот же тезис.",
                current_stage="S2.2",
            )
        )

        assert response is None
        assert rest_calls == []
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_stage_agenda_falls_back_to_current_stage_without_502():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": (
                                        "Модель не вернула JSON, а данных пока мало "
                                        "для уверенного определения стадии."
                                    )
                                }
                            ]
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        orchestrator = LlmOrchestrator(
            make_settings(
                cerebras_api_key=None,
                vertex_project="project-id",
                vertex_access_token="token",
            ),
            client,
        )

        response = await orchestrator.stage_agenda(
            StageRequest(run_id="run", context="dialogue", current_stage="S2.2")
        )

        assert response.stage == "S2.2"
        assert response.provider == "fallback"
        assert response.model == "last-known-stage"
        assert response.confidence == 0.0
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_stage_agenda_returns_pending_scorecard_on_vertex_timeout():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": (
                                            "<<<STAGE>>>\n"
                                            "stage: S2.2\n"
                                            "confidence: 0.90\n"
                                            "<<<END_STAGE>>>"
                                        )
                                    }
                                ]
                            }
                        }
                    ]
                },
            )
        if len(calls) == 2:
            raise httpx.ReadTimeout("scorecard timeout", request=request)
        if len(calls) == 3:
            return httpx.Response(
                200,
                text=(
                    '{"candidates":[{"content":{"parts":[{"text":'
                    '"Уточнить: 1) что именно не получилось; 2) какой результат нужен."'
                    '}]}}]}'
                ),
            )
        return httpx.Response(500, text="unexpected request")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        orchestrator = LlmOrchestrator(
            make_settings(
                cerebras_api_key=None,
                vertex_project="project-id",
                vertex_access_token="token",
            ),
            client,
        )

        response = await orchestrator.stage_agenda(
            StageRequest(run_id="run", context="dialogue", current_stage=None)
        )

        assert response.stage == "S2.2"
        assert response.provider == "vertex"
        assert response.scorecard is not None
        assert response.scorecard.readiness == "pending"
        assert response.scorecard.hit_count == 0
        assert response.scorecard.miss_count == 0
        assert response.scorecard.ready_to_advance is False
        assert "Оценка не успела" in response.scorecard.summary
        assert response.scorecard.next_action.startswith("Уточнить:")
        assert len(calls) == 3
        assert calls[1]["generationConfig"]["thinkingConfig"] == {
            "thinkingLevel": "minimal"
        }
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_help_opener_selects_primary_model():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        model = payload["model"]
        text = (
            'data: {"choices":[{"delta":{"content":"primary answer"}}]}\n\n'
            "data: [DONE]\n\n"
            if model == "primary-model"
            else "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=text)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        orchestrator = LlmOrchestrator(make_settings(), client)

        response = await orchestrator.help_opener(
            HelpRequest(id=1, run_id="run", context="context")
        )

        assert response.text == "primary answer"
        assert response.model == "primary-model"
        assert response.fallback is False
        assert all(call["temperature"] == 1.0 for call in calls)
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_help_opener_stream_waits_for_higher_priority_delta():
    async def high_priority_stream() -> AsyncIterator[str]:
        await asyncio.sleep(0.05)
        yield "priority answer"

    async def low_priority_stream() -> AsyncIterator[str]:
        yield "fast"
        yield " answer"

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    try:
        orchestrator = LlmOrchestrator(make_settings(cerebras_api_key=None), client)
        orchestrator._opener_candidates = lambda _: [
            OpenerCandidate(
                slot="gemini",
                priority=0,
                provider="vertex",
                model="gemini-3.5-flash",
                stream=high_priority_stream(),
            ),
            OpenerCandidate(
                slot="oss",
                priority=2,
                provider="cerebras",
                model="gpt-oss-120b",
                stream=low_priority_stream(),
            ),
        ]

        frames = [
            json.loads(frame.decode("utf-8").removeprefix("data:").strip())
            async for frame in orchestrator.help_opener_stream(
                HelpRequest(id=1, run_id="run", context="context")
            )
        ]

        assert frames == [
            {"event": "model", "model": "gemini-3.5-flash", "provider": "vertex"},
            {"event": "delta", "text": "priority answer"},
            {"event": "done"},
        ]
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_help_opener_stream_uses_lower_priority_after_timeout():
    async def high_priority_stream() -> AsyncIterator[str]:
        await asyncio.sleep(0.05)
        yield "too late"

    async def low_priority_stream() -> AsyncIterator[str]:
        yield "fallback winner"

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    try:
        orchestrator = LlmOrchestrator(
            make_settings(cerebras_api_key=None, help_opener_timeout_ms=5), client
        )
        orchestrator._opener_candidates = lambda _: [
            OpenerCandidate(
                slot="gemini",
                priority=0,
                provider="vertex",
                model="gemini-3.5-flash",
                stream=high_priority_stream(),
            ),
            OpenerCandidate(
                slot="oss",
                priority=2,
                provider="cerebras",
                model="gpt-oss-120b",
                stream=low_priority_stream(),
            ),
        ]

        frames = [
            json.loads(frame.decode("utf-8").removeprefix("data:").strip())
            async for frame in orchestrator.help_opener_stream(
                HelpRequest(id=1, run_id="run", context="context")
            )
        ]

        assert frames == [
            {"event": "model", "model": "gpt-oss-120b", "provider": "cerebras"},
            {"event": "delta", "text": "fallback winner"},
            {"event": "done"},
        ]
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_help_opener_vertex_candidate_sends_low_thinking():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            text='[{"candidates":[{"content":{"parts":[{"text":"vertex answer"}]}}]}]',
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        orchestrator = LlmOrchestrator(
            make_settings(
                cerebras_api_key=None,
                vertex_project="project",
                vertex_access_token="token",
                vertex_thinking_level="low",
            ),
            client,
        )

        response = await orchestrator.help_opener(
            HelpRequest(id=1, run_id="run", context="context")
        )

        assert response.text == "vertex answer"
        assert response.model == "gemini-3.5-flash"
        assert calls[0]["generationConfig"]["temperature"] == 1.0
        assert calls[0]["generationConfig"]["thinkingConfig"] == {
            "thinkingLevel": "low"
        }
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_help_constructive_stream_sends_temperature_one():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            text='[{"candidates":[{"content":{"parts":[{"text":"**Следующий ход:** next step"}]}}]}]',
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        orchestrator = LlmOrchestrator(
            make_settings(
                cerebras_api_key=None,
                vertex_project="project",
                vertex_access_token="token",
            ),
            client,
        )

        frames = [
            json.loads(frame.decode("utf-8").removeprefix("data:").strip())
            async for frame in orchestrator.help_constructive_stream(
                HelpRequest(id=1, run_id="run", context="context")
            )
        ]

        assert frames == [
            {"event": "model", "model": "gemini-3.5-flash"},
            {"event": "delta", "text": "next step"},
            {"event": "done"},
        ]
        assert calls[0]["generationConfig"]["temperature"] == 1.0
        user_text = calls[0]["contents"][0]["parts"][0]["text"]
        assert "--- Fixed stage -> agenda mapping ---" in user_text
        assert "S3.4a — Возражение: уточняющий вопрос" in user_text
        assert "Строго опирайся на текущий stage" in user_text
    finally:
        await client.aclose()


def test_sse_event_is_json_data_frame():
    frame = sse_event({"event": "delta", "text": "привет"}).decode("utf-8")

    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    assert '"text":"привет"' in frame


def test_vertex_stream_parser_handles_array_delimited_json():
    buffer = (
        '[{"candidates":[{"content":{"parts":[{"text":"one"}]}}]},'
        '{"candidates":[{"content":{"parts":[{"text":"two"}]}}]}]'
    )

    first, buffer, consumed = pop_vertex_stream_value(buffer)
    assert consumed is True
    assert first["candidates"][0]["content"]["parts"][0]["text"] == "one"

    second, buffer, consumed = pop_vertex_stream_value(buffer)
    assert consumed is True
    assert second["candidates"][0]["content"]["parts"][0]["text"] == "two"

    _, _, consumed = pop_vertex_stream_value(buffer)
    assert consumed is False
