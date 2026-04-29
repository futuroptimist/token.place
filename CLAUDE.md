# Claude Integration

This file summarizes best practices from [Anthropic's "Claude Code Best Practices"](https://www.anthropic.com/engineering/claude-code-best-practices).
It complements [AGENTS.md](AGENTS.md) and [llms.txt](llms.txt) by focusing on guidance specific to the Claude model family.

## Key Points
- Keep prompts concise and provide explicit context.
- Prefer deterministic functions with clear input and output formats.
- Use code comments to explain non-obvious logic.
- Validate model output before acting on it.

For broader assistant behavior, see [docs/AGENTS.md](docs/AGENTS.md).


## token.place API v1 architecture constraints (current)
- API v1 is the active API for v0.1.0 and is non-streaming. Return outputs after full generation.
- Do not add streaming behavior to API v1.
- API v2 exists but is incomplete; do not migrate active runtime paths to API v2 yet.
- Deprecated legacy relay endpoints `/sink`, `/faucet`, `/source`, `/retrieve`, `/next_server` are historical only and must not be used in active production paths.
- Required active-path alignment on API v1 E2EE: `server.py`, `relay.py`, `client.py`, desktop Tauri, and relay landing-page HTML chat UI.
- Relay must remain blind to plaintext model payload content; if E2EE cannot be preserved, fail closed.
- Reference: [docs/architecture/api_v1_e2ee_relay.md](docs/architecture/api_v1_e2ee_relay.md).
