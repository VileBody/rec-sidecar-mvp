from llm_service.app.paper_roleplay import (
    BuyerProfile,
    StageSnapshot,
    complex_buyer_profile,
    detect_terminal_outcome,
    extract_buyer_profiles,
    fallback_buyer_reply,
    parse_seller_response,
    render_metrics_lines,
    heuristic_stage_snapshot,
    seller_fallback_reply,
    seller_reply_is_usable,
    speakable_seller_text,
)


def test_extract_buyer_profiles_from_seed_markdown() -> None:
    markdown = """
## Скрипт 7. Тестовый покупатель

**Персона:** предприниматель, сопротивление: "нет времени".

**Клиент:** Давайте коротко.
"""

    profiles = extract_buyer_profiles(markdown)

    assert len(profiles) == 1
    assert profiles[0].number == 7
    assert profiles[0].title == "Тестовый покупатель"
    assert "нет времени" in profiles[0].persona
    assert profiles[0].seed_client_lines == ("Давайте коротко.",)


def test_complex_buyer_profile_has_multilayer_constraints() -> None:
    profile = BuyerProfile(
        number=2,
        title="Скептик",
        persona="скептик после курсов",
        seed_client_lines=(),
    )

    text = complex_buyer_profile(profile)

    assert "Скрытый мотив" in text
    assert "Resistance phases" in text
    assert "Switching condition" in text
    assert "Red lines" in text


def test_speakable_seller_text_strips_ui_prefix() -> None:
    stage = StageSnapshot(
        stage="S2.1",
        title="Интро / рамка",
        agenda="снять напряжение",
        emotion="Рад знакомству.",
        step="Сейчас задам пару вопросов.",
        provider="test",
        model="test",
        confidence=None,
        readiness="green",
        readiness_label="Готово",
        score=1.0,
        summary="ok",
        next_action="Уточнить: Подскажите, удобно сейчас пару минут?",
        checks=(),
    )

    assert speakable_seller_text(stage) == "Подскажите, удобно сейчас пару минут?"


def test_parse_seller_response_accepts_json_text() -> None:
    assert (
        parse_seller_response('{"text":"Дмитрий, отправляю материал в Telegram."}')
        == "Дмитрий, отправляю материал в Telegram."
    )


def test_seller_reply_rejects_meta_and_repeated_boundary() -> None:
    assert seller_reply_is_usable(
        "Дмитрий, давайте уточним, где ломается внедрение.",
        [],
    )
    assert not seller_reply_is_usable(
        "дать короткий pitch блоками: feature -> outcome клиента -> check-in",
        [],
    )
    history = [
        (
            "Продавец",
            "Честно: подтвержденный кейс агентства или внешний аудит в стоимость я не заявляю; это не тот формат.",
        )
    ]
    assert not seller_reply_is_usable(
        "Честно: подтвержденный кейс агентства или внешний аудит в стоимость я не заявляю; это не тот формат.",
        history,
    )
    assert not seller_reply_is_usable(
        "Да, пропишем в договоре штрафные санкции и вернем пропорциональную часть стоимости.",
        [],
    )
    assert not seller_reply_is_usable(
        "В программе точно будет подробная информация о бэкграунде менторов.",
        [],
    )


def test_seller_fallback_honors_telegram_request() -> None:
    stage = StageSnapshot(
        stage="S3.5",
        title="Фоллоу-ап",
        agenda="follow-up",
        emotion="",
        step="",
        provider="test",
        model="test",
        confidence=None,
        readiness="red",
        readiness_label="Рано",
        score=0.0,
        summary="",
        next_action="Уточнить: зафиксировать причину, канал, срок и действие следующего контакта.",
        checks=(),
    )
    history = [("Клиент", "Скиньте материал в Telegram и не звоните.")]

    assert "Telegram" in seller_fallback_reply(stage, history)


def test_seller_fallback_answers_target_audience_directly() -> None:
    stage = StageSnapshot(
        stage="S2.2",
        title="Квалификация",
        agenda="current reality",
        emotion="",
        step="",
        provider="test",
        model="test",
        confidence=None,
        readiness="red",
        readiness_label="Рано",
        score=0.0,
        summary="direct answer debt",
        next_action="Уточнить: задайте вопрос о бизнесе",
        checks=(),
    )
    history = [("Клиент", "Я спросил: для собственников или для топов переслать?")]

    reply = seller_fallback_reply(stage, history)

    assert "собственник" in reply.lower()
    assert "топам" in reply.lower()


