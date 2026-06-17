# Paper roleplay index

## Metrics

- Scenarios: 10
- Dozhim outcomes: 4/10
- Average replies: 37.4
- Outcomes: `closed_lost`=4, `max_replies_reached`=1, `next_step_scheduled`=1, `unresolved_with_next_step_attempt`=1, `won_payment_intent`=3
- Readiness labels: `Готово`=20, `Мало данных`=20, `Почти`=100, `Рано`=47
- Check hit/(hit+miss): 0.74 (467/631)
- Incomplete checks: 276
- Total scored checks: 907

## Scenarios

- [01. Собственник застрял в операционке](scenario_01_scenario-01.md) - 22 turns/44 replies, final S3.5 Рано, outcome `closed_lost`, 172.0s
- [02. Скептик после курсов и мастермайндов](scenario_02_scenario-02.md) - 15 turns/30 replies, final S3.4a Рано, outcome `closed_lost`, 115.2s
- [03. Цена, окупаемость, кассовый разрыв](scenario_03_scenario-03.md) - 9 turns/18 replies, final S3.4b Рано, outcome `next_step_scheduled`, 70.1s
- [04. Успешный предприниматель, личное влияет на бизнес](scenario_04_scenario-04.md) - 30 turns/60 replies, final S3.5 Готово, outcome `max_replies_reached`, 228.9s
- [05. Сильная среда и страх попасть не к своему уровню](scenario_05_scenario-05.md) - 14 turns/28 replies, final S2.4 Рано, outcome `closed_lost`, 101.1s
- [06. Предприниматель из другого города, вход в Татарстан](scenario_06_scenario-06.md) - 24 turns/48 replies, final S3.4a Готово, outcome `won_payment_intent`, 238.9s
- [07. "Мне нужны инструменты, а не психологи"](scenario_07_scenario-07.md) - 17 turns/34 replies, final S3.4a Почти, outcome `won_payment_intent`, 173.9s
- [08. Нужно обсудить с партнером / женой / командой](scenario_08_scenario-08.md) - 30 turns/60 replies, final S3.5 Рано, outcome `unresolved_with_next_step_attempt`, 316.3s
- [09. Горячий лид с мелкими тревогами](scenario_09_scenario-09.md) - 13 turns/26 replies, final S3.3 Готово, outcome `won_payment_intent`, 137.5s
- [10. Холодный / полухолодный лид по рекомендации](scenario_10_scenario-10.md) - 13 turns/26 replies, final S2.2 Рано, outcome `closed_lost`, 143.8s
