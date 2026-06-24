# Extracted hostile-client prompts

Эта папка содержит текстовые prompt-шаблоны для тестового приложения с вредными/сложными клиентами.

Источники:

- `/Users/ergin/Desktop/rec-sidecar-mvp/scripts/live_client_voice_agent.py`
- `/Users/ergin/Desktop/rec-sidecar-mvp/scripts/live_client_chat_app.py`
- `/Users/ergin/Desktop/rec-sidecar-mvp/clean_start/internal/clean/test_agent.go`
- `/Users/ergin/Desktop/rec-sidecar-mvp/clean_start/internal/clean/llm.go`
- `/Users/ergin/Desktop/rec-sidecar-mvp/llm_service/app/paper_roleplay.py`
- `/Users/ergin/Desktop/rec-sidecar-mvp/sales_scripts/glubina_kazan_10_call_scripts_v1.md`

Файлы:

- `01_live_client_voice_agent.md` — локальный voice/chat клиент: system prompt, modes, user prompt template.
- `02_paper_roleplay_buyer_agent.md` — buyer-agent из paper roleplay: общий system prompt и user prompt template.
- `03_complex_buyer_profiles.md` — 10 приватных профилей сложных покупателей.
- `04_clean_start_test_agent.md` — clean_start Go test-agent prompts.
- `05_reference_script_personas.md` — краткая карта 10 seed-сценариев из `glubina_kazan_10_call_scripts_v1.md`.
- `06_reference_client_arcs.md` — первые клиентские реплики, которые используются как reference arc.

Важно: `live_client_chat_app.py` использует те же prompt-функции из `live_client_voice_agent.py`, поэтому отдельного prompt-файла для него нет.
