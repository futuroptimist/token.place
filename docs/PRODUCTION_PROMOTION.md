# token.place 0.1.0 staging-to-prod promotion checklist

This checklist is the repeatable smoke gate for promoting the `v0.1.0` relay from staging to production. It is intentionally focused on the launch risks that have caused confusion before: the single public API v1 model, landing-page model text, live compute-node accounting, sticky routing, automatic failover, and relay-blind E2EE/privacy invariants.

Run it once against staging before promotion and repeat the externally observable smoke checks against production after promotion. Record the exact image tag, chart version, release artifact digest, desktop build versions, command output, browser evidence, and rollback decision in the release notes.

## Safety rules

- Use staging by default. Do not target production smoke checks unless the release owner explicitly sets `TOKENPLACE_SMOKE_ENV=prod` and `TOKENPLACE_SMOKE_ALLOW_PROD=1` for the helper script, or otherwise records an equivalent explicit production approval.
- Do not send sensitive prompts, customer data, secrets, private keys, relay registration tokens, or proprietary model payloads during smoke testing.
- Preserve relay-blind E2EE: relay-owned state, logs, diagnostics, and payloads must contain ciphertext only plus safe routing metadata. If a path would expose plaintext prompts, messages, responses, tool arguments, or model output to relay-owned state, fail closed instead of promoting.
- API v1 is the only active runtime target for `v0.1.0`; it is non-streaming. Do not use API v2, legacy relay routes, or API v1 streaming as promotion evidence.

## Pre-promotion release gates

- [ ] PR CI checks are green, including the Linux and macOS `./run_all_tests.sh` PR checks.
- [ ] The staging deployment image, chart, and release artifact are exactly the immutable artifact intended for production, not a mutable `latest`, `staging`, `prod`, or `production` convenience tag.
- [ ] Desktop releases for Windows and macOS install successfully and register as compute nodes against the staging relay.
- [ ] Production secrets, relay registration tokens, and any Cloudflare/TLS/ingress secrets are set in the production environment.
- [ ] Rate-limit storage and production environment settings are configured for the production relay.
- [ ] Rollback path is documented, including the prior approved Helm revision/tag and the commands needed to restore it.

## Staging smoke checklist

- [ ] `GET /livez` returns healthy liveness status.
- [ ] `GET /healthz` returns healthy readiness status and reports the expected registered compute-node count.
- [ ] `GET /relay/diagnostics` reports the live node count accurately, including `total_api_v1_registered_compute_nodes` matching the registered API v1 compute nodes.
- [ ] `GET /api/v1/models` returns exactly one public model: `llama-3.1-8b-instruct`.
- [ ] The landing-page model dropdown has exactly one model.
- [ ] The landing UI does not show `owned by token.place` or any equivalent misleading owner line.
- [ ] Two compute nodes round-robin across new browser clients.
- [ ] One browser chat remains sticky to its selected server across multiple turns.
- [ ] Stopping the sticky server causes automatic failover to another available node without losing chat history.
- [ ] No full public key is rendered in the DOM; only safe fingerprints or shortened identifiers may appear.
- [ ] Landing-chat network traffic makes no `/api/v2` calls.
- [ ] Landing-chat network traffic makes no direct `/api/v1/chat/completions` calls; relay landing chat should use the API v1 E2EE relay path.
- [ ] Browser and relay evidence confirms no plaintext prompts/messages/responses/tool arguments/model output are present in relay-owned logs, diagnostics, queues, or payloads.

## Production post-promotion smoke checklist

Repeat the staging smoke checklist against the production hostname after promotion, using production desktop/compute nodes registered to the production relay. Production sign-off requires fresh production evidence; do not reuse staging screenshots or staging endpoint output.

Also confirm:

- [ ] `GET /livez`, `GET /healthz`, `GET /relay/diagnostics`, and `GET /api/v1/models` pass against production.
- [ ] The production artifact tag/digest and chart version match the staging artifact that was approved for promotion.
- [ ] Production rollback remains available until smoke checks have passed and the release owner signs off.

## Optional endpoint smoke helper

A lightweight JSON endpoint helper lives at `scripts/promotion_smoke.py`. It is offline-safe by default: normal test runs import and unit-test its validation logic, but the helper does not call a live environment unless explicitly enabled.

Example staging run:

```bash
RUN_PROMOTION_SMOKE=1 TOKENPLACE_SMOKE_ENV=staging TOKENPLACE_SMOKE_BASE_URL=https://staging.token.place python scripts/promotion_smoke.py
```

Example production run, requiring explicit production approval:

```bash
RUN_PROMOTION_SMOKE=1 TOKENPLACE_SMOKE_ENV=prod TOKENPLACE_SMOKE_ALLOW_PROD=1 TOKENPLACE_SMOKE_BASE_URL=https://token.place python scripts/promotion_smoke.py
```

The helper checks only JSON endpoints (`/livez`, `/healthz`, `/relay/diagnostics`, and `/api/v1/models`). Browser-only assertions such as dropdown count, owner text absence, sticky routing, automatic failover, DOM public-key redaction, and forbidden landing-chat network calls still require the manual/browser checklist or the Playwright landing-chat E2E tests.

## Suggested verification commands

Run focused checks first, then the full repo check before promotion:

```bash
python -m pytest tests/unit/test_docs_* tests/unit/test_testing_documentation.py -v
python -m pytest tests/unit/test_promotion_smoke.py -v
python -m pytest tests/unit/test_api_v1_launch_contract.py -v
python -m pytest tests/e2e/test_ui.py -k "landing_chat or model or sticky or failover" -v
npm run test:js
./run_all_tests.sh
pre-commit run --all-files
```
