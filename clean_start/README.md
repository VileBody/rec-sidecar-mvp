# Clean Start

Clean Start is a Go/NATS rewrite sandbox for the live sales-coach loop. It has no UI. The goal is to make the backend event model explicit before moving the browser experience onto it.

## Roles

One binary runs different roles:

- `gateway`: external HTTP/SSE API and session reducer
- `seller-worker`: listens to client partial/final events and streams seller deltas
- `stage-worker`: listens to client partial/final events and publishes stage updates
- `scorecard-worker`: listens to committed stages and publishes scorecard updates
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

## LLM Sidecar

Workers use `COACH_LLM_SERVICE_URL` when available. If the sidecar is unavailable, workers publish deterministic fallback outputs so that NATS/session behavior remains testable.

In Kubernetes the default should be:

```text
COACH_LLM_SERVICE_URL=http://llm-helper
```
