# Botamin voice demo

This directory is the bounded admin-cluster exception described by
[`ADR 0008`](../../../docs/adr/0008-botamin-voice-demo-exception.md). It is not a
generic PaaS tenant deployment and does not close the platform beta gates.

The public path is:

```text
voicechat.teamgenius.ru
  -> retained legacy IPv4 / Caddy in rec-sidecar
  -> botamin-voice.botamin-voice.svc.cluster.local:80
  -> Botamin API on port 8080
  -> api.inworld.ai:443 for protected custom TTS-2
  -> botamin-postgres.botamin-voice.svc.cluster.local:5432
```

The bundle intentionally does not contain secret values, the registry pull
secret, public DNS changes, or a new ingress controller. The Caddy host block is
kept in `deploy/legacy/edge-caddy-override.yaml`. Caddy ACME storage and its
deployment mount are managed as an edge prerequisite, outside this
kustomization.

## Release inputs

Use the dedicated cluster kubeconfig:

```bash
export KUBECONFIG="${KUBECONFIG:-$(pwd)/infra/timeweb/ai-native-paas-test.kubeconfig}"
```

The following resources must already exist:

- `botamin-voice-secrets` in namespace `botamin-voice`, created through the
  approved secret workflow;
- `craas-ai-native-paas-registry` in namespace `botamin-voice`;
- the retained Caddy edge in namespace `rec-sidecar`;
- durable Caddy `/data` storage for ACME account and certificate state;
- an `A` record for `voicechat.teamgenius.ru` pointing at the retained legacy
  edge IPv4 (`85.193.87.39`).

`botamin-voice-secrets` must contain exactly the runtime inputs expected by the
manifests:

```text
POSTGRES_PASSWORD
DATABASE_URL
VAPI_TOOL_BEARER_TOKEN
VAPI_ADMIN_BEARER_TOKEN
PII_ENCRYPTION_KEY
VAPI_PUBLIC_KEY
VAPI_ASSISTANT_ID
INWORLD_API_KEY
```

Do not put Soniox or the Vapi private key into this Kubernetes workload. Those
provider credentials belong in Vapi. The backend reads Inworld for TTS-2. The
public demo uses the deterministic mock calendar; contact validation is also
deterministic and does not call an additional LLM. Never pass a secret value on
a command line or commit it to this repository.

Kubernetes NetworkPolicy cannot allow an FQDN. The API pod therefore has
TCP/443 egress to public IPv4 while private, link-local, loopback, carrier-grade
NAT, multicast, and reserved ranges remain excluded. Database egress stays
limited to the namespace-local PostgreSQL selector.

Both the application and PostgreSQL images are immutable digests. A release
must use the same application digest in `deployment.yaml` and
`migration-job.template.yaml`.

## Deployment order

The migration Job is deliberately excluded from `kustomization.yaml`. Apply the
database prerequisites first:

```bash
kubectl apply -f deploy/demo/botamin-voice/namespace.yaml
kubectl apply -f deploy/demo/botamin-voice/serviceaccount.yaml
kubectl apply -f deploy/demo/botamin-voice/postgres-pvc.yaml
kubectl apply -f deploy/demo/botamin-voice/postgres-service.yaml
kubectl apply -f deploy/demo/botamin-voice/postgres-statefulset.yaml
kubectl apply -f deploy/demo/botamin-voice/networkpolicies.yaml
kubectl -n botamin-voice rollout status statefulset/botamin-postgres --timeout=300s
```

Confirm only that external secret objects exist; do not print or decode them:

```bash
kubectl -n botamin-voice get secret botamin-voice-secrets -o name
kubectl -n botamin-voice get secret craas-ai-native-paas-registry -o name
```

Run the one-shot schema gate and wait for success before starting the API:

```bash
kubectl apply -f deploy/demo/botamin-voice/migration-job.template.yaml
kubectl -n botamin-voice wait \
  --for=condition=complete job/botamin-voice-migrate \
  --timeout=600s
```

The Job has a one-hour TTL. For a later release, use a release-unique Job name
or remove only the completed prior Job after its evidence has been retained.
Never run migrations in every application replica.

