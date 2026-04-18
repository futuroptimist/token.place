# Outage: desktop operator relay registration stalled after Start operator

- **Date:** 2026-04-18
- **Slug:** `desktop-operator-relay-registration-stalled`
- **Affected area:** desktop-tauri compute node operator (`Start operator`) and relay connectivity diagnostics

## Summary
Desktop operator sessions could remain stuck at **Registered: no** even when users pointed the app
at a running `relay.py` endpoint, including loopback targets like `http://127.0.0.1:<port>`.

## Symptoms
- `Start operator` transitioned to running, but registration never flipped to `yes`.
- Desktop command-line logs were too sparse to pinpoint where the handshake failed.
- Operators could not quickly determine whether failure came from relay connectivity,
  relay compatibility, or runtime/model initialization.

## Impact
Desktop operators could not reliably register with local or network relays in environments
with global HTTP(S) proxy settings, and troubleshooting required guesswork.

## Root cause
Relay client requests inherited environment proxy settings by default, which can route
loopback relay URLs through a proxy instead of direct localhost transport.

## Remediation
- Force direct (no-proxy) transport for loopback relay targets in `RelayClient`.
- Add detailed bridge stderr diagnostics for critical lifecycle points:
  startup args, runtime/llama setup, relay polling outcomes, registration failures,
  request processing, sleep/cancel, and shutdown.
- Preserve unit coverage for loopback proxy bypass behavior.

## Follow-up / prevention
- Keep explicit transport diagnostics around relay `/sink` and `/source` interactions.
- Keep model/runtime diagnostics visible so llama runtime fallback behavior is obvious.
- Treat loopback registration as a required regression path for desktop operator changes.