def test_seller_fallback_does_not_invent_mentor_cases() -> None:
    stage = StageSnapshot(
        stage="S3.1",
        title="Питч",
        agenda="pitch",
        emotion="",
        step="",
        provider="test",
        model="test",
        confidence=None,
        readiness="yellow",
        readiness_label="Почти",
        score=0.5,
        summary="direct answer debt",
        next_action="Показать ценность менторов.",
        checks=(),
    )
    history = [("Клиент", "Кто эти менторы конкретно? Фамилии, опыт, реальные кейсы?")]

    reply = seller_fallback_reply(stage, history)

    assert "выдумывать не буду" in reply.lower()
    assert "telegram" in reply.lower()


def test_repeated_concrete_request_blocks_more_clarifying_questions() -> None:
    history = [
        ("Клиент", "Как именно это работает, какой механизм?"),
        ("Продавец", "Что именно вызывает сомнение?"),
        ("Клиент", "Я прошу конкретный пример шаблона, как он выглядит?"),
    ]

    assert not seller_reply_is_usable(
        "Что именно для вас самое важное в таком шаблоне?",
        history,
    )
    assert "telegram" in seller_fallback_reply(
        StageSnapshot(
            stage="S3.4a",
            title="Возражение",
            agenda="objection",
            emotion="",
            step="",
            provider="test",
            model="test",
            confidence=None,
            readiness="red",
            readiness_label="Рано",
            score=0.0,
            summary="",
            next_action="классифицировать root reason",
            checks=(),
        ),
        history,
    ).lower()


def test_heuristic_stage_snapshot_advances_to_price_objection() -> None:
    history = [
        ("Продавец", "Участие стоит 99 000 рублей."),
        ("Клиент", "Дорого, как именно вы гарантируете результат?"),
    ]

    stage = heuristic_stage_snapshot("S3.3", history)

    assert stage.stage == "S3.4a"
    assert stage.provider == "heuristic"
    assert "гарант" in stage.next_action.lower()


def test_fallback_buyer_reply_skips_placeholder_seed_lines() -> None:
    profile = BuyerProfile(
        number=10,
        title="Холодный лид",
        persona="холодный лид",
        seed_client_lines=("[Отвечает.]", "Давайте коротко, у меня мало времени."),
    )

    assert (
        fallback_buyer_reply(profile=profile, turn_index=0, error="boom")
        == "Давайте коротко, у меня мало времени."
    )


def test_detect_terminal_outcome_finds_payment_intent() -> None:
    stage = StageSnapshot(
        stage="S3.3",
        title="Деньги",
        agenda="оплата",
        emotion="",
        step="",
        provider="test",
        model="test",
        confidence=None,
        readiness="green",
        readiness_label="Готово",
        score=1.0,
        summary="ok",
        next_action="",
        checks=(),
    )

    outcome, reason = detect_terminal_outcome(
        buyer_text="Да, давайте оформлять, пришлите счет.",
        seller_text="",
        stage=stage,
        turn_index=8,
        max_pairs=30,
    )

    assert outcome == "won_payment_intent"
    assert "payment" in reason.lower()

    outcome, reason = detect_terminal_outcome(
        buyer_text="Да, вопросов нет. Давайте переходить к оплате, что нужно для брони?",
        seller_text="",
        stage=stage,
        turn_index=8,
        max_pairs=30,
    )

    assert outcome == "won_payment_intent"
    assert "payment" in reason.lower()


def test_detect_terminal_outcome_keeps_conditional_objection_open() -> None:
    stage = StageSnapshot(
        stage="S3.2",
        title="Проверка ценности",
        agenda="value",
        emotion="",
        step="",
        provider="test",
        model="test",
        confidence=None,
        readiness="yellow",
        readiness_label="Почти",
        score=0.5,
        summary="active objection",
        next_action="",
        checks=(),
    )

    outcome, reason = detect_terminal_outcome(
        buyer_text="Если там просто красивые презентации, мне это не интересно. Механизм контроля какой?",
        seller_text="",
        stage=stage,
        turn_index=8,
        max_pairs=30,
    )

    assert outcome is None
    assert reason == ""


