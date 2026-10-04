"""Offline tests for explicit relay credential ownership and transport isolation."""
import io
import json
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock
from urllib import request as urllib_request
from urllib.response import addinfourl

import pytest

from utils.networking.relay_credentials import (
    RelayCredentialError, canonical_relay_url, load_registration_credentials,
    validate_registration_credentials,
)
from utils.networking.relay_client import RelayClient
from utils.networking import http_requests_compat as transport


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch):
    for key in ("TOKEN_PLACE_RELAY_REGISTRATION_CREDENTIALS", "TOKEN_PLACE_RELAY_SERVER_TOKEN",
                "TOKEN_PLACE_RELAY_SERVER_TOKEN_URL"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("url", [
    "http://example.com", "http://0.0.0.0", "http://[::]", "http://127.0.0.1.example.com",
    "https://user@example.com", "https://example.com?", "https://example.com#",
    "https://example.com:0", "https://example.com:65536", "https://example.com:",
    "https://example.com/a/../b", "https://example.com/a/%2e/b", "https://example.com/a//b",
    "https://example.com\\@other.example", "https://example.com\n", "https://example.com./",
    "https://[v1.example.com]/path", "https://[::1]suffix", "https://[::1%25eth0]",
    "https://example.com/a;b", "ftp://example.com", "example.com", "http://127.1",
])
def test_invalid_bindings_are_redacted(url):
    with pytest.raises(RelayCredentialError) as caught:
        validate_registration_credentials({url: "synthetic-admission"})
    assert url not in str(caught.value)
    assert "synthetic-admission" not in str(caught.value)


@pytest.mark.parametrize("url,expected", [
    ("https://EXAMPLE.com:443/team/", "https://example.com/team"),
    ("https://example.com:8443/team", "https://example.com:8443/team"),
    ("http://127.0.0.1:5010", "http://127.0.0.1:5010"),
    ("http://[::1]:5010", "http://[::1]:5010"),
    ("http://localhost:80/", "http://localhost"),
])
def test_canonical_binding(url, expected):
    assert canonical_relay_url(url) == expected


def test_duplicate_bindings_and_invalid_tokens_fail_closed(monkeypatch):
    with pytest.raises(RelayCredentialError):
        validate_registration_credentials({"https://example.com": "one", "https://EXAMPLE.com:443/": "two"})
    monkeypatch.setenv("TOKEN_PLACE_RELAY_REGISTRATION_CREDENTIALS", '{"https://example.com":"one","https://example.com":"two"}')
    with pytest.raises(RelayCredentialError):
        load_registration_credentials()
    for token in [None, "", "a\nb", "a b", 10]:
        with pytest.raises(RelayCredentialError):
            validate_registration_credentials({"https://example.com": token})


def test_legacy_requires_explicit_binding_and_never_follows_primary(monkeypatch):
    monkeypatch.setenv("TOKEN_PLACE_RELAY_SERVER_TOKEN", "synthetic-admission")
    with pytest.raises(RelayCredentialError, match="TOKEN_PLACE_RELAY_SERVER_TOKEN_URL"):
        load_registration_credentials()
    monkeypatch.setenv("TOKEN_PLACE_RELAY_SERVER_TOKEN_URL", "https://a.example")
    assert load_registration_credentials() == {"https://a.example": "synthetic-admission"}


def make_client(monkeypatch, primary, targets, credentials):
    monkeypatch.setattr("utils.networking.relay_client.get_config_lazy", lambda: SimpleNamespace(get=lambda key, default=None: default))
    return RelayClient(primary, None, Mock(public_key_b64="synthetic-public-key"), Mock(),
                       include_configured_servers=False, explicit_relay_urls=targets,
                       registration_credentials=credentials)


def test_actual_registration_fallback_reorder_and_child_ownership(monkeypatch):
    credentials = {"https://a.example": "synthetic-a"}
    for primary, others in [("https://a.example", ["https://b.example"]),
                            ("https://b.example", ["https://a.example"])]:
        client = make_client(monkeypatch, primary, others, credentials)
        seen = []
        def post(url, **kwargs):
            seen.append((url, kwargs.get("headers", {})))
            return Mock(status_code=200, json=lambda: {"control_credential": "synthetic-owner"})
        monkeypatch.setattr("utils.networking.relay_client.requests.post", post)
        for target in client.relay_urls:
            client.register_api_v1_compute_node(target)
        assert dict(seen) == {
            "https://a.example/api/v1/relay/servers/register": {"X-Relay-Server-Token": "synthetic-a"},
            "https://b.example/api/v1/relay/servers/register": {},
        }
        for target in client.relay_urls:
            child = make_client(monkeypatch, target, [], credentials)
            assert child._auth_headers(target) == client._auth_headers(target)
        client._relay_urls.append("https://c.example")
        client._active_relay_index = 2
        assert client._auth_headers(client.relay_url) == {}


def test_binding_does_not_widen_basepath_port_or_host(monkeypatch):
    client = make_client(monkeypatch, "https://a.example/team", [],
                         {"https://A.example:443/team/": "synthetic-a"})
    assert client._auth_headers("https://a.example/team")
    for target in ["https://a.example", "https://a.example/team/sub", "https://a.example/Team",
                   "https://a.example:444/team", "https://b.example/team", "http://a.example/team"]:
        assert client._auth_headers(target) == {}
    for target in ["https://a.example/team?ignored", "https://a.example/team#ignored"]:
        with pytest.raises(RelayCredentialError):
            make_client(monkeypatch, target, [], client._registration_credentials)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("location", ["https://b.example/next", "http://a.example/next", "/next", "https://a.example:8443/next"])
@pytest.mark.parametrize("proof", ["registration", "owner", "explicit"])
def test_urllib_redirects_never_make_followup(monkeypatch, status, location, proof):
    seen = []
    class FakeHTTPS(urllib_request.HTTPSHandler):
        def https_open(self, req):
            seen.append(req.full_url)
            headers = Message()
            headers["Location"] = location
            result = addinfourl(io.BytesIO(b""), headers, req.full_url, status)
            result.msg = "Synthetic redirect"
            return result
    class FakeHTTP(urllib_request.HTTPHandler):
        def http_open(self, req):
            pytest.fail("Unexpected HTTP followup")
    build = urllib_request.build_opener
    monkeypatch.setattr(transport.urllib_request, "build_opener", lambda *handlers: build(*handlers, FakeHTTPS(), FakeHTTP()))
    kwargs = {"json": {}}
    if proof == "registration":
        kwargs["headers"] = {"x-relay-server-token": "synthetic-a"}
    elif proof == "owner":
        kwargs["json"] = {"control_credential": "synthetic-owner"}
    else:
        kwargs["allow_redirects"] = False
    response = transport.requests.post("https://a.example/register", **kwargs)
    assert response.status_code == status
    assert seen == ["https://a.example/register"]


def test_new_config_credentials_never_persist(tmp_path):
    from config import Config
    config = Config(env="testing")
    config.set("relay.registration_credentials", {"https://a.example": "synthetic-a"})
    path = tmp_path / "config.json"
    config.save_user_config(str(path))
    assert json.loads(path.read_text())["relay"]["registration_credentials"] is None


def test_redacted_config_can_reload_without_credentials(monkeypatch):
    config = SimpleNamespace(get=lambda key, default=None: None if key == "relay.registration_credentials" else default)
    assert load_registration_credentials(config) == {}
    monkeypatch.setenv("TOKEN_PLACE_RELAY_REGISTRATION_CREDENTIALS", "null")
    with pytest.raises(RelayCredentialError):
        load_registration_credentials(config)


@pytest.mark.parametrize("proof", ["registration", "owner"])
def test_plaintext_remote_credentials_refused_before_io(monkeypatch, proof):
    monkeypatch.setattr(transport.urllib_request, "build_opener", lambda *_: pytest.fail("Unexpected IO"))
    kwargs = {"headers": {"X-Relay-Server-Token": "synthetic-a"}} if proof == "registration" else {
        "json": {"control_credential": "synthetic-owner"}}
    with pytest.raises(transport.RequestException, match="HTTPS"):
        transport.requests.post("http://remote.example/register", **kwargs)


def test_poll_fallback_and_unregister_keep_admission_and_owner_proofs_separate(monkeypatch):
    a, b = "https://a.example", "https://b.example"
    client = make_client(monkeypatch, a, [b], {a: "synthetic-a", b: "synthetic-b"})
    monkeypatch.setattr(client, "_api_v1_compute_node_capabilities", lambda: {})
    seen = []
    def post(url, **kwargs):
        seen.append((url, kwargs))
        target = a if url.startswith(a + "/") else b
        if url.endswith("/register"):
            return Mock(status_code=200, json=lambda: {
                "control_credential": "owner-a" if target == a else "owner-b",
                "next_ping_in_x_seconds": 30, "poll_wait_seconds": 0,
            })
        if url == a + "/api/v1/relay/servers/poll":
            raise transport.ConnectionError("synthetic network failure")
        return Mock(status_code=200, json=lambda: {"next_ping_in_x_seconds": 30})
    monkeypatch.setattr("utils.networking.relay_client.requests.post", post)
    client.poll_api_v1_encrypted_work()
    assert any(url == b + "/api/v1/relay/servers/poll" for url, _ in seen)
    assert client.unregister_from_relay()
    for url, kwargs in seen:
        is_a = url.startswith(a + "/")
        assert kwargs["headers"] == {"X-Relay-Server-Token": "synthetic-a" if is_a else "synthetic-b"}
        if not url.endswith("/register"):
            assert kwargs["json"]["control_credential"] == ("owner-a" if is_a else "owner-b")


def test_unscoped_token_aborts_multi_relay_before_transport(monkeypatch, caplog):
    monkeypatch.setenv("TOKEN_PLACE_RELAY_SERVER_TOKEN", "synthetic-unscoped")
    monkeypatch.setattr("utils.networking.relay_client.get_config_lazy", lambda: SimpleNamespace(get=lambda key, default=None: default))
    monkeypatch.setattr("utils.networking.relay_client.requests.post", lambda *_a, **_k: pytest.fail("Unexpected IO"))
    with pytest.raises(RelayCredentialError) as caught:
        RelayClient("https://a.example", None, Mock(), Mock(), explicit_relay_urls=["https://b.example"])
    assert "synthetic-unscoped" not in str(caught.value) + caplog.text
    assert "TOKEN_PLACE_RELAY_SERVER_TOKEN_URL" in str(caught.value)


@pytest.mark.parametrize("host", ["a..example", "a.-label.example", "a.label-.example", "a." + "x" * 64 + ".example"])
def test_malformed_dns_labels_cannot_receive_credentials(host, caplog):
    with pytest.raises(RelayCredentialError, match="Invalid relay URL") as caught:
        validate_registration_credentials({f"https://{host}": "synthetic-admission"})
    assert host not in str(caught.value) + caplog.text
    assert "synthetic-admission" not in str(caught.value) + caplog.text


@pytest.mark.parametrize("legacy_token", ["synthetic-mapped", "synthetic-legacy"])
def test_legacy_and_map_collision_is_rejected_even_for_same_token(monkeypatch, caplog, legacy_token):
    monkeypatch.setenv("TOKEN_PLACE_RELAY_REGISTRATION_CREDENTIALS", json.dumps({
        "https://a.example/team": "synthetic-mapped",
    }))
    monkeypatch.setenv("TOKEN_PLACE_RELAY_SERVER_TOKEN", legacy_token)
    monkeypatch.setenv("TOKEN_PLACE_RELAY_SERVER_TOKEN_URL", "https://A.example:443/team/")
    with pytest.raises(RelayCredentialError, match="configure only one") as caught:
        load_registration_credentials()
    for token in ("synthetic-mapped", legacy_token):
        assert token not in str(caught.value) + caplog.text


@pytest.mark.parametrize("expanded,compressed", [
    ("http://[0:0:0:0:0:0:0:1]:5010/team", "http://[::1]:5010/team"),
    ("https://[2001:0DB8:0000:0000:0000:0000:0000:0001]:8443/team", "https://[2001:db8::1]:8443/team"),
    ("https://[::ffff:192.0.2.1]/team", "https://[::ffff:c000:201]/team"),
])
def test_equivalent_ipv6_spellings_match_credentials_and_reject_duplicate_bindings(
    monkeypatch, expanded, compressed,
):
    # Python versions differ in the display form of IPv4-mapped IPv6 literals.
    # Both spellings must resolve to the same key within the runtime.
    assert canonical_relay_url(expanded) == canonical_relay_url(compressed)
    for binding, target in [(expanded, compressed), (compressed, expanded)]:
        client = make_client(monkeypatch, target, [], {binding: "synthetic-ipv6"})
        assert client._auth_headers(target) == {"X-Relay-Server-Token": "synthetic-ipv6"}
        assert client._auth_headers(target + "/other") == {}
    with pytest.raises(RelayCredentialError, match="Duplicate canonical"):
        validate_registration_credentials({expanded: "synthetic-one", compressed: "synthetic-two"})


def test_internal_service_fallback_without_token_does_not_abort_protected_primary(monkeypatch):
    primary = "https://a.example"
    internal = "http://token_place_relay:5010"
    client = make_client(monkeypatch, primary, [internal], {primary: "synthetic-a"})
    seen = []
    def post(url, **kwargs):
        seen.append((url, kwargs.get("headers", {})))
        return Mock(status_code=200, json=lambda: {})
    monkeypatch.setattr("utils.networking.relay_client.requests.post", post)
    for target in client.relay_urls:
        client.register_api_v1_compute_node(target)
    assert seen == [
        (primary + "/api/v1/relay/servers/register", {"X-Relay-Server-Token": "synthetic-a"}),
        (internal + "/api/v1/relay/servers/register", {}),
    ]


def test_https_internal_service_credentials_remain_exactly_bound(monkeypatch):
    target = "https://token_place_relay:8443/team"
    client = make_client(monkeypatch, target, [], {target: "synthetic-internal"})
    assert client._auth_headers(target) == {"X-Relay-Server-Token": "synthetic-internal"}
    assert client._auth_headers("https://token-place-relay:8443/team") == {}
    assert client._auth_headers("https://token_place_relay:8444/team") == {}
    with pytest.raises(RelayCredentialError, match="HTTPS"):
        validate_registration_credentials({"http://token_place_relay:5010": "synthetic-internal"})


def test_surrounding_configuration_whitespace_is_trimmed_but_bindings_remain_strict(monkeypatch):
    target = "https://a.example/team"
    padded = " \n" + target + "\n "
    client = make_client(monkeypatch, padded, [" \nhttp://token_place_relay:5010\n "],
                         {target: "synthetic-a"})
    assert client.relay_urls == (target, "http://token_place_relay:5010")
    assert client._auth_headers(client.relay_urls[0]) == {"X-Relay-Server-Token": "synthetic-a"}
    assert client._auth_headers(client.relay_urls[1]) == {}
    with pytest.raises(RelayCredentialError, match="Invalid relay URL"):
        validate_registration_credentials({padded: "synthetic-a"})
    with pytest.raises(RelayCredentialError, match="Invalid relay URL"):
        make_client(monkeypatch, "https://a.\nexample/team", [], {target: "synthetic-a"})
