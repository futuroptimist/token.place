"""Bounded, privacy-preserving client identity for request quotas."""

from __future__ import annotations

import hashlib
import ipaddress
import os
from dataclasses import dataclass

from flask import request

TRUSTED_PROXY_NETWORKS_ENV = "TOKENPLACE_TRUSTED_PROXY_NETWORKS"
AUTHORITATIVE_CLIENT_IP_HEADER = "CF-Connecting-IP"
MAX_TRUSTED_PROXY_ENTRIES = 32
MAX_FORWARDED_IDENTITY_LENGTH = 64
UNKNOWN_PEER = "unknown-peer"


def _canonical_ip(value: str, *, forwarded: bool) -> str | None:
    """Parse one bare address, rejecting decorated or ambiguous representations."""

    if not value or value != value.strip() or len(value) > MAX_FORWARDED_IDENTITY_LENGTH:
        return None
    if any(character in value for character in (",", "%", "[", "]")):
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if forwarded and isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return None
    if address.is_unspecified or address.is_multicast:
        return None
    # A direct peer may be presented by the WSGI server as an IPv4-mapped IPv6
    # address. Collapse it so alternate spellings cannot split a peer's quota.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.compressed


@dataclass(frozen=True)
class ClientIdentityPolicy:
    """Resolve a limiter key without exposing raw addresses to the backend."""

    trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]

    @classmethod
    def from_environment(cls) -> "ClientIdentityPolicy":
        raw = os.environ.get(TRUSTED_PROXY_NETWORKS_ENV, "")
        if not raw.strip():
            return cls(())
        entries = raw.split(",")
        if len(entries) > MAX_TRUSTED_PROXY_ENTRIES or any(not item.strip() for item in entries):
            raise ValueError(f"invalid {TRUSTED_PROXY_NETWORKS_ENV}")
        networks = []
        for item in entries:
            candidate = item.strip()
            try:
                network = ipaddress.ip_network(candidate, strict=True)
            except ValueError as exc:
                raise ValueError(f"invalid {TRUSTED_PROXY_NETWORKS_ENV}") from exc
            minimum_prefix = 8 if network.version == 4 else 32
            if network.prefixlen < minimum_prefix or network.is_multicast or network.is_unspecified:
                raise ValueError(f"unsafe {TRUSTED_PROXY_NETWORKS_ENV}")
            networks.append(network)
        return cls(tuple(networks))

    def _immediate_peer(self) -> str:
        return _canonical_ip(request.remote_addr or "", forwarded=False) or UNKNOWN_PEER

    def _peer_is_trusted(self, peer: str) -> bool:
        if peer == UNKNOWN_PEER:
            return False
        address = ipaddress.ip_address(peer)
        return any(address.version == network.version and address in network for network in self.trusted_proxies)

    def client_address(self) -> str:
        """Return a canonical address, coalescing unsafe data to the direct peer."""

        peer = self._immediate_peer()
        if not self._peer_is_trusted(peer):
            return peer
        # Werkzeug combines duplicate field lines with commas. Requiring one
        # bare address therefore rejects duplicates and every multi-hop shape.
        forwarded = request.headers.get(AUTHORITATIVE_CLIENT_IP_HEADER, "")
        return _canonical_ip(forwarded, forwarded=True) or peer

    def limiter_key(self) -> str:
        """Return a stable opaque key; raw addresses never enter limiter storage."""

        digest = hashlib.sha256(self.client_address().encode("ascii")).hexdigest()
        return f"client:{digest}"
