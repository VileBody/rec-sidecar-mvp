# Paper roleplay index

## Metrics

- Scenarios: 10
- Dozhim outcomes: 2/10
- Average replies: 44.4
- Outcomes: `closed_lost`=6, `max_replies_reached`=2, `next_step_scheduled`=1, `won_payment_intent`=1
- Readiness labels: `Готово`=20, `Мало данных`=17, `Почти`=95, `Рано`=90
- Check hit/(hit+miss): 0.63 (490/779)
- Incomplete checks: 290
- Total scored checks: 1069

## Scenarios

- [01. Собственник застрял в операционке](scenario_01_scenario-01.md) - 17 turns/34 replies, final S3.4b Рано, outcome `next_step_scheduled`, 147.2s
- [02. Скептик после курсов и мастермайндов](scenario_02_scenario-02.md) - 26 turns/52 replies, final S3.5 Рано, outcome `closed_lost`, 197.4s
- [03. Цена, окупаемость, кассовый разрыв](scenario_03_scenario-03.md) - 24 turns/48 replies, final S3.4a Почти, outcome `closed_lost`, 193.5s
- [04. Успешный предприниматель, личное влияет на бизнес](scenario_04_scenario-04.md) - 16 turns/32 replies, final S2.4 Почти, outcome `closed_lost`, 111.6s
- [05. Сильная среда и страх попасть не к своему уровню](scenario_05_scenario-05.md) - 30 turns/60 replies, final S3.1 Готово, outcome `max_replies_reached`, 254.2s
- [06. Предприниматель из другого города, вход в Татарстан](scenario_06_scenario-06.md) - 28 turns/56 replies, final S3.5 Рано, outcome `closed_lost`, 217.6s
- [07. "Мне нужны инструменты, а не психологи"](scenario_07_scenario-07.md) - 30 turns/60 replies, final S3.4a Почти, outcome `max_replies_reached`, 228.8s
- [08. Нужно обсудить с партнером / женой / командой](scenario_08_scenario-08.md) - 22 turns/44 replies, final S2.5 Рано, outcome `closed_lost`, 180.8s
- [09. Горячий лид с мелкими тревогами](scenario_09_scenario-09.md) - 20 turns/40 replies, final S3.4a Почти, outcome `won_payment_intent`, 156.0s
- [10. Холодный / полухолодный лид по рекомендации](scenario_10_scenario-10.md) - 9 turns/18 replies, final S2.3 Рано, outcome `closed_lost`, 59.0s
