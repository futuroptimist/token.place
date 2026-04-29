# Claude Integration

This guide complements `AGENTS.md`, `llms.txt`, and project docs for Claude-family assistants.

## Mandatory architecture context before code edits
- API v1 is the active API for v0.1.0.
- API v1 is non-streaming; responses are returned after complete generation.
- Do not add streaming to API v1.
- API v2 exists but is incomplete; do not move active runtime traffic to API v2 yet.
- `/sink`, `/faucet`, `/source`, `/retrieve`, and `/next_server` are deprecated legacy relay endpoints.
- Do not use, extend, or reintroduce legacy endpoints in active production paths.
- Maintain relay-blind E2EE: relay sees ciphertext and safe routing metadata only; plaintext paths must fail closed.

Known migration context: there is still a gap between `relay.py`, desktop Tauri, and relay HTML chat UI alignment. Code migration is owned by the Prompt 1-4 sequence; docs-only prompts should not implement runtime migrations early.

See `docs/architecture/api_v1_e2ee_relay.md` for full baseline.
