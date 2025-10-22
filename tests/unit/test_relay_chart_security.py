"""Ensure the Helm chart enforces the documented relay security defaults."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


CHART_ROOT = Path("deploy/charts/tokenplace-relay")
VALUES_PATH = CHART_ROOT / "values.yaml"
SCHEMA_PATH = CHART_ROOT / "values.schema.json"


def test_chart_security_defaults_lock_down_runtime():
    """The default chart values should harden the relay container."""

    values = yaml.safe_load(VALUES_PATH.read_text(encoding="utf-8"))

    pod_security = values["podSecurityContext"]
    assert pod_security["runAsNonRoot"] is True
    assert pod_security["runAsUser"] == 1000
    assert pod_security["runAsGroup"] == 1000
    assert pod_security["fsGroup"] == 1000
    assert pod_security["seccompProfile"]["type"] == "RuntimeDefault"

    container_security = values["securityContext"]
    assert container_security["runAsNonRoot"] is True
    assert container_security["runAsUser"] == 1000
    assert container_security["allowPrivilegeEscalation"] is False
    assert container_security["readOnlyRootFilesystem"] is True
    assert container_security["capabilities"]["drop"] == ["ALL"]

    network_policy = values["networkPolicy"]
    assert network_policy["enabled"] is True
    assert network_policy["allowDNS"] is True
    assert network_policy["extraIngress"] == []
    assert network_policy["extraEgress"] == []
    # The default egress CIDR must not allow the pod to talk to the entire internet.
    assert network_policy["externalNameCIDR"] != "0.0.0.0/0"


def test_schema_requires_security_hardening_fields():
    """The values schema should prevent omitting critical hardening knobs."""

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    properties = schema["properties"]

    pod_schema = properties["podSecurityContext"]
    assert set(pod_schema["required"]) == {
        "runAsNonRoot",
        "runAsUser",
        "runAsGroup",
        "fsGroup",
        "seccompProfile",
    }

    container_schema = properties["securityContext"]
    assert container_schema["properties"]["allowPrivilegeEscalation"]["const"] is False
    assert container_schema["properties"]["readOnlyRootFilesystem"]["const"] is True
    assert container_schema["properties"]["runAsNonRoot"]["const"] is True
    assert container_schema["properties"]["runAsUser"]["const"] == 1000
    assert container_schema["properties"]["capabilities"]["properties"]["drop"]["items"]["const"] == "ALL"
