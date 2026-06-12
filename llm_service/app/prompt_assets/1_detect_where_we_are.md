# Prompt 1: Detect Where We Are

## Вход
- Последние реплики клиента
- Досье клиента (pre-call brief)
- История звонка
- Слоты: frame_set, current_context, pain, target, gap, motive, permission_to_pivot

## Задача
Определить текущую стадию звонка и состояние клиента

## Стадии и критерии

S2.1 — Frame / Установка фрейма
- Цель: снять напряжение, объяснить правила CustDev
- Критерий входа: звонок начался, клиент ещё не понял формат
- Критерий выхода: frame_set = true

S2.2 — Current Reality / Точка А
- Цель: собрать факты о текущей ситуации и боли
- Критерий входа: frame_set = true, current_context = unknown
- Критерий выхода: current_context = known, pain_detected = true

S2.3 — Target & Gap / Точка Б
- Цель: выявить цели клиента и разрыв
- Критерий входа: current_context known, target/gap unknown
- Критерий выхода: target = known, gap = known

S2.4 — Motive / Истинный мотив
- Цель: понять, зачем клиент пришёл
- Критерий входа: target/gap known, motive unknown
- Критерий выхода: motive = known, trust_signal = true

S2.5 — Pivot / Разворот
- Цель: разрешение на переход к офферу / презентации
- Критерий входа: текущий контекст, pain, target, gap, motive известны
- Критерий выхода: permission_to_pitch = true

S3.1 — Pitch / Озвучивание оффера
- Цель: озвучить оффер, чек, партнерскую модель
- Критерий входа: permission_to_pitch = true
- Критерий выхода: offer_explained = true, price_named = true

S3.2 — Value Test / Проверка ценности
- Цель: проверить, видит ли клиент ценность продукта
- Критерий входа: offer_explained = true, value_confirmed unknown
- Критерий выхода: client response received

S3.3 — Bank Option / Downsell
- Цель: ценность подтверждена, денег нет
- Критерий входа: value_confirmed = true, cash_available = false, main_objection = "нет денег"
- Критерий выхода: bank_option_presented = true, client_reaction_received = true

S3.4a — Objection Clarifier
- Цель: выяснить истинную причину возражения ("цена или ценность?")
- Критерий входа: price named, client says "надо подумать", value unknown
- Критерий выхода: objection_type classified

S3.4b — Second Zoom Parking
- Цель: снять давление, запланировать второй созвон
- Критерий входа: client refusal / credit fear / soft no
- Критерий выхода: second call scheduled OR hard refusal

S3.5 — Follow-up / Потеряшки
- Цель: выяснить причину отказа, сохранить лид
- Критерий входа: client disappeared OR sent refusal
- Критерий выхода: refusal reason collected OR next contact scheduled OR lead dead