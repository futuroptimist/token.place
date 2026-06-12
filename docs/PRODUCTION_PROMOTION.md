# token.place production promotion checklist

Use this checklist for every relay promotion from staging to production. It is intentionally
repeatable and focused on the launch risks that have regressed before: API v1 model identity,
landing-chat routing, relay-blind E2EE privacy, live compute-node diagnostics, production secrets,
and rollback readiness. Historical evidence for the completed v0.1.0 launch lives in
[releases/v0.1.0.md](releases/v0.1.0.md).

## Promotion rules

- Promote only an already-verified staging artifact. Do not rebuild between staging sign-off and
  production unless the new artifact repeats this checklist from the beginning.
- Keep API v1 as the only active runtime target for the current production relay; it is
  non-streaming by design.
- Keep relay-owned state, logs, diagnostics, and payloads ciphertext-only plus safe routing metadata.
  Never capture or paste plaintext prompts, messages, responses, tool arguments, or model output.
- Use synthetic smoke prompts only; do not send sensitive, customer, or private content.
- Record exact artifact identifiers, environment URLs, command output summaries, and rollback steps
  in the release notes or promotion issue.

## Environment context guardrail

Staging and production are separate Sugarkube clusters with separate Cloudflare Tunnel connectors:

- Staging: `sugarkube3-sugarkube5`, Cloudflare Tunnel `sugarkube-staging`, hostname
  `staging.token.place`.
- Production: `sugarkube0-sugarkube2`, Cloudflare Tunnel `sugarkube-prod`, hostname `token.place`.

Do not run production promotion from the staging cluster. Doing so repoints the staging cluster's
single `tokenplace` release to production host values and makes `staging.token.place` return 404,
while `token.place` continues routing to the separate production tunnel and cluster.

Before staging deploys, verify the hostname/node context is `sugarkube3-sugarkube5`. Before
production deploys, verify the hostname/node context is `sugarkube0-sugarkube2`:

```bash
hostname
kubectl get nodes -o wide
kubectl -n cloudflare get deploy,pod -l app.kubernetes.io/name=cloudflare-tunnel
```

## Tag verification

Verify release tags before cutting or announcing a production promotion:

```bash
git fetch --tags origin
git ls-remote --tags origin vX.Y.Z desktop-vX.Y.Z
git rev-parse vX.Y.Z desktop-vX.Y.Z
```

For the v0.1.0 launch, both `v0.1.0` and `desktop-v0.1.0` existed on the upstream GitHub remote and
resolved to the same commit. See [releases/v0.1.0.md](releases/v0.1.0.md) for the exact evidence.
If future relay and desktop tags resolve to different commits, do not retag in place from this
checklist; record the actual result and route it for maintainer review.

## Pre-promotion gates

- [ ] Confirm PR CI checks are green, including the Linux and macOS `run_all_tests.sh` PR checks.
- [ ] Confirm the staging deployment image, chart, and release artifact are exactly the ones intended
      for production promotion; prefer immutable image tags and chart versions, plus a digest where
      available.
- [ ] Confirm desktop releases for Windows and macOS install successfully and register as compute
      nodes against staging.
- [ ] Confirm production secrets and relay registration tokens are set for the production target.
- [ ] Confirm rate-limit storage and production environment settings are configured for production,
      not in-memory or development defaults unless an approved temporary launch exception is recorded.
- [ ] Confirm the rollback path, including the previous known-good image/chart/artifact identifiers,
      command owner, expected recovery time, and any data/state caveats.

## Staging smoke checklist

Run these checks against staging before production promotion:

- [ ] `GET /livez` returns healthy JSON (`status: alive`).
- [ ] `GET /healthz` returns healthy JSON (`status: ok`) and no unexpected degraded details.
- [ ] `GET /relay/diagnostics` reports the live compute-node count accurately, including
      `total_api_v1_registered_compute_nodes`.
- [ ] `GET /api/v1/models` returns exactly one public model: `llama-3.1-8b-instruct`.
- [ ] The landing page model dropdown has exactly one model.
- [ ] The landing UI does not show `owned by token.place`.
- [ ] Two compute nodes round-robin across new browser clients.
- [ ] One browser chat remains sticky to its selected server across multiple turns.
- [ ] Stopping the sticky server causes automatic failover to another available compute node without
      losing chat history.
