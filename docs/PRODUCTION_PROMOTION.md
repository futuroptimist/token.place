# token.place staging-to-production promotion checklist

Use this checklist for every relay promotion from staging to production. It is intentionally
repeatable and focused on the release risks that have regressed before: API v1 model identity,
landing-chat routing, relay-blind E2EE privacy, live compute-node diagnostics, production secrets,
Cloudflare routing, and rollback readiness. Keep version-specific evidence in release notes or
`docs/releases/` so future promotions can reuse this checklist by replacing the artifact tag, chart
version, digest, and smoke-test evidence.

## Promotion rules

- Promote only an already-verified staging artifact. Do not rebuild between staging sign-off and
  production unless the new artifact repeats this checklist from the beginning.
- Keep API v1 as the only active runtime target for the current 0.1.x release line; it is non-streaming by design.
- Keep relay-owned state, logs, diagnostics, and payloads ciphertext-only plus safe routing metadata.
  Never capture or paste plaintext prompts, messages, responses, tool arguments, or model output.
- Use synthetic smoke prompts only; do not send sensitive, customer, or private content.
- Record exact artifact identifiers, environment URLs, command output summaries, Cloudflare rule
  evidence, desktop multi-relay results, and rollback steps in the release notes or promotion issue.

## Environment guardrails

Staging and production run on separate Sugarkube clusters and Cloudflare Tunnel connectors:

- Staging: `sugarkube3-sugarkube5`, Cloudflare tunnel `sugarkube-staging`, hostname
  `staging.token.place`.
- Production: `sugarkube0-sugarkube2`, Cloudflare tunnel `sugarkube-prod`, hostname `token.place`.

Do not run production promotion from the staging cluster. That repoints the staging cluster's single
`tokenplace` Helm release to production host values, causing `staging.token.place` to 404 while
`token.place` still routes to the separate production tunnel and cluster.

Before staging deploys, verify the shell and Kubernetes context are on `sugarkube3-sugarkube5`.
Before production deploys, verify they are on `sugarkube0-sugarkube2`:

```bash
hostname
kubectl get nodes -o wide
kubectl -n cloudflare get deploy,pod -l app.kubernetes.io/name=cloudflare-tunnel
```


## Version and chart metadata

For every release, record both application version metadata and deploy-package metadata. These values
may intentionally differ:

- Chart `appVersion` is the token.place application/release version shown by public release metadata
  endpoints and the landing-page badge. For v0.1.1 this is `0.1.1`.
- Chart `version` is the immutable Helm/OCI deployment package version. If chart source changes after
  an app release version is chosen, the chart package version must be bumped instead of overwriting an
  existing OCI chart package. For v0.1.1 deployments, chart `version` may be `0.1.3` while
  `appVersion` remains `0.1.1`.
- Do not bump the app or desktop version just to satisfy Helm chart immutability; bump only the chart
  package version when chart files changed and CI requires a new package.

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
- [ ] Confirm targeted Cloudflare Browser Integrity Check skip rules exist for the relay API paths in
      both staging and production; do not disable Browser Integrity Check globally.
- [ ] Confirm the rollback path, including the previous known-good image/chart/artifact identifiers,
      command owner, expected recovery time, and any data/state caveats.

## Cloudflare Browser Integrity Check skip rules

Desktop compute nodes are non-browser API clients. Cloudflare can return a pre-app `403` with
`error code: 1010` when Browser Integrity Check blocks registration or polling before the request
reaches the relay. Keep Browser Integrity Check enabled globally and add targeted custom WAF skip
rules only for API v1 relay paths.

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

This keeps the skip restricted to `/api/v1/relay/` while preserving the normal browser security
posture for the landing page and all other paths.

Quick validation for both environments:

Validate both `https://staging.token.place/api/v1/relay/servers/register` and
`https://token.place/api/v1/relay/servers/register`:

```bash
for HOST in staging.token.place token.place; do
  curl -i -X POST "https://${HOST}/api/v1/relay/servers/register" \
    -H 'Content-Type: application/json' \
    --data '{}'
done
```

Expected result after each Cloudflare skip: the response is not a Cloudflare `403 error code: 1010`.
Any relay-owned app response is acceptable, including `401 Missing or invalid relay server token`
when `SERVER_REGISTRATION_TOKENS` are configured or a `400` response because the `{}` payload is
intentionally invalid.

## Tag verification

For releases with Git tags, maintainers should verify the remote tags and local checked-out tags
before promotion:

