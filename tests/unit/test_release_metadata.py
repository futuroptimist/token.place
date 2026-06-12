from __future__ import annotations

from pathlib import Path

from release_metadata import get_release_metadata, infer_release_environment, resolve_release_ref, resolve_release_version


SECRET_ENV = {
    "TOKENPLACE_RELEASE_VERSION": "0.1.1",
    "TOKENPLACE_DEPLOY_ENV": "prod",
    "TOKENPLACE_GIT_SHA": "abcdef1234567890abcdef",
    "TOKENPLACE_SECRET_KEY": "super-secret-value",
    "DATABASE_URL": "postgres://user:password@example.invalid/db",
}


def test_release_version_prefers_explicit_env(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELEASE_VERSION", "v9.9.9")

    assert resolve_release_version() == "v9.9.9"


def test_release_version_falls_back_to_chart_app_version(monkeypatch):
    monkeypatch.delenv("TOKENPLACE_RELEASE_VERSION", raising=False)

    assert resolve_release_version() == "0.1.1"


def test_release_environment_host_inference_prod_staging_and_dev(monkeypatch):
    monkeypatch.delenv("TOKENPLACE_DEPLOY_ENV", raising=False)
    monkeypatch.delenv("TOKEN_PLACE_ENV", raising=False)

    assert infer_release_environment("token.place") == "prod"
    assert infer_release_environment("staging.token.place") == "staging"
    assert infer_release_environment("localhost:5010") == "dev"
    assert infer_release_environment("127.0.0.1:5010") == "dev"
    assert infer_release_environment("preview.example.invalid") == "dev"


def test_release_ref_prefers_short_git_sha_over_image_tag():
    metadata = get_release_metadata(
        host="staging.token.place",
        env={
            "TOKENPLACE_RELEASE_VERSION": "0.1.1",
            "TOKENPLACE_IMAGE_TAG": "main-deadbee",
            "TOKENPLACE_GIT_SHA": "abcdef1234567890",
        },
    )

    assert metadata["ref"] == "abcdef123456"
    assert metadata["display_version"] == "abcdef123456"
    assert metadata["badge"] == "staging abcdef123456"


def test_release_metadata_public_safe_and_excludes_secret_env_values():
    metadata = get_release_metadata(host="token.place", env=SECRET_ENV)

    serialized_values = " ".join(metadata.values())
    assert "super-secret-value" not in serialized_values
    assert "password" not in serialized_values
    assert set(metadata) == {"version", "environment", "ref", "display_version", "badge"}
    assert metadata["badge"] == "prod abcdef123456"


def test_release_metadata_endpoint_returns_public_safe_json(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELEASE_VERSION", "0.1.1")
    monkeypatch.delenv("TOKENPLACE_DEPLOY_ENV", raising=False)
    monkeypatch.delenv("TOKEN_PLACE_ENV", raising=False)
    monkeypatch.setenv("TOKENPLACE_GIT_SHA", "deadbeefcafebabefeed")
    monkeypatch.setenv("TOKENPLACE_SECRET_TOKEN", "do-not-leak")

    from relay import app

    with app.test_client() as client:
        response = client.get("/api/v1/meta", headers={"Host": "staging.token.place"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["environment"] == "staging"
    assert payload["version"] == "0.1.1"
    assert payload["ref"] == "deadbeefcafe"
    assert "do-not-leak" not in response.get_data(as_text=True)


def test_helm_chart_sets_public_release_metadata_defaults():
    deployment = Path("charts/tokenplace/templates/deployment.yaml").read_text(encoding="utf-8")
    values = Path("charts/tokenplace/values.yaml").read_text(encoding="utf-8")

    assert "TOKENPLACE_RELEASE_VERSION" in deployment
    assert '"value" .Chart.AppVersion' in deployment
    assert "TOKENPLACE_CHART_VERSION" in deployment
    assert '"value" .Chart.Version' in deployment
    assert "TOKENPLACE_DEPLOY_ENV" in deployment
    assert 'eq .Values.ingress.host "token.place"' in deployment
    assert 'eq .Values.ingress.host "staging.token.place"' in deployment
    assert "TOKENPLACE_IMAGE_TAG" in deployment
    assert "deployEnv:" in values
