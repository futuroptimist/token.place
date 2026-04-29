# E2EE relay invariant for coding agents

When editing relay/API v1 distributed paths, preserve this invariant:

- relay path traffic must be relay-blind E2EE,
- no plaintext `messages`/`prompt`/tool content/model text in relay queues,
- no plaintext in relay-targeted network payloads,
- no plaintext in logs, diagnostics, or error echoes.

If a change needs plaintext, keep it out of distributed relay mode or fail closed.
Update sentinel tests in `tests/unit/test_e2ee_relay_invariant.py` with any relay/API changes.
