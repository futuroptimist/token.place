# Claude Integration

This file summarizes best practices from [Anthropic's "Claude Code Best Practices"](https://www.anthropic.com/engineering/claude-code-best-practices).
It complements [AGENTS.md](AGENTS.md) and [llms.txt](llms.txt) by focusing on guidance specific to the Claude model family.

## Key Points
- Keep prompts concise and provide explicit context.
- Prefer deterministic functions with clear input and output formats.
- Use code comments to explain non-obvious logic.
- Validate model output before acting on it.

For broader assistant behavior, see [docs/AGENTS.md](docs/AGENTS.md).


## token.place API v1 relay guardrails
- Treat API v1 as the only active runtime target for v0.1.0.
- API v1 is non-streaming for relay/client-server paths; do not introduce streaming.
- API v2 is present but incomplete; do not route runtime traffic through it yet.
- Deprecated legacy routes (`/sink`, `/faucet`, `/source`, `/retrieve`, `/next_server`) must not be
  used or extended for active production paths.
- Preserve relay-blind E2EE: relay surfaces may see ciphertext + safe metadata only; plaintext
  model payload content must never appear in relay-owned state/logs/diagnostics/payloads.
- If a path cannot preserve E2EE, fail closed.
- For migration context and required component alignment (`server.py`, `relay.py`, `client.py`,
  desktop Tauri, relay HTML chat UI), see
  [docs/architecture/api_v1_e2ee_relay.md](docs/architecture/api_v1_e2ee_relay.md).
