import json

import pytest

import release_metadata
from relay import app


PUBLIC_METADATA_KEYS = {"environment", "version", "display_version", "badge_label", "ref"}


@pytest.fixture(autouse=True)
def clear_release_metadata_env(monkeypatch):
    for name in (
        "TOKENPLACE_RELEASE_VERSION",
        "TOKENPLACE_DEPLOY_ENV",
        "TOKEN_PLACE_ENV",
        "TOKENPLACE_GIT_SHA",
        "TOKENPLACE_IMAGE_TAG",
        "TOKENPLACE_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_release_version_prefers_env_then_chart_app_version(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELEASE_VERSION", "9.9.9")
    assert release_metadata.release_version() == "9.9.9"

    monkeypatch.delenv("TOKENPLACE_RELEASE_VERSION")
    assert release_metadata.release_version() == "0.1.1"


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("token.place", "prod"),
        ("token.place:443", "prod"),
        ("staging.token.place", "staging"),
        ("staging.token.place:443", "staging"),
        ("localhost:5010", "dev"),
        ("127.0.0.1:5010", "dev"),
        ("preview.example.com", "dev"),
    ],
)
def test_deploy_environment_infers_public_hosts(host, expected):
    assert release_metadata.deploy_environment(host) == expected


def test_deploy_environment_prefers_explicit_env(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_DEPLOY_ENV", "staging")
    monkeypatch.setenv("TOKEN_PLACE_ENV", "testing")
    assert release_metadata.deploy_environment("token.place") == "staging"

    monkeypatch.delenv("TOKENPLACE_DEPLOY_ENV")
    assert release_metadata.deploy_environment("token.place") == "testing"


def test_short_release_ref_uses_public_safe_short_git_sha(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_GIT_SHA", "abcdef1234567890SECRET")
    monkeypatch.setenv("TOKENPLACE_IMAGE_TAG", "main-ignored")
    assert release_metadata.short_release_ref() == "abcdef123456"


def test_short_release_ref_falls_back_to_image_tag(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_IMAGE_TAG", "main-abc123")
    assert release_metadata.short_release_ref() == "main-abc123"


def test_release_metadata_is_public_safe_and_omits_secrets(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELEASE_VERSION", "0.1.1")
    monkeypatch.setenv("TOKENPLACE_DEPLOY_ENV", "prod")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    monkeypatch.setenv("TOKENPLACE_API_KEY", "tokenplace-secret-value")

    metadata = release_metadata.release_metadata("token.place")
    serialized = json.dumps(metadata)

    assert set(metadata).issubset(PUBLIC_METADATA_KEYS)
    assert metadata["badge_label"] == "prod 0.1.1"
    assert "secret" not in serialized.lower()
    assert "sk-" not in serialized
    assert "OPENAI" not in serialized
    assert "TOKENPLACE_API_KEY" not in serialized


def test_staging_badge_prefers_ref_when_available(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELEASE_VERSION", "0.1.1")
    monkeypatch.setenv("TOKENPLACE_IMAGE_TAG", "main-abc123")

    metadata = release_metadata.release_metadata("staging.token.place")

    assert metadata["environment"] == "staging"
    assert metadata["version"] == "0.1.1"
    assert metadata["display_version"] == "main-abc123"
    assert metadata["badge_label"] == "staging main-abc123"


def test_api_v1_meta_endpoint_returns_public_safe_metadata(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELEASE_VERSION", "0.1.1")
    monkeypatch.setenv("TOKENPLACE_IMAGE_TAG", "main-abc123")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")

    with app.test_client() as client:
        response = client.get("/api/v1/meta", headers={"Host": "staging.token.place"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["environment"] == "staging"
    assert payload["version"] == "0.1.1"
    assert payload["display_version"] == "main-abc123"
    assert set(payload).issubset(PUBLIC_METADATA_KEYS)
    assert "secret" not in response.get_data(as_text=True).lower()


def test_rendered_index_injects_prod_badge_metadata(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELEASE_VERSION", "0.1.1")

    with app.test_client() as client:
        response = client.get("/", headers={"Host": "token.place"})

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'data-testid="release-badge"' in html
    assert '"badge_label":"prod 0.1.1"' in html
    assert "__TOKENPLACE_VUE_SCRIPT_SRC__" not in html


def test_chart_defaults_wire_release_metadata_env():
    chart = (release_metadata.REPO_ROOT / "charts" / "tokenplace" / "Chart.yaml").read_text(encoding="utf-8")
    deployment = (
        release_metadata.REPO_ROOT / "charts" / "tokenplace" / "templates" / "deployment.yaml"
    ).read_text(encoding="utf-8")

    assert 'appVersion: "0.1.1"' in chart
    assert '"TOKENPLACE_RELEASE_VERSION"' in deployment
    assert '.Chart.AppVersion' in deployment
    assert '"TOKENPLACE_CHART_VERSION"' in deployment
    assert '.Chart.Version' in deployment
    assert '"TOKENPLACE_DEPLOY_ENV"' in deployment
    assert 'eq .Values.ingress.host "token.place"' in deployment
    assert 'eq .Values.ingress.host "staging.token.place"' in deployment
