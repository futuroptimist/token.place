# token.place 0.1.0 production promotion checklist

Use this checklist for every staging-to-prod promotion so high-risk launch
behavior is verified the same way each time. The checks are intentionally
operator-driven: they combine CI status, release-artifact provenance, desktop
installer smoke tests, relay health checks, landing-chat routing checks, privacy
invariants, and rollback readiness.

## Before promoting

- [ ] Confirm the promotion candidate is the exact commit, image digest, Helm
      chart, and release artifact intended for production.
- [ ] Confirm PR CI checks are green, including the Linux and macOS
      `./run_all_tests.sh` pull-request checks.
- [ ] Confirm desktop releases for Windows and macOS install successfully and
      register as API v1 compute nodes against staging.
- [ ] Confirm production secrets and relay registration tokens are present in the
      production secret store and are not copied from staging logs or chat data.
- [ ] Confirm rate-limit storage is configured for production persistence and
      production environment settings are enabled.
- [ ] Confirm the rollback path, including the previous production image/chart,
      database or storage compatibility notes, and the operator who will execute
      rollback if needed.

## Staging smoke checks

Run the read-only helper against staging when an externally reachable staging
relay is available:

```bash
RUN_PROMOTION_SMOKE=1 TOKENPLACE_SMOKE_BASE_URL=https://staging.example.com \
  python scripts/promotion_smoke.py
```

The helper is safe by default: without `RUN_PROMOTION_SMOKE=1` and an explicit
`TOKENPLACE_SMOKE_BASE_URL` or `--base-url`, it skips instead of contacting live
services. It only performs `GET` requests to JSON endpoints and never sends chat
prompts, API keys, relay registration tokens, or user content.

Manually verify the same candidate in staging:

- [ ] Confirm `/livez` returns a healthy JSON response.
- [ ] Confirm `/healthz` returns a healthy JSON response.
- [ ] Confirm `/relay/diagnostics` reports the live compute-node count
      accurately, including API v1 compute-node counts when present.
- [ ] Confirm `/api/v1/models` returns exactly one public model:
      `llama-3.1-8b-instruct`.
- [ ] Confirm the landing-page model dropdown has exactly one model,
      `llama-3.1-8b-instruct`.
- [ ] Confirm the landing UI does not show `owned by token.place` or equivalent
      model ownership copy.
- [ ] Confirm two compute nodes round-robin across new browser clients.
- [ ] Confirm one browser chat remains sticky to its selected compute server
      across multiple turns.
- [ ] Confirm stopping the sticky server causes automatic failover to another
      live compute node without losing the visible chat history.
- [ ] Confirm no full public key is rendered in the DOM; only short labels or
      safe identifiers may appear.
- [ ] Confirm landing-chat network traffic makes no `/api/v2` calls.
- [ ] Confirm landing-chat network traffic makes no direct
      `/api/v1/chat/completions` calls; the landing chat should use the API v1
      E2EE relay envelope path.
- [ ] Confirm relay-owned state, logs, diagnostics, and payloads remain
      ciphertext-only plus safe routing metadata.

## Production promotion

- [ ] Promote the exact image digest, chart, and release artifact already smoked
      in staging.
- [ ] Confirm production `/livez`, `/healthz`, `/relay/diagnostics`, and
      `/api/v1/models` using the optional helper or equivalent read-only checks:

```bash
RUN_PROMOTION_SMOKE=1 TOKENPLACE_SMOKE_BASE_URL=https://token.place \
  python scripts/promotion_smoke.py
```

- [ ] Confirm desktop Windows and macOS compute nodes can register to production
      using production relay registration tokens.
- [ ] Repeat the landing-chat checks for model identity, absent ownership text,
      sticky routing, automatic failover, no full DOM public key, no `/api/v2`
      calls, and no direct `/api/v1/chat/completions` calls.
- [ ] Record the production image/chart/artifact identifiers, smoke result,
      current live node count, and rollback target in the release notes.

## Rollback trigger examples

Rollback rather than debug in production if any of these launch-contract checks
fail after promotion:

- `/api/v1/models` exposes anything other than the single public
  `llama-3.1-8b-instruct` model.
- Landing chat shows misleading ownership text such as `owned by token.place`.
- Landing chat calls `/api/v2` or direct `/api/v1/chat/completions` paths.
- Relay diagnostics expose plaintext prompt, response, tool, or model-output
  content.
- Sticky-server failover cannot move a chat to another live node without losing
  history.
