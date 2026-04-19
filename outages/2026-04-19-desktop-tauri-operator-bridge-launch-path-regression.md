# Outage: desktop-tauri operator bridge launch path regression

- **Date:** 2026-04-19
- **Slug:** `desktop-tauri-operator-bridge-launch-path-regression`
- **Affected area:** desktop-tauri `Start operator` launch path resolution on Windows

## Summary
On some Windows desktop installs, clicking **Start operator** briefly showed `Running: yes` and
then returned to `Running: no` with stderr reporting:

`python.exe: can't find '__main__' module in '...\\AppData\\Local\\token.place'`

`Registered` never switched to `yes`, so the operator did not connect to the local relay.

## Symptoms
- `Running` flipped to `yes`, then back to `no` after startup polling.
- `Registered` stayed `no`.
- stderr included `can't find '__main__' module in '...token.place'`.
- Relay round-trip was never established from desktop operator mode.

## Root cause
Desktop compute-node bridge script candidate resolution was too narrow for packaged/runtime launch
contexts and could miss the bridge script in current working directory based layouts. In those
contexts the bridge spawn path could degrade into an invalid Python invocation that treated the
app data directory as an entry module target.

## Remediation
- Expanded compute-node bridge script candidate discovery to include working-directory packaged
  layouts (`./resources/python`, `./python`, and `./compute_node_bridge.py`) before fallback.
- Added a Rust unit regression test that asserts current-working-directory candidates are included.
- Strengthened desktop operator UI e2e to require `Running: yes` and `Registered: yes` to remain
  stable for 25 seconds after startup, catching delayed startup regressions.

## Follow-up / prevention
- Keep desktop launcher path discovery aligned between packaged and dev layouts.
- Preserve UI-level operator stability assertions (not just initial transition to `yes`).
- Continue validating relay registration and inference round-trip in the same e2e flow.
