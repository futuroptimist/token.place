# 2026-05-24: Desktop macOS operator startup regression and e2e coverage gap

## Summary
Two desktop-tauri regressions impacted macOS users: the model path field could be auto-populated
from runtime-resolved defaults on first launch, and operator startup failures could appear as a
brief `Running: yes` transition before returning to `Running: no` without a clearly surfaced UI
error context.

## User impact
- First launch could show a non-user-selected GGUF path, including environment-specific absolute
  paths.
- Clicking **Start operator** could fail quickly, leaving users with unclear diagnostics.

## Symptoms
- `Model GGUF path` displayed a machine-specific path before any user browse/download action.
- `Start operator` briefly showed running, then stopped, with no durable/obvious error signal in
  the top-level UI status.

## Root causes
1. The renderer bootstrap path auto-saved `inspect_model_artifact().resolved_model_path` into
   config when `load_config()` returned an empty model path.
2. The UI and backend startup flow both treated startup as immediately running before bridge
   startup health events were emitted, creating transient green status before immediate exit.

## Why existing e2e coverage missed this
- The desktop e2e test immediately filled `Model GGUF path`, so it did not assert first-launch
  blank-path behavior.
- The test asserted `Running: yes` and `Registered: yes` but did not verify stable running state
  after a short post-start interval.

## Fix implemented
- Removed renderer auto-population/saving of model path when config is empty.
- Start flow no longer sets running optimistically before bridge startup events.
- Compute-node event errors now also surface in the visible UI error banner.
- e2e now asserts initial blank model path and validates running stays `yes` after startup.

## Tests updated
- `desktop-tauri/src/App.test.tsx`
  - added first-launch blank model path assertion
  - added persisted model path restoration assertion
- `desktop-tauri/scripts/test_desktop_operator_ui_e2e.py`
  - added first-launch blank model path assertion
  - added stable post-start running assertion window

## Follow-ups
- Keep macOS desktop e2e on CI runners with tauri-driver support; if runner availability changes,
  preserve the stable-running assertions in the strongest available desktop integration job.
