"""Bounded, privacy-preserving client identity for application rate limits."""

from __future__ import annotations

import hashlib
import ipaddress
import os
from dataclasses import dataclass

from flask import Request

TRUSTED_PROXIES_ENV = "TOKENPLACE_RATE_LIMIT_TRUSTED_PROXIES"
AUTHORITATIVE_HEADER = "CF-Connecting-IP"
_CONFLICTING_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Real-IP")
_MAX_CONFIG_LENGTH = 4096
_MAX_NETWORKS = 32
_MAX_HEADER_LENGTH = 64


def _canonical_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if (
        not value
        or value != value.strip()
        or len(value) > _MAX_HEADER_LENGTH
        or "%" in value
    ):
        raise ValueError("invalid address shape")
    # ip_address deliberately rejects ports, lists, brackets and zone identifiers.
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        raise ValueError("IPv4-mapped IPv6 addresses are ambiguous")
    return address


def _parse_trusted_networks(
    raw: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    if not raw:
        return ()
    if len(raw) > _MAX_CONFIG_LENGTH:
        raise ValueError(f"{TRUSTED_PROXIES_ENV} is too long")
    parts = raw.split(",")
    if len(parts) > _MAX_NETWORKS or any(
        not part or part != part.strip() for part in parts
    ):
        raise ValueError(
            f"{TRUSTED_PROXIES_ENV} must contain at most {_MAX_NETWORKS} canonical entries"
        )

    networks = []
    for part in parts:
        try:
            network = ipaddress.ip_network(part, strict=True)
        except ValueError as exc:
            raise ValueError(f"invalid {TRUSTED_PROXIES_ENV} entry") from exc
        minimum_prefix = 8 if network.version == 4 else 32
        if (
            network.prefixlen < minimum_prefix
            or network.is_unspecified
            or network.is_multicast
        ):
            raise ValueError(f"dangerously broad {TRUSTED_PROXIES_ENV} entry")
        networks.append(network)
    return tuple(networks)


@dataclass(frozen=True)
class ClientIdentityPolicy:
    """Resolve a request to a non-reversible limiter key.

    Invalid identity data from a trusted immediate peer coalesces to that peer's
    bucket. Data from an untrusted peer is ignored. Neither case rejects traffic
    or permits caller-controlled input to select a key.
    """

    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]

    @classmethod
    def from_environment(cls) -> "ClientIdentityPolicy":
        return cls(_parse_trusted_networks(os.environ.get(TRUSTED_PROXIES_ENV, "")))

    def _peer(self, request: Request) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
        try:
            return _canonical_address(request.remote_addr or "")
        except ValueError:
            # A WSGI server should supply an IP address. Coalesce an invalid/missing
            # peer to one constant key without reflecting its raw value.
            return ipaddress.ip_address("127.0.0.1")

    def key_for_request(self, request: Request) -> str:
        peer = self._peer(request)
        trusted = any(
            peer.version == network.version and peer in network
            for network in self.trusted_networks
        )
        selected = peer
        if trusted:
            authoritative_values = request.headers.getlist(AUTHORITATIVE_HEADER)
            conflicting = any(
                request.headers.getlist(name) for name in _CONFLICTING_HEADERS
            )
            try:
                if len(authoritative_values) != 1 or conflicting:
                    raise ValueError("ambiguous forwarded identity")
                selected = _canonical_address(authoritative_values[0])
            except ValueError:
                selected = peer
        digest = hashlib.sha256(f"client-ip:{selected.compressed}".encode()).hexdigest()
        return f"client:{digest}"
