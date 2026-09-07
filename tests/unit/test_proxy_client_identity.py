"""Focused tests for the explicit trusted-proxy limiter identity policy."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask

from api import init_app
from api.client_identity import ClientIdentityPolicy


def _key(app: Flask, peer: str, headers=None) -> str:
    policy = app.extensions["tokenplace_client_identity_policy"]
    with app.test_request_context(
        "/api/v1/models", environ_base={"REMOTE_ADDR": peer}, headers=headers or {}
    ):
        return policy.limiter_key()


@pytest.mark.parametrize(
    ("forwarded", "equivalent"),
    [("203.0.113.7", "203.0.113.7"), ("2001:0db8::7", "2001:db8:0:0::7")],
)
@patch.dict(os.environ, {"TOKENPLACE_TRUSTED_PROXY_NETWORKS": "10.0.0.0/24"}, clear=True)
def test_trusted_proxy_accepts_one_canonical_ip(forwarded, equivalent):
    app = Flask(__name__)
    init_app(app)
    assert _key(app, "10.0.0.5", {"CF-Connecting-IP": forwarded}) == _key(
        app, "10.0.0.5", {"CF-Connecting-IP": equivalent}
    )


@patch.dict(
    os.environ,
    {
        "TOKENPLACE_TRUSTED_PROXY_NETWORKS": "10.0.0.0/24",
        "API_RATE_LIMIT": "1/minute",
        "API_DAILY_QUOTA": "10/day",
    },
    clear=True,
)
def test_two_clients_behind_trusted_proxy_have_independent_quotas():
    app = Flask(__name__)
    init_app(app)
    with app.test_client() as client:
        def request_for(address):
            return client.get(
                "/api/v1/models",
                environ_overrides={"REMOTE_ADDR": "10.0.0.5"},
                headers={"CF-Connecting-IP": address},
            )

        assert request_for("203.0.113.1").status_code == 200
        assert request_for("203.0.113.2").status_code == 200
        assert request_for("203.0.113.1").status_code == 429
        assert request_for("203.0.113.2").status_code == 429


@patch.dict(os.environ, {}, clear=True)
def test_direct_peer_is_used_and_untrusted_forwarding_headers_are_ignored():
    app = Flask(__name__)
    init_app(app)
    direct = _key(app, "198.51.100.9")
    assert direct == _key(
        app,
        "198.51.100.9",
        {
            "CF-Connecting-IP": "203.0.113.9",
            "Forwarded": "for=203.0.113.10",
            "X-Forwarded-For": "203.0.113.11",
            "X-Real-IP": "203.0.113.12",
        },
    )
    assert direct != _key(app, "198.51.100.10", {"CF-Connecting-IP": "203.0.113.9"})


@pytest.mark.parametrize(
    "value",
    [
        "",
        " 203.0.113.1",
        "203.0.113.1 ",
        "203.0.113.1,203.0.113.2",
        "203.0.113.1:443",
        "[2001:db8::1]",
        "::ffff:203.0.113.1",
        "fe80::1%eth0",
        "0.0.0.0",
        "ff02::1",
        "not-an-address",
        "1" * 65,
    ],
)
@patch.dict(os.environ, {"TOKENPLACE_TRUSTED_PROXY_NETWORKS": "10.0.0.0/24"}, clear=True)
def test_unsafe_authoritative_values_coalesce_to_trusted_peer(value):
    app = Flask(__name__)
    init_app(app)
    assert _key(app, "10.0.0.5", {"CF-Connecting-IP": value}) == _key(app, "10.0.0.5")


@patch.dict(os.environ, {"TOKENPLACE_TRUSTED_PROXY_NETWORKS": "10.0.0.0/24"}, clear=True)
def test_non_authoritative_conflicting_headers_cannot_select_identity():
    app = Flask(__name__)
    init_app(app)
    authoritative = {"CF-Connecting-IP": "203.0.113.1"}
    conflicting = {
        **authoritative,
        "Forwarded": "for=203.0.113.2",
        "X-Forwarded-For": "203.0.113.3, 10.0.0.5",
        "X-Real-IP": "203.0.113.4",
    }
    assert _key(app, "10.0.0.5", authoritative) == _key(app, "10.0.0.5", conflicting)


@pytest.mark.parametrize(
    "configuration",
    ["0.0.0.0/0", "::/0", "10.0.0.1/24", "10.0.0.0/24,", "bad", "224.0.0.0/8"],
)
def test_invalid_trusted_proxy_configuration_fails_startup(configuration):
    with patch.dict(
        os.environ, {"TOKENPLACE_TRUSTED_PROXY_NETWORKS": configuration}, clear=True
    ):
        with pytest.raises(ValueError, match="TOKENPLACE_TRUSTED_PROXY_NETWORKS"):
            init_app(Flask(__name__))


def test_too_many_trusted_proxy_entries_fail_startup():
    configuration = ",".join(f"10.0.{index}.0/24" for index in range(33))
    with patch.dict(
        os.environ, {"TOKENPLACE_TRUSTED_PROXY_NETWORKS": configuration}, clear=True
    ):
        with pytest.raises(ValueError, match="TOKENPLACE_TRUSTED_PROXY_NETWORKS"):
            ClientIdentityPolicy.from_environment()


def test_repository_owned_gunicorn_access_log_does_not_emit_peer_addresses():
    entrypoint = Path("docker/relay/entrypoint.sh").read_text(encoding="utf-8")
    assert "--access-logfile /dev/null" in entrypoint
    assert "--access-logfile '-'" not in entrypoint