def test_detect_terminal_outcome_marks_qualified_refusal() -> None:
    stage = StageSnapshot(
        stage="S3.1",
        title="Питч",
        agenda="offer",
        emotion="",
        step="",
        provider="test",
        model="test",
        confidence=None,
        readiness="yellow",
        readiness_label="Почти",
        score=0.5,
        summary="fit gap",
        next_action="",
        checks=(),
    )

    outcome, reason = detect_terminal_outcome(
        buyer_text="Нет. Это мне не подходит, я ищу решение, а не семинар.",
        seller_text="",
        stage=stage,
        turn_index=12,
        max_pairs=30,
    )

    assert outcome == "qualified_refusal"
    assert "fit gap" in reason.lower()


def test_detect_terminal_outcome_stops_after_telegram_handoff() -> None:
    stage = StageSnapshot(
        stage="S3.5",
        title="Фоллоу-ап",
        agenda="follow-up",
        emotion="",
        step="",
        provider="test",
        model="test",
        confidence=None,
        readiness="green",
        readiness_label="Готово",
        score=1.0,
        summary="handoff",
        next_action="",
        checks=(),
    )

    outcome, reason = detect_terminal_outcome(
        buyer_text="Окей. Жду материал. Но дальше я сам напишу, если будет интересно.",
        seller_text="Отправляю карту в Telegram прямо сейчас.",
        stage=stage,
        turn_index=18,
        max_pairs=30,
    )

    assert outcome == "next_step_scheduled"
    assert "follow-up" in reason.lower()

    early_stage = StageSnapshot(
        stage="S2.5",
        title="Переход",
        agenda="transition",
        emotion="",
        step="",
        provider="test",
        model="test",
        confidence=None,
        readiness="red",
        readiness_label="Рано",
        score=0.0,
        summary="stage lagged behind terminal follow-up",
        next_action="",
        checks=(),
    )
    outcome, reason = detect_terminal_outcome(
        buyer_text="Хорошо, жду ссылку. Увидимся в четверг.",
        seller_text="Ссылку отправляю в Telegram, до встречи в четверг.",
        stage=early_stage,
        turn_index=18,
        max_pairs=30,
    )

    assert outcome == "next_step_scheduled"
    assert "follow-up" in reason.lower()

    outcome, reason = detect_terminal_outcome(
        buyer_text="Telegram.",
        seller_text="Отправляю материал в Telegram прямо сейчас.",
        stage=early_stage,
        turn_index=18,
        max_pairs=30,
    )

    assert outcome == "next_step_scheduled"
    assert "follow-up" in reason.lower()

    outcome, reason = detect_terminal_outcome(
        buyer_text="Отлично. Буду ждать, когда появится - присылайте.",
        seller_text="Как только будет готово краткое объяснение, пришлю его в Telegram.",
        stage=early_stage,
        turn_index=18,
        max_pairs=30,
    )

    assert outcome == "next_step_scheduled"
    assert "follow-up" in reason.lower()

    outcome, reason = detect_terminal_outcome(
        buyer_text="Если я увижу красивую схему вместо механики, я вешаю трубку. Покажите, как это работает.",
        seller_text="",
        stage=stage,
        turn_index=9,
        max_pairs=30,
    )

    assert outcome is None
    assert reason == ""


def test_render_metrics_lines_counts_dozhim_outcomes() -> None:
    from llm_service.app.paper_roleplay import RoleplayResult

    profile = BuyerProfile(1, "A", "persona", ())
    lines = render_metrics_lines(
        [
            RoleplayResult(
                profile=profile,
                turns=(),
                event_facts="facts",
                run_id="run-1",
                terminal_outcome="won_payment_intent",
                terminal_reason="ok",
                elapsed_secs=1.0,
            ),
            RoleplayResult(
                profile=profile,
                turns=(),
                event_facts="facts",
                run_id="run-2",
                terminal_outcome="closed_lost",
                terminal_reason="no",
                elapsed_secs=1.0,
            ),
        ]
    )

    assert "- Dozhim outcomes: 1/2" in lines
    assert any("`closed_lost`=1" in line for line in lines)
