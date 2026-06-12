from __future__ import annotations

import json

import release_metadata


PUBLIC_METADATA_ENV_VARS = (
    "TOKENPLACE_RELEASE_VERSION",
    "TOKENPLACE_DEPLOY_ENV",
    "TOKEN_PLACE_ENV",
    "TOKENPLACE_GIT_SHA",
    "TOKENPLACE_IMAGE_TAG",
)


def _clear_metadata_env(monkeypatch):
    for name in PUBLIC_METADATA_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_prod_host_inference_yields_prod(monkeypatch):
    _clear_metadata_env(monkeypatch)

    assert release_metadata.infer_release_environment("token.place") == "prod"
    assert release_metadata.get_release_metadata("token.place")["environment"] == "prod"


def test_staging_host_inference_yields_staging(monkeypatch):
    _clear_metadata_env(monkeypatch)

    assert release_metadata.infer_release_environment("staging.token.place:443") == "staging"
    assert release_metadata.get_release_metadata("staging.token.place:443")["environment"] == "staging"


def test_release_metadata_prefers_safe_env_values(monkeypatch):
    _clear_metadata_env(monkeypatch)
    monkeypatch.setenv("TOKENPLACE_RELEASE_VERSION", "v0.1.1")
    monkeypatch.setenv("TOKENPLACE_DEPLOY_ENV", "production")
    monkeypatch.setenv("TOKENPLACE_GIT_SHA", "abcdef1234567890SECRET")

    metadata = release_metadata.get_release_metadata("example.com")

    assert metadata == {
        "environment": "prod",
        "version": "v0.1.1",
        "label": "prod v0.1.1",
        "ref": "abcdef123456",
    }


def test_release_metadata_json_does_not_expose_unrelated_secrets(monkeypatch):
    _clear_metadata_env(monkeypatch)
    monkeypatch.setenv("TOKENPLACE_RELEASE_VERSION", "0.1.1")
    monkeypatch.setenv("TOKENPLACE_DEPLOY_ENV", "staging")
    monkeypatch.setenv("TOKENPLACE_OPERATOR_TOKEN", "super-secret-token")
    monkeypatch.setenv("DATABASE_PASSWORD", "super-secret-password")

    payload = release_metadata.release_metadata_json("staging.token.place")
    metadata = json.loads(payload)

    assert metadata["label"] == "staging 0.1.1"
    assert "super-secret-token" not in payload
    assert "super-secret-password" not in payload
    assert set(metadata) <= {"environment", "version", "label", "ref"}
