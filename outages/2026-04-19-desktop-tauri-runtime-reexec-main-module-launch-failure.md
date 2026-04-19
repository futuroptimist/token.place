# Outage: desktop-tauri operator re-exec could relaunch Python with a directory argv

- **Date:** 2026-04-19
- **Slug:** `desktop-tauri-runtime-reexec-main-module-launch-failure`
- **Affected area:** desktop-tauri operator startup and runtime repair re-exec on Windows

## Summary
After clicking **Start operator**, the desktop app could report `Running: yes` briefly and then
flip back to `Running: no` around 20 seconds later. The bridge process exited before relay
registration completed, leaving `Registered: no`.

## Symptoms
- Operator status toggled to `Running: yes` and then reverted to `Running: no`.
- `Registered` never reached `yes`, so relay registration on localhost did not complete.
- stderr surfaced:
  - `python.exe: can't find '__main__' module in '...\\token.place'`

## Impact
Desktop operators on Windows could not reliably complete startup after runtime repair/reload,
which blocked relay connectivity and local operator inference workflows.

## Root cause
Runtime refresh logic (`maybe_reexec_for_runtime_refresh`) reused `sys.argv` directly for
`os.execve(...)`. In affected desktop launches, `sys.argv[0]` could be a directory path during
re-exec. That produced a Python relaunch targeting a folder instead of the bridge script,
triggering a `__main__` module resolution failure and early operator exit.

## Fix
- Added a re-exec script hint (`TOKEN_PLACE_DESKTOP_REEXEC_SCRIPT`) in desktop bridge/sidecar
  entrypoints.
- Hardened runtime re-exec logic to replace directory-valued `sys.argv[0]` with the script hint
  before calling `os.execve(...)`.
- Added unit regression coverage for the directory-argv re-exec case.
- Strengthened desktop operator UI e2e coverage to keep the operator running/registered past a
  25-second stability window and assert `__main__` launch errors are absent.

## Follow-up / prevention
- Keep runtime re-exec argument construction explicit in sidecar entrypoints.
- Preserve a post-start stability window in desktop operator e2e checks so delayed startup exits
  are caught in CI.
