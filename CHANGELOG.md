# Changelog

All notable user-facing changes to token.place are tracked here. This file is intentionally lightweight and evergreen; detailed promotion checklists and sign-off evidence live in release issues, PRs, and the production promotion docs.

## Unreleased / v0.1.1

### Planned

- Landing environment/version badge for production, staging, and local relay pages.
- Desktop multi-relay registration and polling from a single operator session.
- Production and staging simultaneous desktop compute-node operation.

## v0.1.0 - Initial production release

- Froze and launched the API v1 production contract: non-streaming runtime traffic, API v1 active paths, and legacy relay routes kept out of production flows.
- Shipped the relay landing chat demo backed by API v1 relay envelope routing.
- Added live compute-node count diagnostics to the landing page.
- Added sticky server routing and history-preserving failover behavior for landing chat sessions.
- Shipped Windows and macOS desktop compute-node releases.
- Completed relay-blind E2EE smoke/signoff for launch: relay-owned state remains ciphertext-only plus safe routing metadata.

Historical release evidence is associated with the `v0.1.0` relay tag and `desktop-v0.1.0` desktop tag.
