from pathlib import Path

import pytest


@pytest.mark.unit
def test_k8s_relay_chart_supports_digest_pinning():
    chart_root = Path("k8s/charts/tokenplace-relay")
    helpers_path = chart_root / "templates" / "_helpers.tpl"
    deployment_path = chart_root / "templates" / "deployment.yaml"
    values_path = chart_root / "values.yaml"

    helpers_text = helpers_path.read_text()
    deployment_text = deployment_path.read_text()
    values_text = values_path.read_text()

    assert 'define "tokenplace-relay.image"' in helpers_text
    assert '{{ include "tokenplace-relay.image" . }}' in deployment_text
    assert "digest" in values_text, "values.yaml should expose an image digest override"
