# Trusted-proxy client identity

## Repository-established boundary

Sugarkube documents the expected public request chain as **Cloudflare edge → token-mode
cloudflare-tunnel pods → Traefik → token.place ClusterIP Service → Gunicorn/Flask**. The
token.place chart confirms that its Service is a `ClusterIP` and that Gunicorn runs the Flask
application. These are repository facts and a deployment expectation, not verification of the live
route, source addresses, forwarding behavior, or header preservation.

The application uses only `CF-Connecting-IP`, at exactly one value/hop, as the authoritative client
identity. This is the minimum application-owned contract consistent with Cloudflare being the
public edge. It does not parse `Forwarded`, `X-Forwarded-For`, or `X-Real-IP`. The immediate WSGI
peer must first belong to `TOKENPLACE_TRUSTED_PROXY_NETWORKS`; this must contain verified Traefik
pod/source networks, **not** Cloudflare's public address ranges or the cloudflared pod network.

## Configuration and failure policy

`TOKENPLACE_TRUSTED_PROXY_NETWORKS` is an optional comma-separated list of strict IPv4 or IPv6
CIDRs (at most 32). The Helm interface is `trustedProxy.networks`. Both default to empty, so all
forwarding headers are ignored and the normalized direct peer supplies the quota identity.

Startup rejects malformed CIDRs, host bits, empty list members, more than 32 entries, multicast or
unspecified networks, IPv4 prefixes broader than `/8`, and IPv6 prefixes broader than `/32`. These
bounds prevent an accidental universal trust grant; they do not make an unverified network safe.

For a trusted immediate peer, `CF-Connecting-IP` must be one bare IPv4 or IPv6 address of at most 64
characters. Empty, duplicate/comma-joined, multi-hop, whitespace-padded, port-bearing,
IPv4-mapped-IPv6, zone-qualified, unspecified, multicast, or malformed values are not trusted. The
request is not rejected: it safely coalesces into the immediate peer's quota bucket. This preserves
availability without allowing caller-controlled identity splitting. Valid addresses are canonicalized
with Python's standard `ipaddress` parser. Limiter storage receives only a SHA-256-derived key.

The public-information exemption remains limited to exact `GET`/`HEAD` requests for `/`,
`/api/v1/meta`, and `/api/v1/version`; all other existing hourly, daily, control-plane, and inference
limits remain in force. Metrics continue to use only closed application-owned label vocabularies.
Application request logs omit addresses and limiter keys, and the container disables Gunicorn's
default address-bearing access log.

## Required operator handoff (not performed by this repository change)

Before enabling proxy trust, Sugarkube operators must verify the live chain, determine the actual
immediate peer addresses as observed by Flask, prove that Traefik preserves exactly one
Cloudflare-authenticated `CF-Connecting-IP` value while replacing/removing caller-supplied values,
and then configure the narrow verified Traefik source CIDRs. Operators must test both IP families,
direct/bypass attempts, duplicate headers, and rolling pod/source-address changes in staging before
production. No live CIDR, header behavior, hop count, Cloudflare, Traefik, NetworkPolicy, or cluster
configuration is asserted or changed here.
