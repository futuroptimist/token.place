from __future__ import annotations

import json

import release_metadata
import relay


SECRET_ENV = {
    "TOKENPLACE_RELEASE_VERSION": "0.1.1",
    "TOKENPLACE_DEPLOY_ENV": "prod",
    "TOKENPLACE_GIT_SHA": "abcdef1234567890abcdef",
    "TOKEN_PLACE_RELAY_SERVER_TOKEN": "super-secret-registration-token",
    "OPENAI_API_KEY": "sk-secret-token",
}


def test_release_metadata_prefers_public_env_values():
    metadata = release_metadata.build_release_metadata(
        host="localhost:5010",
        environ=SECRET_ENV,
    )

    assert metadata == {
        "environment": "prod",
        "version": "0.1.1",
        "display_version": "v0.1.1",
        "label": "prod v0.1.1",
        "ref": "abcdef123456",
    }


def test_release_metadata_infers_prod_and_staging_hosts(monkeypatch):
    monkeypatch.delenv("TOKENPLACE_RELEASE_VERSION", raising=False)
    monkeypatch.delenv("TOKENPLACE_DEPLOY_ENV", raising=False)
    monkeypatch.delenv("TOKEN_PLACE_ENV", raising=False)
    monkeypatch.delenv("TOKENPLACE_GIT_SHA", raising=False)
    monkeypatch.delenv("TOKENPLACE_IMAGE_TAG", raising=False)

    prod_metadata = release_metadata.build_release_metadata(host="token.place")
    staging_metadata = release_metadata.build_release_metadata(host="staging.token.place")

    assert prod_metadata["environment"] == "prod"
    assert prod_metadata["label"] == "prod v0.1.1"
    assert staging_metadata["environment"] == "staging"
    assert staging_metadata["label"] == "staging v0.1.1"


def test_release_metadata_uses_image_tag_when_version_is_dev():
    metadata = release_metadata.build_release_metadata(
        host="unknown.example",
        environ={"TOKENPLACE_RELEASE_VERSION": "dev", "TOKENPLACE_IMAGE_TAG": "main-abc123"},
    )

    assert metadata["environment"] == "dev"
    assert metadata["version"] == "dev"
    assert metadata["ref"] == "main-abc123"
    assert metadata["label"] == "dev main-abc123"


def test_release_metadata_does_not_expose_secret_env_values():
    metadata = release_metadata.build_release_metadata(host="token.place", environ=SECRET_ENV)
    serialized = json.dumps(metadata, sort_keys=True)

    assert "super-secret" not in serialized
    assert "sk-secret" not in serialized
    assert set(metadata) <= {"environment", "version", "display_version", "label", "ref"}


def test_rendered_landing_page_injects_release_metadata(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELEASE_VERSION", "0.1.1")
    monkeypatch.delenv("TOKENPLACE_DEPLOY_ENV", raising=False)
    monkeypatch.delenv("TOKEN_PLACE_ENV", raising=False)
    monkeypatch.setenv("TOKENPLACE_GIT_SHA", "abcdef1234567890")

    html = relay._render_index_html(host="token.place")

    assert relay.RELEASE_META_PLACEHOLDER not in html
    assert 'data-testid="release-badge"' in html
    assert '"environment": "prod"' in html
    assert '"version": "0.1.1"' in html
    assert "abcdef123456" in html
    assert "TOKENPLACE_GIT_SHA" not in html


def test_meta_endpoint_returns_public_safe_metadata(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELEASE_VERSION", "0.1.1")
    monkeypatch.delenv("TOKENPLACE_DEPLOY_ENV", raising=False)
    monkeypatch.delenv("TOKEN_PLACE_ENV", raising=False)
    monkeypatch.setenv("TOKENPLACE_IMAGE_TAG", "main-abc123")

    with relay.app.test_client() as client:
        response = client.get("/api/v1/meta", headers={"Host": "staging.token.place"})

    assert response.status_code == 200
    assert response.get_json() == {
        "environment": "staging",
        "version": "0.1.1",
        "display_version": "v0.1.1",
        "label": "staging v0.1.1",
        "ref": "main-abc123",
    }
