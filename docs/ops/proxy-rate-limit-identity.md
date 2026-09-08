# Proxy-aware rate-limit identity

## Repository facts and trust boundary

The documented Sugarkube public path is **Cloudflare edge → token-mode
`cloudflare-tunnel` pods → Traefik → token.place `ClusterIP` Service →
Gunicorn/Flask**. The token.place chart creates the Service, workload, and
optional Traefik Ingress; Cloudflare tunnel routing is owned outside this
repository. These records establish an expected design, not the live source
addresses, CIDRs, header transformations, or hop count.

The application therefore trusts no proxy by default. An operator may set
`TOKENPLACE_RATE_LIMIT_TRUSTED_PROXIES` (Helm
`rateLimit.trustedProxies`) to at most 32 comma-separated, canonical host
addresses or CIDRs for the **immediate peer observed by Gunicorn**. Host bits,
whitespace, zone identifiers, multicast/unspecified networks, IPv4 prefixes
broader than `/8`, IPv6 prefixes broader than `/32`, malformed entries, and a
configuration longer than 2,048 bytes abort application startup. The chart
default is an empty list; it contains no guessed production network.

## Header and hop contract

Only a trusted immediate peer may supply identity, using exactly one
`CF-Connecting-IP` value containing one canonicalizable IPv4 or IPv6 address.
This is a single-value, single-client hop contract, not arbitrary selection
from `Forwarded` or `X-Forwarded-For`. The value is limited to 45 bytes and
rejects commas (including combined duplicate fields), surrounding whitespace,
ports, IPv4-mapped IPv6, and IPv6 zone identifiers. Other forwarding and
identity-like headers are never inputs to the limiter key.

When the peer is untrusted, every forwarding header is ignored and the
normalized direct-peer address is used. When a trusted peer omits or supplies
invalid/ambiguous authoritative identity, requests are not given a
caller-selectable bucket: they coalesce into the normalized immediate-peer
bucket. An invalid direct peer coalesces into one constant bucket. This safe
fallback preserves availability while preventing malformed input from
splitting or resetting quota. Limiter storage receives only a SHA-256-derived
key, and application metrics, responses, health, diagnostics, and logs do not
emit the address or derived key.

The policy is shared by Flask-Limiter's hourly/daily public budgets and the
control-plane aggregate client-IP abuse budget. Existing authenticated
control-plane identity budgets remain separate. Exact `GET`/`HEAD` exemptions
for `/`, `/api/v1/meta`, and `/api/v1/version` are unchanged.

## Required downstream handoff

Before enabling trust, Sugarkube operators must read-only verify the live chain:

1. determine the exact immediate Traefik source addresses/networks observed by
   the token.place pod, including behavior during rescheduling and dual-stack;
2. prove Cloudflare overwrites `CF-Connecting-IP`, the tunnel and Traefik
   preserve exactly one value, and no alternate route reaches a trusted peer
   with caller-supplied identity;
3. configure only those immediate peers in environment-specific private
   Sugarkube values and perform separate IPv4/IPv6 and spoofing validation; and
4. retain network restrictions that prevent unapproved workloads from using a
   trusted source address.

No live topology was queried and this change does not modify Sugarkube,
Cloudflare, Traefik, NetworkPolicy, dashboards, alerts, or deployment state.
Shared Valkey limiter storage remains independently tracked by issue #1569.
