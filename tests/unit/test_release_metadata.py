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
    release_metadata._read_chart_app_version.cache_clear()
    release_metadata._read_package_version.cache_clear()


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
        "ref": "main-abcdef123456",
    }


def test_legacy_token_place_env_does_not_override_public_host(monkeypatch):
    _clear_metadata_env(monkeypatch)
    monkeypatch.setenv("TOKEN_PLACE_ENV", "production")

    assert release_metadata.infer_release_environment("staging.token.place") == "staging"


def test_non_deploy_environment_values_map_to_dev(monkeypatch):
    _clear_metadata_env(monkeypatch)
    monkeypatch.setenv("TOKENPLACE_DEPLOY_ENV", "testing")

    assert release_metadata.infer_release_environment("example.com") == "dev"


def test_release_version_file_fallback_is_cached(monkeypatch):
    _clear_metadata_env(monkeypatch)
    chart_reads = {"count": 0}

    def fake_read_chart_app_version() -> str:
        chart_reads["count"] += 1
        return "0.1.1"

    release_metadata._read_chart_app_version.cache_clear()
    monkeypatch.setattr(
        release_metadata,
        "_read_chart_app_version",
        release_metadata.lru_cache(maxsize=1)(fake_read_chart_app_version),
    )

    assert release_metadata.resolve_release_version() == "0.1.1"
    assert release_metadata.resolve_release_version() == "0.1.1"
    assert chart_reads["count"] == 1


def test_release_version_falls_back_to_package_metadata(monkeypatch, tmp_path):
    _clear_metadata_env(monkeypatch)
    chart_dir = tmp_path / "charts" / "tokenplace"
    chart_dir.mkdir(parents=True)
    (chart_dir / "Chart.yaml").write_text("apiVersion: v2\nname: tokenplace\n", encoding="utf-8")
    package_dir = tmp_path / "desktop-tauri"
    package_dir.mkdir()
    (package_dir / "package.json").write_text('{"version": "9.8.7"}', encoding="utf-8")
    monkeypatch.setattr(release_metadata, "_REPO_ROOT", tmp_path)
    release_metadata._read_chart_app_version.cache_clear()
    release_metadata._read_package_version.cache_clear()

    assert release_metadata.resolve_release_version() == "9.8.7"


def test_release_version_uses_dev_when_metadata_files_are_missing(monkeypatch, tmp_path):
    _clear_metadata_env(monkeypatch)
    monkeypatch.setattr(release_metadata, "_REPO_ROOT", tmp_path)
    release_metadata._read_chart_app_version.cache_clear()
    release_metadata._read_package_version.cache_clear()

    assert release_metadata.resolve_release_version() == "dev"


def test_release_version_ignores_invalid_package_json(monkeypatch, tmp_path):
    _clear_metadata_env(monkeypatch)
    chart_dir = tmp_path / "charts" / "tokenplace"
    chart_dir.mkdir(parents=True)
    (chart_dir / "Chart.yaml").write_text("apiVersion: v2\nname: tokenplace\n", encoding="utf-8")
    package_dir = tmp_path / "desktop-tauri"
    package_dir.mkdir()
    (package_dir / "package.json").write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(release_metadata, "_REPO_ROOT", tmp_path)
    release_metadata._read_chart_app_version.cache_clear()
    release_metadata._read_package_version.cache_clear()

    assert release_metadata.resolve_release_version() == "dev"


def test_host_parsing_handles_urls_loopback_and_ipv6(monkeypatch):
    _clear_metadata_env(monkeypatch)

    assert release_metadata.infer_release_environment(None) == "dev"
    assert release_metadata.infer_release_environment("https://token.place/app") == "prod"
    assert release_metadata.infer_release_environment("127.0.0.1:5000") == "dev"
    assert release_metadata.infer_release_environment("[::1]:5000") == "dev"


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


def test_staging_badge_prefers_immutable_image_tag(monkeypatch):
    _clear_metadata_env(monkeypatch)
    monkeypatch.setenv("TOKENPLACE_DEPLOY_ENV", "staging")
    monkeypatch.setenv("TOKENPLACE_RELEASE_VERSION", "0.1.1")
    monkeypatch.setenv("TOKENPLACE_IMAGE_TAG", "main-830d0a4")

    assert release_metadata.get_release_metadata("staging.token.place") == {
        "environment": "staging",
        "version": "main-830d0a4",
        "label": "staging main-830d0a4",
        "ref": "main-830d0a4",
    }


def test_prod_badge_prefers_release_version_and_keeps_ref(monkeypatch):
    _clear_metadata_env(monkeypatch)
    monkeypatch.setenv("TOKENPLACE_DEPLOY_ENV", "prod")
    monkeypatch.setenv("TOKENPLACE_RELEASE_VERSION", "0.1.1")
    monkeypatch.setenv("TOKENPLACE_IMAGE_TAG", "main-830d0a4")

    assert release_metadata.get_release_metadata("token.place") == {
        "environment": "prod",
        "version": "0.1.1",
        "label": "prod 0.1.1",
        "ref": "main-830d0a4",
    }


def test_prod_badge_uses_semver_image_tag_without_release_version(monkeypatch, tmp_path):
    _clear_metadata_env(monkeypatch)
    monkeypatch.setattr(release_metadata, "_REPO_ROOT", tmp_path)
    release_metadata._read_chart_app_version.cache_clear()
    release_metadata._read_package_version.cache_clear()
    monkeypatch.setenv("TOKENPLACE_DEPLOY_ENV", "prod")
    monkeypatch.setenv("TOKENPLACE_IMAGE_TAG", "v0.1.1")

    assert release_metadata.get_release_metadata("token.place") == {
        "environment": "prod",
        "version": "v0.1.1",
        "label": "prod v0.1.1",
        "ref": "v0.1.1",
    }


def test_deployed_like_missing_files_uses_image_tag_instead_of_dev(monkeypatch, tmp_path):
    _clear_metadata_env(monkeypatch)
    monkeypatch.setattr(release_metadata, "_REPO_ROOT", tmp_path)
    release_metadata._read_chart_app_version.cache_clear()
    release_metadata._read_package_version.cache_clear()
    monkeypatch.setenv("TOKENPLACE_DEPLOY_ENV", "staging")
    monkeypatch.setenv("TOKENPLACE_IMAGE_TAG", "main-830d0a4")

    metadata = release_metadata.get_release_metadata("staging.token.place")

    assert metadata["version"] == "main-830d0a4"
    assert metadata["label"] == "staging main-830d0a4"
