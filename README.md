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
On macOS 14.2+, set `REC_SIDECAR_SYSTEM_AUDIO=true` to mix system output into the same ASR stream through a Core Audio process tap. This is the preferred path when you are on headphones and still want the coach to hear the remote participant. macOS will prompt for System Audio Recording permission the first time.
Set `ASR_PROVIDER=gemini-live` to route microphone audio to the FastAPI sidecar at `COACH_LLM_SERVICE_URL`, which then opens Vertex Gemini Live native-audio WebSocket ASR. This experimental path keeps the same Rust transcript events and uses Gemini Live `inputAudioTranscription` plus a `report_transcript` function-call fallback. Configure the sidecar with `VERTEX_LIVE_ASR_MODEL=gemini-live-2.5-flash-native-audio` and `VERTEX_LIVE_ASR_LOCATION=us-central1`; native-audio Live is regional, so `global` is not valid for this endpoint.
Partials are shown by default. Set `INWORLD_SHOW_PARTIALS=false` to show only final transcript chunks. To avoid late finals during long speech, the client sends semantic STT `end_turn` every `INWORLD_FORCE_END_TURN_MS=4000` ms. Set it to `0` to disable forced turn cuts. Audio batching/backlog is controlled separately by `INWORLD_AUDIO_MAX_BATCH_MS` and `INWORLD_AUDIO_FLUSH_LATENCY_MS`.
Transient Inworld/network failures are retried by default with `INWORLD_STT_MAX_RECONNECTS=3`, `INWORLD_STT_RECONNECT_BACKOFF_MS=750`, `INWORLD_STT_RECONNECT_MAX_BACKOFF_MS=5000`, and `INWORLD_STT_CONNECT_TIMEOUT_MS=10000`; microphone/config/auth failures remain terminal.
Debug timing logs are written to `logs/rec-sidecar.log` by default. Set `REC_SIDECAR_LOG=off` to disable or `REC_SIDECAR_LOG_RAW=true` to include raw server messages.

## Sales Coach

LLM calls are handled by a FastAPI sidecar on `COACH_LLM_SERVICE_URL` (default `http://127.0.0.1:8088`). Start it with:

```bash
docker compose up --build llm-service
curl http://127.0.0.1:8088/healthz
```

The Rust app keeps UI state, ASR, context building, queues, Help stages, chat history, and the stage overlay window. The FastAPI service owns prompts, Cerebras/Vertex auth, provider fallback, prompt-cache retry, model labels, and `/v1/coach/stage` intelligence. Stage detection runs every `COACH_STAGE_DETECT_INTERVAL_MS=5000` ms by default and maps the detected sales stage to the fixed agenda from `llm_service/app/prompt_assets/3_current_stage_agenda.md`.

`/v1/coach/stage` can run in two transports:

- `COACH_INTELLIGENCE_TRANSPORT=rest` uses a cheap frequent state machine: Cerebras `CEREBRAS_STAGE_MODEL` (default `zai-glm-4.7`) detects the stage, and if the detected stage is unchanged the sidecar returns `204 No Content` without recomputing anything. Only when the stage moves forward does the sidecar evaluate the new stage scorecard on Vertex `VERTEX_SCORECARD_MODEL` (default `gemini-3.5-flash`) with `VERTEX_SCORECARD_THINKING_LEVEL=minimal`.
- `COACH_INTELLIGENCE_TRANSPORT=live` uses one persistent Vertex Gemini Live WebSocket session per `run_id` for stage, tactical next action, and scorecard metrics in a single turn. Configure it with `VERTEX_LIVE_MODEL=gemini-2.0-flash-live-preview-04-09` for text-to-text JSON intelligence and `VERTEX_LIVE_TIMEOUT_SECS=20`; if Live fails, the sidecar falls back to the REST flow for that request.

The sidecar also exposes `WS /v1/asr/gemini-live` for the optional Gemini Live ASR mode. The Rust app still sends LINEAR16 16 kHz mono chunks in the existing STT envelope; the sidecar converts them to Vertex Live `realtimeInput.audio` frames and returns transcript envelopes compatible with the current UI.

For the audio-first stage experiment, keep the visible transcript on Inworld (`ASR_PROVIDER=auto`) and set `COACH_STAGE_AUDIO_LIVE=true`. Rust then opens a second, non-visible mic stream to `WS /v1/coach/stage/live-audio`; the sidecar forwards audio to Vertex `gemini-live-2.5-flash-native-audio` and maps function calls back into the existing stage overlay. Configure `VERTEX_LIVE_STAGE_MODEL=gemini-live-2.5-flash-native-audio`, `VERTEX_LIVE_STAGE_LOCATION=us-central1`, and keep `COACH_STAGE_REST_FALLBACK=false` to avoid the old REST poller racing the audio lane.

## Timeweb Kubernetes Deploy

The cloud deployment uses the manifest in `k8s/llm-helper.yaml`.

