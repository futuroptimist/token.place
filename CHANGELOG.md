# Changelog

All notable changes to token.place are tracked here. This project uses a lightweight
[Keep a Changelog](https://keepachangelog.com/) style without duplicating release checklists.

## v0.1.1 - Multi-relay desktop and release metadata

- Added the landing-page environment/version badge so production, staging, and local development
  deploys can show public-safe release metadata without exposing secrets.
- Added desktop compute-node configuration for multiple relay URLs, including persisted migration
  from the legacy single Relay URL field.
- Enabled one desktop compute node to register with production and staging relays at the same time,
  serving the shared API v1 model `llama-3.1-8b-instruct` for both environments.
- Kept API v1 relay inference on a shared warmed llama.cpp runtime so multi-relay polling does not
  require duplicate model warm-loads for v0.1.x.
- Documented stopped-only relay URL editing, partial relay failure behavior, registered-count status,
  and stop/unregister expectations for multi-relay operators.
- Chart packaging note: the app and desktop release remain `0.1.1`; the Helm chart package version
  may be `0.1.2` because OCI chart versions are immutable deployment packages, while chart
  `appVersion` carries the token.place app release version.

## v0.1.0 - Initial production release

Initial production release, with historical evidence in the `v0.1.0` and `desktop-v0.1.0` release
tags.

- API v1 launch contract with non-streaming relay/client-server inference.
- Browser landing chat demo for encrypted relay requests.
- Live compute-node diagnostics and landing-page count.
- Sticky server routing, failover, Windows/macOS desktop compute nodes, and relay-blind E2EE signoff.
