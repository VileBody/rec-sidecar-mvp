# Clean Start

Clean Start is a Go/NATS rewrite sandbox for the live sales-coach loop. It has no UI. The goal is to make the backend event model explicit before moving the browser experience onto it.

## Roles

One binary runs different roles:

- `gateway`: external HTTP/SSE API and session reducer
- `seller-worker`: listens to client partial/final events and streams seller deltas
- `assist-worker`: manual "Помоги" stream with fast emotional opener + slow constructive step
- `stage-worker`: listens to client partial/final events and publishes stage updates
- `scorecard-worker`: listens to committed stages and publishes scorecard updates
- `test-agent`: isolated voice roleplay API; it does not write into `clean.session.*`
- `all`: local smoke role that runs all components in one process

## Event Subjects

Subjects are structured as:

```text
clean.session.<session_id>.<event_type>
```

Examples:

```text
clean.session.sess-abc.client.partial
clean.session.sess-abc.seller.delta
clean.session.sess-abc.assist.delta
clean.session.sess-abc.stt.final
clean.session.sess-abc.stage.committed
clean.session.sess-abc.scorecard.update
```

## Local Smoke

```bash
docker compose -f clean_start/docker-compose.yml up --build
```

Create a session:

```bash
curl -s http://127.0.0.1:8110/v1/sessions -X POST -d '{}' | jq
```

Send a seller line:

```bash
curl -s http://127.0.0.1:8110/v1/sessions/<session_id>/events \
  -H 'Content-Type: application/json' \
  -d '{"type":"seller.input","text":"Здравствуйте, давайте за пару минут поймем, есть ли смысл обсуждать участие."}'
```

Send client partial/final:

```bash
curl -s http://127.0.0.1:8110/v1/sessions/<session_id>/events \
  -H 'Content-Type: application/json' \
  -d '{"type":"client.partial","text":"Сомневаюсь, что план будет рабочим"}'

curl -s http://127.0.0.1:8110/v1/sessions/<session_id>/events \
  -H 'Content-Type: application/json' \
  -d '{"type":"client.final","text":"Сомневаюсь, что план будет рабочим, до внедрения я обычно не дохожу."}'
```

Stream session events:

```bash
curl -N http://127.0.0.1:8110/v1/sessions/<session_id>/stream
```

Request the manual "Помоги" stream:

```bash
curl -s http://127.0.0.1:8110/v1/sessions/<session_id>/events \
  -H 'Content-Type: application/json' \
  -d '{"type":"assist.request","trigger":"button"}'
```

Post STT text events when a browser/system-audio client already has a transcript:

```bash
curl -s http://127.0.0.1:8110/v1/sessions/<session_id>/events \
  -H 'Content-Type: application/json' \
  -d '{"type":"stt.partial","role":"client","source":"system-audio","text":"Сомневаюсь, что план будет рабочим"}'
```

## Isolated Test Agent

`test-agent` is the separate app for roleplay testing. It is intentionally not connected to the main coach session state.

Local compose exposes it on `http://127.0.0.1:8111`:

```bash
curl -s http://127.0.0.1:8111/v1/test-agent/sessions -X POST | jq

curl -s http://127.0.0.1:8111/v1/test-agent/sessions/<test_session_id>/turn \
  -H 'Content-Type: application/json' \
  -d '{"seller_text":"Здравствуйте, хочу понять, есть ли вам смысл идти на событие."}' | jq
```

The websocket endpoint for a local desktop/browser player is:

```text
ws://127.0.0.1:8111/v1/test-agent/ws
```

In Kubernetes the planned public NodePorts are:

```text
coach gateway: http://<node-ip>:30916
test agent:    http://<node-ip>:30917
```

The test agent needs `INWORLD_API_KEY` for TTS/STT. Without it, `/healthz` still works but `/turn` returns a clear missing-key error.

## Browser Audio Capture

The browser UI treats microphone and system audio as two different sources:

- microphone stream -> seller
- screen/tab/system audio stream -> client

Gateway STT provider selection:

- `CLEAN_START_STT_PROVIDER=auto` uses native Soniox first when `SONIOX_API_KEY` is present, otherwise falls back to Inworld.
- `CLEAN_START_STT_PROVIDER=soniox` forces native Soniox `stt-rt-v5`.
- `CLEAN_START_STT_PROVIDER=inworld` forces the old Inworld STT proxy.

Set `CLEAN_START_COACH_ENABLED=false` for STT-only debugging. In that mode sessions do not create an opener, manual seller/help requests are ignored, and the UI shows the diarized transcript with timestamps instead of LLM suggestions.

Seller suggestions use the ZAI gate -> Gemini reply loop. Client partials are first filtered by local growth thresholds. If a seller reply is already visible, the worker calls `/v1/coach/live` with `current_text`; Cerebras/ZAI can return `skip` to keep the current reply, or `suggest` to let Vertex/Gemini replace it. Final client turns and stage changes force a fresh Gemini reply.

Stage detection runs on client partials, but partials are coalesced so one spoken phrase cannot create a queue of parallel LLM calls. Tune with `CLEAN_START_STAGE_PARTIAL_INTERVAL_MS` (default `2200`) and `CLEAN_START_MIN_STAGE_GROWTH` (default `24`).

This is reliable only when the microphone does not hear the remote participant, for example when the seller uses headphones. If laptop speakers leak into the microphone, the gateway now applies a text echo filter in both directions:

- system audio that repeats a recent seller line is rejected as seller echo
- microphone audio that repeats a recent client line is rejected as client echo

This filter is a guardrail, not true diarization. For experiments:

```bash
python3 scripts/diagnostics/bench_hf_diarization.py --dry-run
HF_TOKEN=... python3 scripts/diagnostics/bench_hf_diarization.py

python3 scripts/diagnostics/gemini_retro_diarization.py

printf 'SONIOX_API_KEY=...\n' > .env.local
python3 scripts/diagnostics/check_soniox_diarization.py

set -a; source .env.local; set +a
docker compose -f clean_start/docker-compose.yml up --build clean-start
```

`bench_hf_diarization.py` measures pyannote/HF diarization latency on CPU. `gemini_retro_diarization.py` uses Gemini retrospectively on a WAV and should stay out of the live STT critical path. `check_soniox_diarization.py` calls native Soniox `stt-rt-v5` directly, bypassing the Inworld proxy. In the local WebSocket client, raw PCM uses `audio_format=s16le`; finish the stream with an empty text frame. An empty binary frame caused Soniox to process the audio but return `408 request_timeout`.

## LLM Sidecar

Workers use `COACH_LLM_SERVICE_URL` when available. If the sidecar is unavailable, workers publish deterministic fallback outputs so that NATS/session behavior remains testable.

In Kubernetes the default should be:

```text
COACH_LLM_SERVICE_URL=http://llm-helper
```