Apply the application only after migration success:

```bash
kubectl apply -k deploy/demo/botamin-voice
kubectl -n botamin-voice rollout status deployment/botamin-voice --timeout=300s
```

Before enabling the host, persist Caddy ACME state. The legacy Argo application
already ignores the edge Deployment volume drift, so this bounded patch is not
self-healed back to `emptyDir`:

```bash
kubectl apply -f deploy/demo/botamin-voice/edge-caddy-pvc.yaml
data_index="$(
  kubectl -n rec-sidecar get deployment clean-start-edge -o json |
    jq -r '.spec.template.spec.volumes | to_entries[]
      | select(.value.name == "data") | .key'
)"
patch="$(
  jq -cn --argjson index "${data_index}" \
    '[{"op":"replace",
       "path":("/spec/template/spec/volumes/" + ($index | tostring)),
       "value":{"name":"data","persistentVolumeClaim":
         {"claimName":"clean-start-edge-caddy-data"}}}]'
)"
kubectl -n rec-sidecar patch deployment clean-start-edge \
  --type=json -p "${patch}"
kubectl -n rec-sidecar rollout status deployment/clean-start-edge --timeout=300s
```

After the public A record resolves to `85.193.87.39`, apply the reviewed Caddy
host configuration through the existing legacy-edge workflow and restart the
edge so it requests the certificate:

```bash
kubectl apply -f deploy/legacy/edge-caddy-override.yaml
kubectl -n rec-sidecar rollout restart deployment/clean-start-edge
kubectl -n rec-sidecar rollout status deployment/clean-start-edge --timeout=300s
```

Do not create a second Gateway or HTTPRoute for this exception. Caddy must
reach the cross-namespace Service, retain its `/data` volume, and obtain a
certificate whose SAN includes `voicechat.teamgenius.ru`.

## Read-only acceptance

The verification script performs only Kubernetes GETs and public DNS/TLS/HTTP
requests. It never reads Secret data, changes the cluster, creates a booking, or
prints a bearer token:

```bash
scripts/verify-botamin-voice.sh
```

For the authenticated, side-effect-free Vapi tool endpoint check, place the
backend tool bearer token in a local mode-0600 file. The script sends an
unknown-tool probe, which exercises authentication and the wire envelope without
creating a session or booking:

```bash
BOTAMIN_VAPI_TOKEN_FILE=/secure/path/botamin-vapi-tool-token \
BOTAMIN_REQUIRE_VAPI_AUTH=true \
scripts/verify-botamin-voice.sh
```

Useful overrides:

```text
KUBECONFIG
BOTAMIN_HOSTNAME
BOTAMIN_EXPECTED_IPV4
BOTAMIN_EXPECTED_IMAGE
BOTAMIN_NAMESPACE
BOTAMIN_ALLOW_MISSING_MIGRATION_JOB
BOTAMIN_TLS_MIN_VALIDITY_SECONDS
```

Acceptance requires:

- namespace scope `legacy-quarantine` and CI-pool placement;
- immutable image digests and a successful migration Job;
- Bound PostgreSQL PVC and Ready StatefulSet;
- `/health/live` and `/health/ready` returning `200`;
- public HTTP either redirecting to HTTPS or refusing/non-successfully handling
  the application host; the retained Caddy currently disables automatic
  redirects, so the Botamin application must never be served successfully over
  plaintext;
- valid hostname-matching TLS with at least seven days remaining;
- Caddy config routing the hostname to the namespace-local Service;
- unauthenticated Vapi tool/event/TTS requests returning `401`;
- authenticated no-op tool smoke returning the original `toolCallId` and
  logical `UNKNOWN_TOOL`.

## Rollback

Capture the previous application digest, Vapi assistant/tool export, database
backup, and Alembic revision before release. Roll back the application and Vapi
configuration together. Do not rebuild an old image and do not automatically
downgrade the database. Schema changes must remain compatible with the previous
application image; otherwise use the explicit database restore incident
procedure.

After rollback, rerun `scripts/verify-botamin-voice.sh` and one manual incognito
voice happy path.
