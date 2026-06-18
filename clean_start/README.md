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

## LLM Sidecar

Workers use `COACH_LLM_SERVICE_URL` when available. If the sidecar is unavailable, workers publish deterministic fallback outputs so that NATS/session behavior remains testable.

In Kubernetes the default should be:

```text
COACH_LLM_SERVICE_URL=http://llm-helper
```