- [ ] No full public key is rendered in the DOM; only safe key labels/fingerprints may appear.
- [ ] Landing chat makes no `/api/v2` calls.
- [ ] Landing chat makes no `/api/v1/chat/completions` calls; it must use the API v1 relay envelope
      routes instead of the server-side chat-completions selector.
- [ ] Relay logs and diagnostics remain relay-blind: ciphertext only plus safe routing metadata, with
      no plaintext prompts, messages, responses, tool arguments, or model output.

## Production post-promotion smoke checklist

After production deployment, repeat the same external checks against production before announcing the
promotion complete:

- [ ] `GET /livez` returns healthy JSON (`status: alive`).
- [ ] `GET /healthz` returns healthy JSON (`status: ok`) and no unexpected degraded details.
- [ ] `GET /relay/diagnostics` reports the live compute-node count accurately, including
      `total_api_v1_registered_compute_nodes`.
- [ ] `GET /api/v1/models` returns exactly one public model: `llama-3.1-8b-instruct`.
- [ ] The landing page model dropdown has exactly one model.
- [ ] The landing UI does not show `owned by token.place`.
- [ ] Two compute nodes round-robin across new browser clients.
- [ ] One browser chat remains sticky to its selected server across multiple turns.
- [ ] Stopping the sticky server causes automatic failover to another available compute node without
      losing chat history.
- [ ] No full public key is rendered in the DOM.
- [ ] Landing chat makes no `/api/v2` calls.
- [ ] Landing chat makes no `/api/v1/chat/completions` calls.
- [ ] Prod secrets, relay registration tokens, rate-limit storage, and production environment settings
      are still present after rollout.
- [ ] Rollback remains available until the production smoke window is complete.

## Cloudflare Browser Integrity Check skip rules

Do not disable Browser Integrity Check globally. Desktop compute nodes are non-browser API clients,
and Cloudflare can return a pre-app `403` with `error code: 1010` when Browser Integrity Check blocks
relay registration or polling. Instead, keep normal browser security posture elsewhere and add targeted
custom WAF skip rules for API v1 relay paths only.

Staging rule:

- Name: `Skip BIC for staging token.place relay API`
- Expression: `(http.host eq "staging.token.place" and starts_with(http.request.uri.path, "/api/v1/relay/"))`
- Action: `Skip`
- WAF component skipped: `Browser Integrity Check`
- Log matching requests: enabled

Production rule:

- Name: `Skip BIC for prod token.place relay API`
- Expression: `(http.host eq "token.place" and starts_with(http.request.uri.path, "/api/v1/relay/"))`
- Action: `Skip`
- WAF component skipped: `Browser Integrity Check`
- Log matching requests: enabled

Quick validation after the Cloudflare skip is in place:

```bash
curl -i -X POST https://token.place/api/v1/relay/servers/register \
  -H 'Content-Type: application/json' \
  --data '{}'
```

Expected result: not a Cloudflare `403 error code: 1010`. An app-level validation error is acceptable
because `{}` is intentionally invalid.

## Optional JSON endpoint smoke helper

`scripts/promotion_smoke.py` provides a small offline-testable harness for the JSON endpoint portion
of the checklist. It does not run during normal tests and refuses to contact any live target unless it
is explicitly enabled.

Staging example:

```bash
RUN_PROMOTION_SMOKE=1 \
TOKENPLACE_SMOKE_ENV=staging \
TOKENPLACE_SMOKE_BASE_URL=https://staging.token.place \
python scripts/promotion_smoke.py
```

Production requires an additional explicit acknowledgement:

```bash
RUN_PROMOTION_SMOKE=1 \
TOKENPLACE_SMOKE_ENV=production \
TOKENPLACE_SMOKE_ALLOW_PROD=1 \
TOKENPLACE_SMOKE_BASE_URL=https://token.place \
python scripts/promotion_smoke.py
```

The helper checks only safe JSON endpoints:

- `/livez`
- `/healthz`
- `/relay/diagnostics`
- `/api/v1/models`

Browser-only checklist items such as dropdown count, missing owner text, no full public key in the
DOM, no landing-chat `/api/v2` calls, no landing-chat `/api/v1/chat/completions` calls, sticky
routing, two-node round-robin, and automatic history-preserving failover still require the Playwright
or manual browser evidence listed above.

## Post-launch backlog

- Shorten public keys in health, diagnostics, and log output to fingerprints.
- Keep the staging and production Cloudflare BIC skip rules documented and reviewed as relay paths
  evolve.
- Consider making staging/prod release separation harder to misuse with clearer recipes, guardrails,
  or separate release names.
