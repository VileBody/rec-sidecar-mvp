# Prompt 1: Detect Where We Are

## Вход
- Последние реплики клиента
- Досье клиента (pre-call brief)
- История звонка
- Слоты: frame_set, current_context, pain, target, gap, motive, permission_to_pivot

## Задача
Определить текущую стадию звонка и состояние клиента

## Стадии и критерии

S2.1 — Интро / рамка
- Цель: снять напряжение, объяснить правила CustDev
- Критерий входа: звонок начался, клиент ещё не понял формат
- Критерий выхода: frame_set = true

S2.2 — Квалификация: текущая ситуация
- Цель: собрать факты о текущей ситуации и боли
- Критерий входа: frame_set = true, current_context = unknown
- Критерий выхода: current_context = known, pain_detected = true

S2.3 — Квалификация: цель и разрыв
- Цель: выявить цели клиента и разрыв
- Критерий входа: current_context known, target/gap unknown
- Критерий выхода: target = known, gap = known

S2.4 — Мотив / доверие
- Цель: понять, зачем клиент пришёл
- Критерий входа: target/gap known, motive unknown
- Критерий выхода: motive = known, trust_signal = true

S2.5 — Переход к офферу
- Цель: разрешение на переход к офферу / презентации
- Критерий входа: текущий контекст, pain, target, gap, motive известны
- Критерий выхода: permission_to_pitch = true

S3.1 — Питч
- Цель: озвучить оффер и связать формат с задачей клиента
- Критерий входа: permission_to_pitch = true
- Критерий выхода: offer_explained = true, client_checkin_done = true

S3.2 — Проверка ценности
- Цель: проверить, видит ли клиент ценность продукта
- Критерий входа: offer_explained = true, value_confirmed unknown
- Критерий выхода: value_confirmed = true OR value_objection_classified = true

S3.3 — Цена / условия участия
- Цель: после value buy-in назвать цену/условия и проверить реакцию
- Критерий входа: value_confirmed = true, price_named = false OR price_named = true, payment_reaction unknown
- Критерий выхода: price_named = true, client_reaction_received = true

S3.4a — Возражение: уточняющий вопрос
- Цель: выяснить истинную причину возражения ("цена или ценность?")
- Критерий входа: price/terms named, client raises price/value/time/trust/risk/stakeholder objection
- Критерий выхода: objection_type classified

S3.4b — Возражение: мягкий перенос
- Цель: снять давление, запланировать второй созвон
- Критерий входа: client refusal / credit fear / soft no
- Критерий выхода: second call scheduled OR hard refusal

S3.5 — Фоллоу-ап / возврат лида
- Цель: выяснить причину отказа, сохранить лид
- Критерий входа: client disappeared OR sent refusal
- Критерий выхода: refusal reason collected OR next contact scheduled OR lead dead
