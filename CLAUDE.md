
> **API baseline (v0.1.0):** API v1 is the only active runtime target, non-streaming, and E2EE
> relay-blind (ciphertext + routing metadata only). API v2 is incomplete and must not carry active
> runtime traffic yet. Deprecated legacy relay routes (`/sink`, `/faucet`, `/source`, `/retrieve`,
> `/next_server`) must not be used or extended for active production flows.
> See `docs/architecture/api_v1_e2ee_relay.md`.

# Claude Integration

This file summarizes best practices from [Anthropic's "Claude Code Best Practices"](https://www.anthropic.com/engineering/claude-code-best-practices).
It complements [AGENTS.md](AGENTS.md) and [llms.txt](llms.txt) by focusing on guidance specific to the Claude model family.

## Key Points
- Keep prompts concise and provide explicit context.
- Prefer deterministic functions with clear input and output formats.
- Use code comments to explain non-obvious logic.
- Validate model output before acting on it.

For broader assistant behavior, see [docs/AGENTS.md](docs/AGENTS.md).
