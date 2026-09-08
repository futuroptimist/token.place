"""Security contract for bounded proxy-aware limiter identity."""

import json
import logging
from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from flask import Flask, request
from jsonschema import Draft7Validator

from api import init_app
from api.client_identity import (
    TrustedProxyConfigurationError,
    parse_trusted_proxy_networks,
)


def _app():
    app = Flask(__name__)
    app.config["TESTING"] = True
    init_app(app)
    return app


@pytest.mark.parametrize(
    ("header", "expected"),
    [("198.51.100.7", "198.51.100.7"), ("2001:0db8::7", "2001:db8::7")],
)
def test_trusted_proxy_accepts_one_authoritative_address(monkeypatch, header, expected):
    monkeypatch.setenv("TOKENPLACE_RATE_LIMIT_TRUSTED_PROXIES", "10.0.0.0/8")
    app = _app()
    with app.test_request_context(
        "/",
        environ_base={"REMOTE_ADDR": "10.1.2.3"},
        headers={"CF-Connecting-IP": header},
    ):
        assert (
            app.extensions["tokenplace_client_identity_policy"].client_address(request)
            == expected
        )


def test_two_clients_behind_proxy_have_independent_quotas(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RATE_LIMIT_TRUSTED_PROXIES", "10.0.0.0/8")
    monkeypatch.setenv("API_RATE_LIMIT", "1/minute")
    monkeypatch.setenv("API_DAILY_QUOTA", "100/day")
    app = _app()
    with app.test_client() as client:

        def get(address):
            return client.get(
                "/api/v1/models",
                environ_base={"REMOTE_ADDR": "10.1.2.3"},
                headers={"CF-Connecting-IP": address},
            )

        assert get("198.51.100.1").status_code == 200
        assert get("198.51.100.1").status_code == 429
        assert get("198.51.100.2").status_code == 200


def test_untrusted_forwarding_is_ignored(monkeypatch):
    monkeypatch.delenv("TOKENPLACE_RATE_LIMIT_TRUSTED_PROXIES", raising=False)
    monkeypatch.setenv("API_RATE_LIMIT", "1/minute")
    app = _app()
    with app.test_client() as client:
        first = client.get(
            "/api/v1/models",
            environ_base={"REMOTE_ADDR": "192.0.2.8"},
            headers={
                "CF-Connecting-IP": "198.51.100.1",
                "Forwarded": "for=203.0.113.9",
                "X-Forwarded-For": "203.0.113.8",
                "X-Real-IP": "203.0.113.7",
            },
        )
        second = client.get(
            "/api/v1/models",
            environ_base={"REMOTE_ADDR": "192.0.2.8"},
            headers={"CF-Connecting-IP": "198.51.100.2"},
        )
    assert first.status_code == 200
    assert second.status_code == 429


@pytest.mark.parametrize(
    "value",
    [
        "",
        " 198.51.100.1",
        "198.51.100.1 ",
        "198.51.100.1:80",
        "198.51.100.1, 203.0.113.1",
        "2001:db8::1%eth0",
        "::ffff:198.51.100.1",
        "garbage",
        "1" * 46,
    ],
)
def test_ambiguous_authoritative_values_coalesce_to_proxy(monkeypatch, value):
    monkeypatch.setenv("TOKENPLACE_RATE_LIMIT_TRUSTED_PROXIES", "10.0.0.1")
    app = _app()
    with app.test_request_context(
        "/",
        environ_base={"REMOTE_ADDR": "10.0.0.1"},
        headers={"CF-Connecting-IP": value},
    ):
        policy = app.extensions["tokenplace_client_identity_policy"]
        assert policy.client_address(request) == "10.0.0.1"


def test_missing_forwarded_identity_and_invalid_direct_peer_coalesce(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RATE_LIMIT_TRUSTED_PROXIES", "10.0.0.1")
    app = _app()
    with app.test_request_context("/", environ_base={"REMOTE_ADDR": "not-an-address"}):
        assert (
            app.extensions["tokenplace_client_identity_policy"].client_address(request)
            == "invalid-direct-peer"
        )


def test_direct_ipv4_mapped_address_has_one_canonical_bucket(monkeypatch):
    monkeypatch.delenv("TOKENPLACE_RATE_LIMIT_TRUSTED_PROXIES", raising=False)
    app = _app()
    policy = app.extensions["tokenplace_client_identity_policy"]
    with app.test_request_context(
        "/", environ_base={"REMOTE_ADDR": "::ffff:192.0.2.4"}
    ):
        mapped_key = policy.limiter_key(request)
    with app.test_request_context("/", environ_base={"REMOTE_ADDR": "192.0.2.4"}):
        assert policy.limiter_key(request) == mapped_key


@pytest.mark.parametrize(
    "value",
    [
        "0.0.0.0/0",
        "::/0",
        "0.0.0.0/8",
        "::/32",
        "10.0.0.1/8",
        " 10.0.0.0/8",
        "10.0.0.0/7",
        "2001:db8::/31",
        "::ffff:192.0.2.1",
        "::ffff:192.0.2.0/120",
        "bad",
    ],
)
def test_invalid_trusted_proxy_configuration_fails_startup(monkeypatch, value):
    monkeypatch.setenv("TOKENPLACE_RATE_LIMIT_TRUSTED_PROXIES", value)
    with pytest.raises(TrustedProxyConfigurationError):
        _app()


@pytest.mark.parametrize(
    ("trusted", "peer"),
    [
        ("10.0.0.0/8,2001:db8::/32", "10.1.2.3"),
        ("2001:db8::/32,10.0.0.0/8", "10.1.2.3"),
        ("10.0.0.0/8,2001:db8::/32", "2001:db8::1"),
        ("2001:db8::/32,10.0.0.0/8", "2001:db8::1"),
    ],
)
def test_dual_stack_allowlist_matches_only_the_peers_ip_version(
    monkeypatch, trusted, peer
):
    monkeypatch.setenv("TOKENPLACE_RATE_LIMIT_TRUSTED_PROXIES", trusted)
    app = _app()
    with app.test_request_context(
        "/",
        environ_base={"REMOTE_ADDR": peer},
        headers={"CF-Connecting-IP": "198.51.100.7"},
    ):
        policy = app.extensions["tokenplace_client_identity_policy"]
        assert policy.client_address(request) == "198.51.100.7"


def test_unique_trusted_identities_do_not_expand_metric_series(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RATE_LIMIT_TRUSTED_PROXIES", "10.0.0.1")
    monkeypatch.setenv("API_RATE_LIMIT", "10000/hour")
    monkeypatch.setenv("API_DAILY_QUOTA", "10000/day")
    registry = CollectorRegistry()
    app = Flask(__name__)
    app.config["TESTING"] = True
    init_app(
        app,
        metrics_registry=registry,
        metrics_export_defaults=False,
        metrics_path=None,
    )

    with app.test_client() as client:
        for index in range(2000):
            response = client.get(
                "/api/v1/models",
                environ_base={"REMOTE_ADDR": "10.0.0.1"},
                headers={"CF-Connecting-IP": f"198.51.{index // 256}.{index % 256}"},
            )
            assert response.status_code == 200

    samples = [
        sample
        for family in registry.collect()
        for sample in family.samples
        if sample.name.startswith("tokenplace_public_http_quota_outcomes_")
    ]
    assert len(samples) == 2
    assert "198.51." not in generate_latest(registry).decode()


def test_identity_sentinels_do_not_reach_public_surfaces(monkeypatch, caplog):
    address = "198.51.100.77"
    monkeypatch.setenv("TOKENPLACE_RATE_LIMIT_TRUSTED_PROXIES", "10.0.0.1")
    monkeypatch.setenv("API_RATE_LIMIT", "1/minute")
    app = _app()
    with caplog.at_level(logging.DEBUG), app.test_client() as client:
        headers = {"CF-Connecting-IP": address, "X-Limiter-Key": "limiter-secret"}
        client.get(
            "/api/v1/models", environ_base={"REMOTE_ADDR": "10.0.0.1"}, headers=headers
        )
        response = client.get(
            "/api/v1/models", environ_base={"REMOTE_ADDR": "10.0.0.1"}, headers=headers
        )
    assert address not in response.get_data(as_text=True)
    assert address not in caplog.text
    assert "limiter-secret" not in caplog.text


def test_gunicorn_access_log_format_omits_identity_and_request_target():
    entrypoint = Path("docker/relay/entrypoint.sh").read_text(encoding="utf-8")
    format_line = next(
        line for line in entrypoint.splitlines() if "--access-logformat" in line
    )
    assert all(
        token not in format_line
        for token in ("%(h)s", "%(L)s", "%(r)s", "%(U)s", "%(q)s")
    )
    assert "%(s)s" in format_line
    assert "%(m)s" in format_line


@pytest.mark.parametrize(
    "value",
    [
        "not-an-ip",
        "999.0.0.1",
        "0.0.0.0/0",
        "224.0.0.1",
        "::/0",
        "ff02::1",
        ":::1",
        "::ffff:192.0.2.1",
        "::ffff:192.0.2.0/120",
    ],
)
def test_chart_schema_rejects_obviously_invalid_trusted_proxy_values(value):
    schema = json.loads(
        Path("charts/tokenplace/values.schema.json").read_text(encoding="utf-8")
    )
    errors = list(
        Draft7Validator(schema).iter_errors({"rateLimit": {"trustedProxies": [value]}})
    )
    assert errors
