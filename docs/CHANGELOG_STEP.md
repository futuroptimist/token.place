# token.place Step-by-Step Changelog

This document captures incremental improvements to `token.place` over time. Add new entries chronologically under the current date heading.

## [2025-07-01]
### Added
- Rate limiting and Prometheus metrics via `Flask-Limiter` and `prometheus_flask_exporter`.
- Weekly Dependabot configuration.
- Docker build workflow for GHCR pushes.
- Resource limits and health probes in Kubernetes manifests.

### Changed
- Requirements updated to include new dependencies.
- `api/__init__.py` now initializes rate limiting and metrics.
- README expanded with quickstart instructions, architecture link, and environment variable notes.
- Kubernetes documentation mentions production-ready probes and resources.

## [2025-08-06]
### Changed
- Windows path helpers now default to `AppData` directories when environment variables are missing.

## [2025-08-20]
### Fixed
- Crypto client now rejects empty chat messages before hitting the network.
