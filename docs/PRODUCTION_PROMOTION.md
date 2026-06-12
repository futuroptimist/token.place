# token.place production promotion checklist

Use this checklist for every relay promotion from staging to production. It is intentionally
repeatable and focused on risks that have regressed before: API v1 model identity,
landing-chat routing, relay-blind E2EE privacy, live compute-node diagnostics, production secrets,
cluster/tunnel targeting, and rollback readiness. For the historical v0.1.0 launch evidence, see
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

## Environment targeting guardrail

Staging and production are separate Sugarkube clusters and separate Cloudflare Tunnel connectors.
Do not run production promotion from the staging cluster. Doing so repoints the staging cluster's
single `tokenplace` release to production host values and makes `staging.token.place` return 404,
while `token.place` still routes to the separate production tunnel and cluster.

Before staging deploys, verify hostname/node context is `sugarkube3`-`sugarkube5` and the tunnel is
`sugarkube-staging` for `staging.token.place`. Before production deploys, verify hostname/node
context is `sugarkube0`-`sugarkube2` and the tunnel is `sugarkube-prod` for `token.place`.

```bash
hostname
kubectl get nodes -o wide
kubectl -n cloudflare get deploy,pod -l app.kubernetes.io/name=cloudflare-tunnel
```

## Artifact and tag verification

Confirm the staging deployment image, chart, and release artifact are exactly the ones intended for
production promotion; prefer immutable image tags and chart versions, plus a digest where available.
For semver releases, verify Git tags before announcing the release:

```bash
git fetch --tags origin
git ls-remote --tags origin v0.1.0 desktop-v0.1.0
git rev-parse v0.1.0 desktop-v0.1.0
```

If release and desktop tags point to the same commit, record that in the release notes. If they point
to different commits, do not retag from this checklist; record the actual SHAs and ask maintainers to
review.

## Cloudflare Browser Integrity Check skip rules

Do not disable Browser Integrity Check globally. Desktop compute nodes are non-browser API clients,
and Cloudflare can return a pre-app `403` with `error code: 1010` when BIC blocks registration or
polling. Instead, add targeted custom WAF skip rules for relay API paths only, preserving the normal
browser security posture elsewhere.

Staging custom WAF skip rule:

- Name: `Skip BIC for staging token.place relay API`
- Expression: `(http.host eq "staging.token.place" and starts_with(http.request.uri.path, "/api/v1/relay/"))`
- Action: `Skip`
- WAF component skipped: `Browser Integrity Check`
- Log matching requests: enabled

Production custom WAF skip rule:

- Name: `Skip BIC for prod token.place relay API`
- Expression: `(http.host eq "token.place" and starts_with(http.request.uri.path, "/api/v1/relay/"))`
- Action: `Skip`
- WAF component skipped: `Browser Integrity Check`
- Log matching requests: enabled

Quick validation after the production skip is active:

```bash
curl -i -X POST https://token.place/api/v1/relay/servers/register \
  -H 'Content-Type: application/json' \
  --data '{}'
```

Expected result: not a Cloudflare `403 error code: 1010`. An app-level validation error is acceptable
because `{}` is intentionally invalid.

## Pre-promotion gates

- [ ] Confirm PR CI checks are green, including the Linux and macOS `run_all_tests.sh` PR checks.
- [ ] Confirm the immutable image tag, chart reference, chart version, and chart digest where
      available.
- [ ] Confirm Git release tags as described above when promoting a semver release.
- [ ] Confirm desktop releases for Windows and macOS install successfully and register as compute
      nodes against staging.
- [ ] Confirm production secrets and relay registration tokens are set for the production target.
- [ ] Confirm rate-limit storage and production environment settings are configured for production,
      not in-memory or development defaults unless an approved temporary launch exception is recorded.
- [ ] Confirm the Cloudflare BIC skip rule is present for the target relay API path and does not
      apply globally.
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
- Keep Cloudflare BIC skip rules documented for staging and production relay API paths.
- Consider making staging/prod release separation harder to misuse with clearer recipes, guardrails,
  or separate release names.
