from __future__ import annotations

from pathlib import Path

CHANGELOG = Path("CHANGELOG.md")
PROMOTION_DOC = Path("docs/PRODUCTION_PROMOTION.md")
TESTING_DOC = Path("docs/TESTING.md")
DESKTOP_README = Path("desktop-tauri/README.md")
PARITY_CONTRACT = Path("docs/architecture/desktop_operator_parity_contract.md")
RELEASE_DOC = Path("docs/releases/v0.1.1.md")


def test_changelog_has_concise_v0_1_0_and_v0_1_1_sections() -> None:
    text = CHANGELOG.read_text(encoding="utf-8")

    assert "## v0.1.1 - Multi-relay desktop and release metadata" in text
    assert "landing-page environment/version badge" in text
    assert "multiple relay URLs" in text
    assert "production and staging relays at the same time" in text
    assert "shared warmed llama.cpp runtime" in text
    assert "Helm chart package version\n  may be `0.1.2`" in text
    assert "## v0.1.0 - Initial production release" in text
    assert "Initial production release" in text


def test_cloudflare_bic_skip_docs_still_cover_staging_and_prod() -> None:
    text = PROMOTION_DOC.read_text(encoding="utf-8")

    assert "Skip BIC for staging token.place relay API" in text
    assert "staging.token.place" in text
    assert "Skip BIC for prod token.place relay API" in text
    assert "token.place" in text
    assert "Browser Integrity Check" in text


def test_promotion_docs_include_multi_relay_and_chart_version_validation() -> None:
    text = PROMOTION_DOC.read_text(encoding="utf-8")

    required = (
        "Multi-relay desktop validation",
        "https://token.place` and `https://staging.token.place",
        "registered `2/2`",
        "one landing chat through production and one landing chat through staging",
        "partial failure does not kill the\n      other relay registration/poll loop",
        "both relay registrations unregister or expire",
        "Chart `appVersion` is the token.place application/release version",
        "Chart `version` is the immutable Helm/OCI deployment package version",
        "chart `version` may be `0.1.2` while\n  `appVersion` remains `0.1.1`",
    )
    missing = [item for item in required if item not in text]
    assert not missing


def test_docs_do_not_say_v0_1_0_is_pending() -> None:
    docs = [CHANGELOG, PROMOTION_DOC, TESTING_DOC, DESKTOP_README, PARITY_CONTRACT]
    offenders = [
        path for path in docs if "v0.1.0 is pending" in path.read_text(encoding="utf-8")
    ]

    assert not offenders


def test_desktop_docs_explain_stopped_only_multi_relay_operation() -> None:
    text = DESKTOP_README.read_text(encoding="utf-8")

    required = (
        "Multi-relay compute-node operation",
        "Stop the operator before editing relay URLs",
        "Relay URL fields are stopped-only",
        "changes apply\n   on the next Start operator action",
        "https://token.place",
        "https://staging.token.place",
        "one model: `llama-3.1-8b-instruct`",
        "registered count such as `2/2`",
        "Partial failures are isolated per relay",
        "unregister from every configured\nrelay",
    )
    missing = [item for item in required if item not in text]
    assert not missing


def test_multi_relay_docs_examples_avoid_full_public_keys() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (DESKTOP_README, PARITY_CONTRACT, PROMOTION_DOC, RELEASE_DOC)
    )

    assert (
        "must\n   not include full public keys" in combined
        or "without exposing full public keys" in combined
    )
    assert "full public keys" in combined
    assert "-----BEGIN PUBLIC KEY-----" not in combined
    assert "client_public_key" not in DESKTOP_README.read_text(encoding="utf-8")


def test_release_specific_evidence_doc_covers_v0_1_1_guardrails() -> None:
    text = RELEASE_DOC.read_text(encoding="utf-8")

    required = (
        "App/desktop release version: `0.1.1`",
        "Helm chart package version: `0.1.2`",
        "Helm chart `appVersion`: `0.1.1`",
        "environment/version badge",
        "single Relay URL field",
        "shared warmed llama.cpp runtime",
        "registered `2/2`",
        "ciphertext only plus safe routing metadata",
    )
    missing = [item for item in required if item not in text]
    assert not missing
