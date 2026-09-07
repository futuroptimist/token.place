"""Bounded, fail-closed client identity for application rate limits."""

from __future__ import annotations

import hashlib
import ipaddress
import os
from dataclasses import dataclass

from flask import Request, current_app, request

TRUSTED_PROXIES_ENV = "TOKENPLACE_RATE_LIMIT_TRUSTED_PROXIES"
AUTHORITATIVE_HEADER = "CF-Connecting-IP"
MAX_CONFIG_LENGTH = 2048
MAX_TRUSTED_NETWORKS = 32
MAX_HEADER_LENGTH = 45


class TrustedProxyConfigurationError(ValueError):
    """Raised when proxy trust cannot be configured safely."""


def _parse_address(
    value: str, *, allow_mapped: bool
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if not value or value != value.strip() or "%" in value:
        raise ValueError("invalid address shape")
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        if not allow_mapped:
            raise ValueError("IPv4-mapped IPv6 is ambiguous")
        return address.ipv4_mapped
    return address


def parse_trusted_proxy_networks(
    raw: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse a small explicit peer allowlist; reject broad or malformed trust."""

    if not raw:
        return ()
    if len(raw) > MAX_CONFIG_LENGTH:
        raise TrustedProxyConfigurationError("trusted-proxy configuration is too long")
    values = raw.split(",")
    if len(values) > MAX_TRUSTED_NETWORKS:
        raise TrustedProxyConfigurationError("too many trusted-proxy networks")
    networks = []
    for value in values:
        if not value or value != value.strip() or "%" in value:
            raise TrustedProxyConfigurationError("invalid trusted-proxy network")
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError as exc:
            # A single host remains convenient but CIDRs with host bits are rejected.
            try:
                address = _parse_address(value, allow_mapped=False)
                network = ipaddress.ip_network(f"{address}/{address.max_prefixlen}")
            except ValueError:
                raise TrustedProxyConfigurationError(
                    "invalid trusted-proxy network"
                ) from exc
        minimum_prefix = 8 if network.version == 4 else 32
        if (
            network.prefixlen < minimum_prefix
            or network.is_unspecified
            or network.is_multicast
        ):
            raise TrustedProxyConfigurationError(
                "trusted-proxy network is dangerously broad"
            )
        networks.append(network)
    return tuple(networks)


@dataclass(frozen=True)
class ClientIdentityPolicy:
    """Select one canonical address without exposing it outside limiter internals."""

    trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

    @classmethod
    def from_environment(cls) -> "ClientIdentityPolicy":
        return cls(
            parse_trusted_proxy_networks(os.environ.get(TRUSTED_PROXIES_ENV, ""))
        )

    @staticmethod
    def _direct_peer(
        req: Request,
    ) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        try:
            return _parse_address(req.remote_addr or "", allow_mapped=True)
        except ValueError:
            return None

    def client_address(self, req: Request) -> str:
        """Return authoritative client address or a deterministic coalescing key.

        Invalid identity never rejects application traffic: it coalesces to the
        direct peer (or one global invalid-peer bucket), so caller input cannot
        create fresh quota buckets.
        """

        peer = self._direct_peer(req)
        fallback = str(peer) if peer is not None else "invalid-direct-peer"
        if peer is None or not any(peer in network for network in self.trusted_proxies):
            return fallback

        raw = req.headers.get(AUTHORITATIVE_HEADER)
        if raw is None or len(raw) > MAX_HEADER_LENGTH or "," in raw:
            return fallback
        try:
            return str(_parse_address(raw, allow_mapped=False))
        except ValueError:
            return fallback

    def limiter_key(self, req: Request) -> str:
        address = self.client_address(req)
        return "client:" + hashlib.sha256(address.encode("ascii")).hexdigest()


def current_client_address() -> str:
    policy = current_app.extensions.get("tokenplace_client_identity_policy")
    if policy is None:
        policy = ClientIdentityPolicy()
    return policy.client_address(request)


def current_limiter_key() -> str:
    return current_app.extensions["tokenplace_client_identity_policy"].limiter_key(
        request
    )
