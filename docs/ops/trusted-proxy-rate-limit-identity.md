# Trusted-proxy rate-limit identity

## Repository facts and trust boundary

The token.place Helm chart deploys a ClusterIP service in front of Gunicorn/Flask. Sugarkube's
current documentation describes the intended public path as Cloudflare edge → token-mode
`cloudflare-tunnel` pods → Traefik → the token.place ClusterIP service → Gunicorn/Flask. This is a
repository-documented expectation, **not live evidence** of the deployed addresses, CIDRs, header
rewrites, or hop count.

The application therefore treats its immediate socket peer as the only independently observable
trust boundary. It accepts a forwarded identity only when all of these conditions hold:

1. `REMOTE_ADDR` is a canonical IP in an explicitly configured trusted network;
2. exactly one bounded `CF-Connecting-IP` value is present and is one bare canonical IPv4 or IPv6
   address; and
3. `Forwarded`, `X-Forwarded-For`, and `X-Real-IP` are absent.

`CF-Connecting-IP` is the single authoritative, one-element header for this contract. The
application does not walk a caller-controlled forwarding chain or choose a leftmost/rightmost hop.
It rejects commas, whitespace changes, ports, brackets, zone identifiers, IPv4-mapped IPv6, empty
values, malformed values, and values longer than 64 bytes. Duplicate HTTP header fields are joined
by normal WSGI servers and consequently contain a comma, so they fail the same bounded rule.

## Safe fallback and privacy

The default trusted-network list is empty. Forwarding headers from an untrusted peer are ignored.
Direct requests use the canonical immediate-peer address. If an explicitly trusted peer supplies
missing, malformed, duplicate, conflicting, or otherwise ambiguous identity data, the request is
not rejected: it deterministically **coalesces into the trusted immediate peer's quota bucket**.
This may conservatively share quota but never lets the supplied value select, split, or reset a
bucket.

Limiter keys are SHA-256 domain-separated digests, not addresses. Application-owned metrics keep
the existing closed route/method/outcome/reason vocabularies; neither raw addresses nor derived
keys are labels. The container disables Gunicorn's address-bearing access log. Do not add identity
values to logs, errors, traces, health, diagnostics, or incident evidence.

The same resolver supplies public, daily, streaming, and aggregate control-plane IP limits. The
authenticated public-key control-plane buckets remain separate and unchanged. Storage remains a
Flask-Limiter concern, allowing the separately tracked shared-limiter work to reuse these stable
opaque keys.

## Configuration and operator handoff

Set `TOKENPLACE_RATE_LIMIT_TRUSTED_PROXIES` to a comma-separated list of canonical immediate-peer
IP networks. The Helm interface is `rateLimit.trustedProxies`. An empty string is the safe default.
Parsing is bounded to 4,096 bytes and 32 networks. Host bits, whitespace, empty elements, malformed
networks, multicast/unspecified networks, IPv4 prefixes broader than `/8`, and IPv6 prefixes broader
than `/32` fail application startup rather than silently enabling trust. Exact peer addresses may
be written as `/32` or `/128`.

Before enabling this setting, the Sugarkube operator must perform a repository-external handoff:

- observe and record the actual immediate Traefik peer address/network seen by the application in
  each environment without publishing it through application diagnostics;
- verify Cloudflare and every intermediary overwrite/remove caller-provided identity fields, emit
  exactly one canonical `CF-Connecting-IP`, and strip the three conflicting headers before Traefik
  reaches token.place;
- verify the live chain, service routing, address-family behavior, and any NAT do not introduce an
  additional hop; and
- stage-test two external clients for independent quotas, malformed-header coalescing, direct-peer
  fallback, and rollback before changing production.

Do not guess these values from pod/service CIDRs in documentation. This repository change does not
alter Cloudflare, Traefik, NetworkPolicy, Sugarkube, dashboards, alerts, or a live deployment.