```bash
RELEASE_TAG=vX.Y.Z
DESKTOP_TAG=desktop-${RELEASE_TAG}
git fetch --tags origin
git ls-remote --tags origin \
  "refs/tags/${RELEASE_TAG}" "refs/tags/${RELEASE_TAG}^{}" \
  "refs/tags/${DESKTOP_TAG}" "refs/tags/${DESKTOP_TAG}^{}"
git rev-parse "${RELEASE_TAG}^{commit}" "${DESKTOP_TAG}^{commit}"
```

The remote check intentionally requests both the normal tag refs and any peeled `^{}` refs. Lightweight
tags normally return only the normal `refs/tags/...` lines, so missing `^{}` lines are not a failure;
annotated tags should also show peeled `^{}` lines that identify their target commits.

Compare the local `^{commit}` results when checking release/desktop commit equality, and use the
remote output as supporting evidence that the requested tags exist and annotated tags peel to the
same commit targets instead of tag object IDs. If the relay and desktop tags point to different
commits, do not rewrite tags in the promotion process. Record the actual commits in the release
notes and ask maintainers to review the mismatch. If a release has no desktop artifact or desktop
tag, record that fact and skip the desktop-tag comparison instead of failing the whole checklist.

## Staging smoke checklist

Run these checks against staging before production promotion:

- [ ] `GET /livez` returns healthy JSON (`status: alive`).
- [ ] `GET /healthz` returns healthy JSON (`status: ok`) and no unexpected degraded details.
- [ ] `GET /relay/diagnostics` reports the live compute-node count accurately, including
      `total_api_v1_registered_compute_nodes`.
- [ ] `GET /api/v1/models` returns exactly one public model: `qwen3-8b-instruct`.
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
- [ ] `GET /api/v1/models` returns exactly one public model: `qwen3-8b-instruct`.
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


## Multi-relay desktop validation

For v0.1.x, one desktop compute node can serve both production and staging because API v1 exposes a
single model, `qwen3-8b-instruct`, and the desktop runtime reuses one warmed llama.cpp runtime
while polling each configured relay. Validate simultaneous prod+staging registration before closing
the release:

- [ ] Build and install desktop on Windows and macOS release targets.
- [ ] Configure exactly these relay URLs in the desktop Compute node operator panel:
      `https://token.place` and `https://staging.token.place`.
- [ ] Confirm relay URL fields are stopped-only: start the operator, verify editing is disabled, and
      stop the operator before making changes that apply on the next start.
- [ ] Start one desktop operator and confirm the desktop status shows registered `2/2` (or an
      equivalent registered count where both configured relays are healthy).
- [ ] Confirm `https://token.place/relay/diagnostics` reports the node by incrementing
      `total_api_v1_registered_compute_nodes`.
- [ ] Confirm `https://staging.token.place/relay/diagnostics` reports the same operator by
      incrementing `total_api_v1_registered_compute_nodes`.
- [ ] Send one landing chat through production and one landing chat through staging.
- [ ] If practical, stop one relay or block one route and confirm partial failure does not kill the
      other relay registration/poll loop; the healthy relay should continue serving work and status
      should show the remaining registered count.
- [ ] Stop the operator and verify both relay registrations unregister or expire from diagnostics.
- [ ] Confirm relay logs remain relay-blind: ciphertext only plus safe routing metadata, with no
      plaintext prompts, messages, responses, tool arguments, model output, private keys, or full
      public keys.

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

If `TOKENPLACE_SMOKE_EXPECT_VERSION` or `TOKENPLACE_SMOKE_EXPECT_ENV` is set, the helper also checks
`/api/v1/version` for public-safe release metadata without making those expectations mandatory in
normal runs. Example:

```bash
RUN_PROMOTION_SMOKE=1 \
TOKENPLACE_SMOKE_ENV=production \
TOKENPLACE_SMOKE_ALLOW_PROD=1 \
TOKENPLACE_SMOKE_BASE_URL=https://token.place \
TOKENPLACE_SMOKE_EXPECT_VERSION=0.1.1 \
TOKENPLACE_SMOKE_EXPECT_ENV=prod \
python scripts/promotion_smoke.py
```

Browser-only checklist items such as dropdown count, missing owner text, no full public key in the
DOM, no landing-chat `/api/v2` calls, no landing-chat `/api/v1/chat/completions` calls, sticky
routing, two-node round-robin, and automatic history-preserving failover still require the Playwright
or manual browser evidence listed above.
