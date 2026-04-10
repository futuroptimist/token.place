# token.place repository map

This guide orients contributors to key directories and the docs that define current,
near-term, and target architecture.

## Canonical migration and architecture docs

- [Roadmap: desktop compute-node migration](roadmap/desktop_compute_node_migration.md)
  - Canonical 7-step plan and phase exit criteria.
- [Tauri desktop design](design/tauri_desktop_client.md)
  - Forward-looking desktop strategy; desktop-tauri is currently MVP, not parity.
- [Architecture](ARCHITECTURE.md)
  - Current and target architecture boundaries.
- [Relay on sugarkube onboarding](relay_sugarkube_onboarding.md)
  - Practical relay deployment guidance and links to environment runbooks.

## Applications

- `server.py`
  - Canonical compute-node entrypoint and shared-runtime wrapper.
- `server/server_app.py`
  - Legacy compatibility shim that delegates to `server.py` (non-canonical).
- `server/`
  - Package utilities and compatibility imports.
- `relay.py`
  - Lightweight relay handling legacy sink/source and multi-node registration.
  - First and only short/medium-term deployment candidate for sugarkube.
- `api/`
  - FastAPI implementation and experimental API surface for contributors evaluating API-facing
    runtime paths.
- `desktop-tauri/`
  - Forward-looking desktop client path.
  - Must reach feature parity with `server.py` via shared compute-node runtime before API v1
    distributed migration.
  - Primary workstation targets: Windows 11 (CUDA/NVIDIA) and macOS Apple Silicon (Metal), with
    CPU fallback and later Raspberry Pi support.
- `desktop/`
  - Deprecated Electron prototype retained as historical context.

## Contracts and evolution boundaries

- **Current contract:** legacy relay sink/source flows used by `server.py` and relay nodes.
- **Planned runtime alignment:** shared compute-node runtime co-used by `server.py` and desktop-tauri.
- **Future contract:** API v1-aligned distributed compute after parity and operations readiness.

## Deployment and operations docs

- [k3s sugarkube (dev)](k3s-sugarkube-dev.md)
- [k3s sugarkube (staging)](k3s-sugarkube-staging.md)
- [k3s sugarkube (prod)](k3s-sugarkube-prod.md)
- [relay deploy notes](relay-deploy.md)

Use these together with the roadmap doc when planning implementation prompts 1–7.
