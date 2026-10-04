"""Explicit compute admission credentials, bound to exact relay base URLs."""
from __future__ import annotations

import ipaddress
import json
import os
import re
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit


class RelayCredentialError(ValueError):
    """Configuration error whose message never contains supplied values."""


def canonical_relay_url(value: str, *, require_secure: bool = True) -> str:
    """Canonicalize only equivalent origins; never broaden a base-path binding."""
    invalid = RelayCredentialError("Invalid relay URL; use an absolute URL without userinfo, query, fragment or ambiguous path.")
    if not isinstance(value, str) or not value or re.search(r"[\s\\\x00-\x1f\x7f]", value):
        raise invalid
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if (parsed.scheme not in {"http", "https"} or not host
                or parsed.username is not None or parsed.password is not None
                or "?" in value or "#" in value or parsed.netloc.endswith(":")):
            raise invalid
        if port is not None and not 1 <= port <= 65535:
            raise invalid
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
            if not re.fullmatch(r"[a-zA-Z0-9_](?:[a-zA-Z0-9_.-]*[a-zA-Z0-9_])?", host):
                raise invalid
            if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                   for label in host.split(".")):
                raise invalid
        # urlsplit accepts IPvFuture and some text after a closing bracket.
        # Never reinterpret those authorities as DNS names or IPv6 literals.
        authority_pattern = (r"\[[0-9a-fA-F:.]+\](?::[0-9]+)?"
                             if address is not None and address.version == 6
                             else r"[a-zA-Z0-9_.-]+(?::[0-9]+)?")
        if not re.fullmatch(authority_pattern, parsed.netloc):
            raise invalid
        if require_secure and parsed.scheme != "https":
            if not (host == "localhost" or (address is not None and address.is_loopback)):
                raise RelayCredentialError("Registration credentials require HTTPS; HTTP is allowed only for explicit loopback URLs.")
        path = parsed.path
        if "%" in path or "//" in path or any(part in {".", ".."} for part in path.split("/")):
            raise invalid
        if not re.fullmatch(r"[A-Za-z0-9/_~.\-]*", path):
            raise invalid
        authority = f"[{address.compressed}]" if address is not None and address.version == 6 else host.lower()
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            authority += f":{port}"
        return urlunsplit((parsed.scheme, authority, path.rstrip("/"), "", ""))
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, RelayCredentialError):
            raise
        raise invalid from None


def validate_registration_credentials(values: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(values, dict):
        raise RelayCredentialError("relay.registration_credentials must be a URL-to-token object.")
    result = {}
    for url, token in values.items():
        target = canonical_relay_url(url)
        if target in result:
            raise RelayCredentialError("Duplicate canonical relay credential binding; keep one entry per relay URL.")
        if not isinstance(token, str) or not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise RelayCredentialError("Registration tokens must be nonempty printable ASCII without whitespace.")
        result[target] = token
    return result


def load_registration_credentials(config=None) -> dict[str, str]:
    """Resolve bindings before fan-out. Legacy secrets never acquire an implicit target."""
    def setting(key, env, default=None):
        value = os.environ.get(env)
        return value if value is not None else (config.get(key, default) if config is not None else default)

    values = setting("relay.registration_credentials", "TOKEN_PLACE_RELAY_REGISTRATION_CREDENTIALS", {})
    if values is None and "TOKEN_PLACE_RELAY_REGISTRATION_CREDENTIALS" not in os.environ:
        values = {}  # Saved configuration intentionally redacts this field to null.
    if isinstance(values, str):
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise RelayCredentialError("Duplicate relay credential binding.")
                result[key] = value
            return result
        try:
            values = json.loads(values, object_pairs_hook=unique_object)
        except (ValueError, TypeError):
            raise RelayCredentialError("TOKEN_PLACE_RELAY_REGISTRATION_CREDENTIALS must be a URL-to-token JSON object.") from None
    result = validate_registration_credentials(values)
    legacy = setting("relay.server_registration_token", "TOKEN_PLACE_RELAY_SERVER_TOKEN")
    if legacy:
        target = setting("relay.server_registration_token_url", "TOKEN_PLACE_RELAY_SERVER_TOKEN_URL")
        if not target:
            raise RelayCredentialError("Unscoped registration token refused. Set TOKEN_PLACE_RELAY_SERVER_TOKEN_URL to its trusted relay base URL or migrate to relay.registration_credentials / TOKEN_PLACE_RELAY_REGISTRATION_CREDENTIALS.")
        binding = validate_registration_credentials({target: legacy.strip() if isinstance(legacy, str) else legacy})
        if result.keys() & binding.keys():
            raise RelayCredentialError("Conflicting legacy and mapped relay credential bindings; configure only one.")
        result.update(binding)
    return result
