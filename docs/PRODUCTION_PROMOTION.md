# token.place 0.1.0 staging-to-prod promotion checklist

Use this checklist every time a staging relay build is promoted to production. The goal is to make
high-risk launch behavior repeatable without changing the runtime or sending sensitive prompts.
Record links to CI runs, release artifacts, deployment revisions, smoke output, and rollback notes in
the release ticket before promotion.

## Scope and invariants

- `v0.1.0` production promotion is API v1-only for public relay inference paths.
- API v1 relay inference remains non-streaming; do not add a streaming promotion exception.
- Relay-owned diagnostics, logs, queues, and state must remain relay-blind: ciphertext only plus safe routing metadata. Never expose plaintext prompts, tool arguments, messages, or model output.
- The public launch model catalog is intentionally minimal: `/api/v1/models` must return exactly one
  public model, `llama-3.1-8b-instruct`, owned by `Meta`.
- The landing chat uses API v1 relay E2EE routes, remains sticky to one selected compute node while
  it is available, and automatically fails over to another live node without losing visible chat
  history when the selected node becomes unavailable.

## Pre-promotion release gates

- [ ] Confirm the promotion candidate PR has green CI checks, including Linux and macOS
  `run_all_tests.sh` PR checks.
- [ ] Confirm the staging deployment image, Helm chart, and release artifact digests/tags are the
  exact immutable artifacts intended for production.
- [ ] Confirm Windows and macOS desktop release candidates install successfully and register as
  compute nodes against the staging relay.
- [ ] Confirm production secrets are present in the deployment environment.
- [ ] Confirm relay registration tokens are configured for production compute nodes.
- [ ] Confirm rate-limit storage is configured for the production environment.
- [ ] Confirm production environment settings are set, including public base URL, ingress/TLS,
  resource limits, and upstream/relay-only health policy.
- [ ] Confirm the rollback path: previous image/chart versions, data/config compatibility notes,
  operator contact, and the exact command or platform action to revert.

## Staging smoke checklist

Run these checks against the staging host before production promotion. Use only non-sensitive smoke
prompts if a manual browser chat is required.

- [ ] `GET /livez` returns a healthy liveness response.
- [ ] `GET /healthz` returns a healthy readiness response.
- [ ] `GET /relay/diagnostics` reports live compute-node counts accurately:
  `total_registered_compute_nodes` matches `registered_compute_nodes.length`, and
  `total_api_v1_registered_compute_nodes` matches `api_v1_registered_compute_nodes.length`.
- [ ] `GET /api/v1/models` returns exactly one public model: `llama-3.1-8b-instruct`.
- [ ] The landing dropdown has exactly one model option: `llama-3.1-8b-instruct`.
- [ ] The landing UI does not show `owned by token.place` or any equivalent owner line.
- [ ] With two registered compute nodes, two new browser clients round-robin across different
  selected server keys.
- [ ] One browser chat remains sticky to its selected server across multiple turns while that server
  stays available.
- [ ] Stopping the sticky server causes automatic failover to another available compute node without losing visible chat history.
- [ ] No full public key is rendered in the DOM; only shortened/fingerprinted server-key labels are
  acceptable.
- [ ] The landing chat makes no `/api/v2` calls.
- [ ] The landing chat makes no `/api/v1/chat/completions` calls; relay chat dispatch must use the
  API v1 E2EE relay request/retrieve flow.
- [ ] Relay logs and diagnostics do not expose plaintext prompts, messages, responses, tool
  arguments, model output, or full public keys.

## Production post-promotion smoke checklist

Repeat the safe endpoint checks against production after the deployment completes. Browser and
compute-node checks should be repeated only by an authorized operator using non-sensitive prompts.

- [ ] `GET /livez` returns a healthy liveness response.
- [ ] `GET /healthz` returns a healthy readiness response.
- [ ] `GET /relay/diagnostics` live counts match the registered-node arrays.
- [ ] `GET /api/v1/models` returns exactly one public model: `llama-3.1-8b-instruct`.
- [ ] Production desktop compute nodes can register with the configured relay registration token.
- [ ] Landing chat continues to avoid `/api/v2` and `/api/v1/chat/completions` calls.
- [ ] Sticky routing and automatic failover remain history-preserving with two live production
  compute nodes.
- [ ] Confirm the rollback command/action remains viable after the promotion.

## Optional endpoint smoke helper

`scripts/promotion_smoke.py` provides an opt-in JSON endpoint harness for staging or production. It
checks only `/livez`, `/healthz`, `/relay/diagnostics`, and `/api/v1/models`; it does not send chat
prompts and does not require live network access during normal tests.

Staging example:

```bash
RUN_PROMOTION_SMOKE=1 \
TOKENPLACE_SMOKE_BASE_URL=https://staging.token.place \
python scripts/promotion_smoke.py
```

Production is blocked unless explicitly allowed:

```bash
RUN_PROMOTION_SMOKE=1 \
TOKENPLACE_SMOKE_BASE_URL=https://token.place \
TOKENPLACE_SMOKE_ALLOW_PROD=1 \
python scripts/promotion_smoke.py
```

If `RUN_PROMOTION_SMOKE=1` is not set, the script exits without making network requests. Keep that
behavior so routine local and CI test runs remain deterministic and offline.

## Suggested verification before sign-off

```bash
python -m pytest tests/unit/test_docs_* tests/unit/test_testing_documentation.py -v
python -m pytest tests/unit/test_api_v1_launch_contract.py -v
python -m pytest tests/e2e/test_ui.py -k "landing_chat or model or sticky or failover" -v
npm run test:js
./run_all_tests.sh
```
