# rec-sidecar-mvp

Minimal Rust desktop MVP:

- native `egui/eframe` app
- no webview
- main REC dashboard
- historical runs list
- separate transcription window
- transcript bubbles
- stop / continue / save / save and exit controls
- ASR worker connected through `std::sync::mpsc`
- native Rust Inworld Soniox STT
- manual sales coach Help button and chat
- default mock ASR fallback

## Run

```bash
docker compose up --build llm-service
cargo run
```

The top `REC` button starts a new live run. The transcription window has:

- `Остановить` / `Продолжить`
- `Сохранить`
- `Сохранить и выйти`
- `Помоги`

Saved runs are written to `runs/`. Historical exports are copied to `exports/`.
If `.env` contains `INWORLD_API_KEY`, the Rust app captures the microphone directly and streams audio to Inworld STT with model `soniox/stt-rt-v4`.
If `INWORLD_API_KEY` is not set, the app uses an in-process mock ASR fallback.
Partials are shown by default. Set `INWORLD_SHOW_PARTIALS=false` to show only final transcript chunks. To avoid late finals during long speech, the client sends semantic STT `end_turn` every `INWORLD_FORCE_END_TURN_MS=4000` ms. Set it to `0` to disable forced turn cuts. Audio batching/backlog is controlled separately by `INWORLD_AUDIO_MAX_BATCH_MS` and `INWORLD_AUDIO_FLUSH_LATENCY_MS`.
Transient Inworld/network failures are retried by default with `INWORLD_STT_MAX_RECONNECTS=3`, `INWORLD_STT_RECONNECT_BACKOFF_MS=750`, `INWORLD_STT_RECONNECT_MAX_BACKOFF_MS=5000`, and `INWORLD_STT_CONNECT_TIMEOUT_MS=10000`; microphone/config/auth failures remain terminal.
Debug timing logs are written to `logs/rec-sidecar.log` by default. Set `REC_SIDECAR_LOG=off` to disable or `REC_SIDECAR_LOG_RAW=true` to include raw server messages.

## Sales Coach

LLM calls are handled by a FastAPI sidecar on `COACH_LLM_SERVICE_URL` (default `http://127.0.0.1:8088`). Start it with:

```bash
docker compose up --build llm-service
curl http://127.0.0.1:8088/healthz
```

The Rust app keeps UI state, ASR, context building, queues, Help stages, and chat history. The FastAPI service owns prompts, Cerebras/Vertex auth, provider fallback, prompt-cache retry, and model labels.

## Timeweb Kubernetes Deploy

The cloud deployment uses the manifest in `k8s/llm-helper.yaml`.

```bash
kubectl create namespace rec-sidecar --dry-run=client -o yaml | kubectl apply -f -
kubectl -n rec-sidecar create secret generic llm-helper-env --from-env-file=.deploy/llm-helper-secret.env --dry-run=client -o yaml | kubectl apply -f -
kubectl -n rec-sidecar create secret docker-registry ghcr-pull --docker-server=ghcr.io --docker-username=<github-user> --docker-password=<github-token> --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s/llm-helper.yaml
```

`llm-helper` is exposed as `NodePort` `30914`, so the desktop app can use `COACH_LLM_SERVICE_URL=http://<node-external-ip>:30914`.

Auto live suggestions are disabled by default in code (`COACH_AUTO_SUGGESTIONS=false`) to avoid provider rate limits. The manual `Помоги` flow is pull-based:

- UI sends an ASR `Flush { reason: "help" }` command.
- The Inworld sender emits `end_turn`, so the latest speech can settle.
- After `COACH_HELP_CONTEXT_DELAY_MS=300`, the UI freezes a compact help context and sends it to the coach.
- The coach shows staged Help status: context freeze, read-aloud phrase, constructive next step, done, fallback, or error.
- Fast opener calls the FastAPI sidecar, which races streaming Cerebras `zai-glm-4.7`, Cerebras `gpt-oss-120b`, and Vertex `gemini-3.5-flash` with `VERTEX_THINKING_LEVEL=low`; the first model to emit a token is shown. Slow constructive help streams from Vertex Gemini through the same sidecar and is capped in the Rust client by `COACH_HELP_CONSTRUCTIVE_TIMEOUT_MS=20000`.
- Help output is shaped as `Сказать сейчас` first, then a streaming `Следующий ход` from Vertex when constructive help is available.

Coach logs go to `logs/rec-sidecar.coach.log`. Cerebras `prompt_cache_key` is an optimization inside the sidecar only; if a provider rejects it, the service retries without the key.

The Rust STT client sends this config by default:

```json
{
  "transcribe_config": {
    "modelId": "soniox/stt-rt-v4",
    "audioEncoding": "LINEAR16",
    "sampleRateHertz": 16000,
    "numberOfChannels": 1,
    "enableLanguageDetection": true,
    "enableSpeakerDiarization": true
  }
}
```

## VSCode

Open the folder:

```bash
code .
```

Then run the VSCode task `cargo run` or use terminal.

## Checks

```bash
scripts/check.sh
python -m venv .venv
. .venv/bin/activate
pip install -r llm_service/requirements.txt
python -m pytest llm_service/tests
```

Runs `cargo fmt -- --check`, `cargo clippy --all-targets -- -D warnings`, and `cargo test`.

## Native ASR

For MVP the UI does **not** embed the STT model. It captures mic audio through Rust `cpal`, converts it to LINEAR16 16 kHz mono, streams it to Inworld over WebSocket, and receives transcript chunks back into the UI through `std::sync::mpsc`.

Set `INWORLD_MIC_DEVICE` to an input device index or partial device name if the default microphone is not the one you want.
Set `INWORLD_SHOW_PARTIALS=false` when you want to hide non-final transcript messages in the UI.

## Architecture

```text
src/main.rs
  thin native app entrypoint

src/lib.rs
  library wiring for the app, ASR, coach, session, context, UI, and platform modules

src/app.rs
  REC app state, event handling, sidecar window orchestration, and high-level workflows

src/session.rs
  run persistence, history loading, exports, and injectable app paths for tests

src/context.rs
  saved transcript rendering and Help/Chat/live-coach context construction

src/ui.rs
  egui drawing helpers for transcript bubbles, coach chat markdown, and panel layout

src/platform.rs
  platform-specific window behavior, including macOS all-spaces pinning

src/asr.rs
  ASR worker lifecycle, native mic capture, Inworld WebSocket STT client, and event bridge

src/asr/audio.rs
  audio batching, backlog flushing, resampling, and LINEAR16 chunk production

src/asr/config.rs
  Inworld STT configuration, transcribe config payload, and SOCKS proxy parsing

src/coach.rs
  sales coach worker orchestration, Help flow, and HTTP/SSE client for the FastAPI LLM sidecar

src/coach/streaming.rs
  small SSE parser used by the Rust sidecar client

llm_service/app
  FastAPI LLM sidecar with prompts, Cerebras/Vertex HTTP clients, provider fallback, and SSE responses
```

## macOS note

First build can take a while because Cargo downloads and compiles dependencies.
