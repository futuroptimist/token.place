from __future__ import annotations

from pathlib import Path


PROMOTION_DOC = Path("docs/PRODUCTION_PROMOTION.md")
TESTING_DOC = Path("docs/TESTING.md")


def test_production_promotion_checklist_captures_0_1_0_smoke_risks() -> None:
    text = PROMOTION_DOC.read_text(encoding="utf-8")
    required = (
        "Linux `run_all_tests.sh` PR check",
        "Desktop Tauri Release` workflow only when an immutable `desktop-vX.Y.Z` tag is pushed",
        "workflow_dispatch` for a dry run, retry, or rebuild",
        "Operators must not also manually dispatch the\nsame release unless they are intentionally retrying",
        "staging deployment image, chart, and release artifact",
        "desktop releases for Windows and macOS install successfully and register",
        "`GET /livez` returns healthy JSON (`status: alive`)",
        "`GET /healthz` returns healthy JSON (`status: ok`)",
        "`GET /relay/diagnostics` reports the live compute-node count accurately",
        "`GET /api/v1/models` returns exactly one public model: `qwen3-8b-instruct`",
        "landing page model dropdown has exactly one model",
        "landing UI does not show `owned by token.place`",
        "Two compute nodes round-robin across new browser clients",
        "chat remains sticky to its selected server across multiple turns",
        "automatic failover to another available compute node without\n      losing chat history",
        "No full public key is rendered in the DOM",
        "Landing chat makes no `/api/v2` calls",
        "Landing chat makes no `/api/v1/chat/completions` calls",
        "production secrets and relay registration tokens",
        "rate-limit storage and production environment settings",
        "rollback path",
    )
    missing = [item for item in required if item not in text]
    assert not missing


def test_promotion_docs_preserve_relay_blind_and_opt_in_smoke_contract() -> None:
    text = PROMOTION_DOC.read_text(encoding="utf-8")
    required = (
        "relay-owned state, logs, diagnostics, and payloads ciphertext-only",
        "Never capture or paste plaintext prompts",
        "RUN_PROMOTION_SMOKE=1",
        "TOKENPLACE_SMOKE_BASE_URL=https://staging.token.place",
        "TOKENPLACE_SMOKE_ALLOW_PROD=1",
        "python scripts/promotion_smoke.py",
        "The helper checks only safe JSON endpoints",
        "`/api/v1/models`",
        "Browser-only checklist items",
    )
    missing = [item for item in required if item not in text]
    assert not missing


def test_testing_guide_links_promotion_smoke_helper_and_offline_default() -> None:
    text = TESTING_DOC.read_text(encoding="utf-8")
    required = (
        "[PRODUCTION_PROMOTION.md](PRODUCTION_PROMOTION.md)",
        "`qwen3-8b-instruct` as the only public\nmodel",
        "absence of `owned by token.place`",
        "automatic\nhistory-preserving failover",
        "RUN_PROMOTION_SMOKE=1",
        "TOKENPLACE_SMOKE_ALLOW_PROD=1",
        "normal test runs never contact live services",
        "`/livez`, `/healthz`, `/relay/diagnostics`, and `/api/v1/models`",
    )
    missing = [item for item in required if item not in text]
    assert not missing
