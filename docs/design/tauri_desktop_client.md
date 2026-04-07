# token.place Tauri desktop client design

## 1) Summary and status

`desktop-tauri/` is the forward-looking desktop path for token.place, but it is still an MVP.
As of April 2026, it is not yet full parity with `server.py` for compute-node responsibilities.

Canonical migration order and phase gates live in:
[docs/roadmap/desktop_compute_node_migration.md](../roadmap/desktop_compute_node_migration.md).

## 2) Current state, near-term, target state

### Current state

- `desktop-tauri/` proves key UX/runtime seams (streaming UI, sidecar boundary, relay config).
- `server.py` remains the reference compute node for production-style behavior.
- `relay.py` supports the legacy sink/source contract and multi-node registration flows.
- API v1 exists for client-facing compatibility, but distributed compute migration is still future.

### Near-term strategy

- Desktop must become a real compute node under the existing legacy relay contract.
- `server.py` and desktop must co-evolve in lockstep through a shared compute-node runtime.
- Relay deployment should move to sugarkube earlier, while compute nodes remain external.

### Target state (post-parity + post-API-v1 migration)

- Desktop is the replacement path for `server.py` compute-node duties.
- Distributed compute contracts are API v1 aligned end-to-end.

## 3) Core requirement: desktop is a compute node, not just a prompt tester

The desktop app is not done when local prompting works. It must satisfy compute-node behavior:

- deterministic request/response handling compatible with legacy relay flow,
- streaming + cancellation semantics compatible with shared runtime fixtures,
- node registration and work execution compatibility in mixed fleets,
- operationally visible model/runtime state for debugging and incident response.

## 4) Lockstep evolution: shared runtime for `server.py` and desktop

To avoid drift, desktop and `server.py` must share a common compute-node contract:

- one runtime interface,
- one compatibility fixture harness,
- two adapters (server adapter, desktop adapter).

No API v1 distributed migration work should bypass this parity gate.

## 5) Model management parity requirements

Desktop model management must expose both model-family context and exact runtime artifact identity.

- Canonical family reference (example baseline):
  [Meta Llama models](https://www.llama.com/models/)
- Runtime artifact identity (example):
  show the exact GGUF file selected by the runtime, e.g.
  `Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf`.
- UX capabilities required for parity:
  - browse model options,
  - download selected artifact,
  - display exact artifact path/hash/metadata used by runtime.

## 6) Relay defaults and connectivity expectations

- Default relay URL should be `https://token.place`.
- The relay URL must remain overridable for development, self-hosting, and staging use.
- Relay integration must remain compatible with current legacy sink/source behavior until the
  post-parity API v1 migration phase.

## 7) Sidecar strategy

The current fake sidecar seam is transitional.

- It is useful for UI and IPC development.
- It is not a long-term substitute for real compute-node execution.
- It must be replaced by real runtime behavior before parity is declared.

## 8) API v1 migration timing

API v1 distributed compute migration is explicitly **post-parity**.

Order constraint:

1. desktop/server parity via shared runtime,
2. multi-node hardening on legacy relay contract,
3. then API v1 distributed migration.

## 9) Operations target: relay on sugarkube

The operational target is to run `relay.py` on sugarkube with environment-specific runbooks,
while compute nodes remain external during parity phases.

See:

- [../relay_sugarkube_onboarding.md](../relay_sugarkube_onboarding.md)
- [../k3s-sugarkube-dev.md](../k3s-sugarkube-dev.md)
- [../k3s-sugarkube-staging.md](../k3s-sugarkube-staging.md)
- [../k3s-sugarkube-prod.md](../k3s-sugarkube-prod.md)

## 10) Explicit non-claims

- This doc does **not** claim desktop already replaces `server.py` today.
- This doc does **not** claim API v1 distributed compute migration is complete today.