```bash
kubectl create namespace rec-sidecar --dry-run=client -o yaml | kubectl apply -f -
kubectl -n rec-sidecar create secret generic llm-helper-env --from-env-file=.deploy/llm-helper-secret.env --dry-run=client -o yaml | kubectl apply -f -
kubectl -n rec-sidecar create secret docker-registry ghcr-pull --docker-server=ghcr.io --docker-username=<github-user> --docker-password=<github-token> --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s/llm-helper.yaml
```

`llm-helper` is exposed as `NodePort` `30914`, so the desktop app can use `COACH_LLM_SERVICE_URL=http://<node-external-ip>:30914`.

The browser-only roleplay UI can be deployed separately with:

```bash
kubectl apply -f k8s/fresh-start-chat.yaml
```

It is exposed as `NodePort` `30915`.

Auto live suggestions are disabled by default in code (`COACH_AUTO_SUGGESTIONS=false`) to avoid provider rate limits. The manual `Помоги` flow is pull-based:

- UI sends an ASR `Flush { reason: "help" }` command.
- The Inworld sender emits `end_turn`, so the latest speech can settle.
- After `COACH_HELP_CONTEXT_DELAY_MS=300`, the UI freezes a compact help context and sends it to the coach.
- The coach shows staged Help status: context freeze, read-aloud phrase, constructive next step, done, fallback, or error.
- Fast opener calls the FastAPI sidecar, which starts streaming Vertex `gemini-3.5-flash` (`VERTEX_THINKING_LEVEL=low`), Cerebras `zai-glm-4.7`, and Cerebras `gpt-oss-120b` in parallel. For up to `COACH_HELP_OPENER_TIMEOUT_MS=4000`, selection prefers Gemini, then ZAI, then OSS; after the window, the best ready lower-priority stream is used. Slow constructive help streams from Vertex Gemini through the same sidecar and is capped in the Rust client by `COACH_HELP_CONSTRUCTIVE_TIMEOUT_MS=20000`.
- Help output is shaped as `Сказать сейчас` first, then a streaming `Следующий ход` from Vertex when constructive help is available.

Coach logs go to `logs/rec-sidecar.coach.log`. Cerebras `prompt_cache_key` is an optimization inside the sidecar only; if a provider rejects it, the service retries without the key.

## Voice Roleplay Smoke

The Kazan event roleplay source is `sales_scripts/glubina_kazan_10_call_scripts_v1.md` and is based on `https://glubina-community.ru/kazan`.

Dry-run a scripted client persona while asking the sidecar for the seller's next read-aloud phrase:

```bash
python3 scripts/coach_roleplay_tts_harness.py --dry-run --script 1 --max-client-turns 3
```

Render a short two-voice Gemini TTS smoke through Vertex/ADC:

```bash
python3 scripts/coach_roleplay_tts_harness.py \
  --coach-source source \
  --script 1 \
  --max-client-turns 1 \
  --vertex \
  --location global
```

Add `--play` to play each generated WAV with `afplay`; route system audio into the selected ASR input device when you want the desktop app to hear the roleplay.

For a lightweight local text-to-voice rehearsal UI, run:

```bash
python3 scripts/live_client_chat_app.py --host 127.0.0.1 --port 8097
```

Open `http://127.0.0.1:8097`, type the seller line, and the app will:

- synthesize the seller line with `INWORLD_TTS_SELLER_VOICE`
- play it locally through the system output
- generate the buyer reply from the chosen script/persona
- synthesize the buyer reply with `INWORLD_TTS_CLIENT_VOICE`
- play the buyer reply locally and save both WAVs under `logs/live_client_chat/`

This mode pairs well with `REC_SIDECAR_SYSTEM_AUDIO=true cargo run` because the Rust app can now listen to the desktop/system output instead of relying on mic bleed.

For the browser-only seller/client roleplay loop, run:

```bash
python3 fresh-start/chat_loop_app.py --host 127.0.0.1 --port 8101
```

Open `http://127.0.0.1:8101`. The page keeps two async loops alive:

- the buyer reply streams in as text
- a parallel ZAI -> Gemini lane refreshes stage, scorecard, and the seller's next line while the client reply is still unfolding
- browser updates are pushed over SSE from `/api/session/stream`, so the UI no longer polls snapshots in the background

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
  ASR worker lifecycle, native mic/system-audio capture, Inworld WebSocket STT client, and event bridge

src/asr/audio.rs
  audio batching, backlog flushing, resampling, mixing, and LINEAR16 chunk production

src/asr/config.rs
  Inworld STT configuration, transcribe config payload, and SOCKS proxy parsing

src/asr/system_audio.rs
  macOS Core Audio tap bridge for system-output capture

src/coach.rs
  sales coach worker orchestration, Help flow, and HTTP/SSE client for the FastAPI LLM sidecar

src/coach/streaming.rs
  small SSE parser used by the Rust sidecar client

llm_service/app
  FastAPI LLM sidecar with prompts, Cerebras/Vertex HTTP clients, provider fallback, and SSE responses
```

## macOS note

First build can take a while because Cargo downloads and compiles dependencies.
