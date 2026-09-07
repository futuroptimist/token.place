"""Trusted-proxy rate-limit identity policy regression tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from flask import Flask

from api import init_app
from api.client_identity import ClientIdentityPolicy


def _policy(networks: str = "10.0.0.0/24") -> ClientIdentityPolicy:
    with patch.dict(
        "os.environ", {"TOKENPLACE_RATE_LIMIT_TRUSTED_PROXIES": networks}, clear=True
    ):
        return ClientIdentityPolicy.from_environment()


def _key(policy: ClientIdentityPolicy, peer: str, headers=None) -> str:
    app = Flask(__name__)
    with app.test_request_context(
        "/protected", environ_base={"REMOTE_ADDR": peer}, headers=headers or {}
    ):
        from flask import request

        return policy.key_for_request(request)


def test_trusted_proxy_accepts_one_ipv4_or_ipv6_authoritative_identity() -> None:
    v4 = _policy()
    assert _key(v4, "10.0.0.7", {"CF-Connecting-IP": "198.51.100.8"}) != _key(
        v4, "10.0.0.7"
    )
    v6 = _policy("2001:db8:1::/64")
    assert _key(v6, "2001:db8:1::7", {"CF-Connecting-IP": "2001:db8:2::8"}) != _key(
        v6, "2001:db8:1::7"
    )


def test_untrusted_headers_are_ignored_and_direct_addresses_are_canonical() -> None:
    policy = _policy()
    plain = _key(policy, "198.51.100.1")
    assert _key(policy, "198.51.100.1", {"CF-Connecting-IP": "203.0.113.9"}) == plain
    assert _key(policy, "2001:db8::1") == _key(policy, "2001:0db8:0:0:0:0:0:1")


@pytest.mark.parametrize(
    "value",
    [
        "",
        " 198.51.100.1",
        "198.51.100.1 ",
        "198.51.100.1,203.0.113.2",
        "198.51.100.1:443",
        "[2001:db8::1]:443",
        "::ffff:198.51.100.1",
        "fe80::1%eth0",
        "not-an-address",
        "1" * 65,
    ],
)
def test_ambiguous_authoritative_values_coalesce_to_trusted_peer(value: str) -> None:
    policy = _policy()
    assert _key(policy, "10.0.0.7", {"CF-Connecting-IP": value}) == _key(
        policy, "10.0.0.7"
    )


@pytest.mark.parametrize("other", ["Forwarded", "X-Forwarded-For", "X-Real-IP"])
def test_conflicting_identity_headers_coalesce_to_trusted_peer(other: str) -> None:
    policy = _policy()
    headers = {"CF-Connecting-IP": "198.51.100.8", other: "198.51.100.8"}
    assert _key(policy, "10.0.0.7", headers) == _key(policy, "10.0.0.7")


def test_mixed_address_family_is_not_membership_confused() -> None:
    policy = _policy()
    assert _key(
        policy, "::ffff:10.0.0.7", {"CF-Connecting-IP": "198.51.100.8"}
    ) == _key(policy, "127.0.0.1")


@pytest.mark.parametrize(
    "value",
    [
        "0.0.0.0/0",
        "::/0",
        "10.0.0.1/24",
        "10.0.0.0/7",
        "ff00::/8",
        "bad",
        "10.0.0.0/24,",
        " 10.0.0.0/24",
    ],
)
def test_invalid_or_dangerously_broad_trusted_proxy_config_fails_startup(
    value: str,
) -> None:
    app = Flask(__name__)
    with patch.dict(
        "os.environ", {"TOKENPLACE_RATE_LIMIT_TRUSTED_PROXIES": value}, clear=True
    ):
        with pytest.raises(ValueError, match="TOKENPLACE_RATE_LIMIT_TRUSTED_PROXIES"):
            init_app(app)


def test_two_clients_behind_proxy_have_independent_hourly_quotas() -> None:
    app = Flask(__name__)
    app.config["TESTING"] = True
    with patch.dict(
        "os.environ",
        {
            "TOKENPLACE_RATE_LIMIT_TRUSTED_PROXIES": "10.0.0.0/24",
            "API_RATE_LIMIT": "1/hour",
            "API_DAILY_QUOTA": "10/day",
        },
        clear=True,
    ):
        init_app(app, metrics_export_defaults=False, metrics_path=None)
    app.add_url_rule("/protected", view_func=lambda: {"ok": True})
    with app.test_client() as client:

        def get(address: str):
            return client.get(
                "/protected",
                environ_base={"REMOTE_ADDR": "10.0.0.7"},
                headers={"CF-Connecting-IP": address},
            )

        assert get("198.51.100.1").status_code == 200
        assert get("198.51.100.2").status_code == 200
        assert get("198.51.100.1").status_code == 429
        assert get("198.51.100.2").status_code == 429


def test_keys_are_fixed_hashes_without_raw_addresses() -> None:
    policy = _policy()
    sentinel = "198.51.100.241"
    key = _key(policy, "10.0.0.7", {"CF-Connecting-IP": sentinel})
    assert key.startswith("client:") and len(key) == 71
    assert sentinel not in key and "10.0.0.7" not in key
    assert all(char in "0123456789abcdef" for char in key.removeprefix("client:"))
