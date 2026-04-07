# token.place documentation

## Repository map

Use this page as your hub: it points to the canonical guides for setting up, running,
testing, and deploying token.place. For an expanded overview of every directory, see
[REPO_MAP.md](REPO_MAP.md).

- **Set up a development environment**
  - Start here: [README.md](../README.md#quickstart)
  - Also see: [ONBOARDING.md](ONBOARDING.md), [DEVELOPMENT.md](DEVELOPMENT.md)
- **Understand architecture choices**
  - Start here: [ARCHITECTURE.md](ARCHITECTURE.md)
  - Also see: [STREAMING_IMPLEMENTATION_GUIDE.md](STREAMING_IMPLEMENTATION_GUIDE.md),
    [CROSS_PLATFORM.md](CROSS_PLATFORM.md),
    [design/tauri_desktop_client.md](design/tauri_desktop_client.md) (forward-looking desktop direction; `desktop-tauri/` is still MVP)
  - Canonical migration order: [roadmap/desktop_compute_node_migration.md](roadmap/desktop_compute_node_migration.md)
- **Run or deploy the services**
  - Start here: [README.md](../README.md#quickstart)
  - Also see: [RPI_DEPLOYMENT_GUIDE.md](RPI_DEPLOYMENT_GUIDE.md),
    [docker-compose.yml](../docker-compose.yml), [k8s/](../k8s/),
    [relay_sugarkube_onboarding.md](relay_sugarkube_onboarding.md),
    [k3s-sugarkube-dev.md](k3s-sugarkube-dev.md),
    [k3s-sugarkube-staging.md](k3s-sugarkube-staging.md),
    [k3s-sugarkube-prod.md](k3s-sugarkube-prod.md)
- **Learn the encryption model**
  - Start here: [encrypt.py](../encrypt.py)
  - Also see: [SECURITY_PRIVACY_AUDIT.md](SECURITY_PRIVACY_AUDIT.md),
    [STREAMING_IMPLEMENTATION_GUIDE.md](STREAMING_IMPLEMENTATION_GUIDE.md)
- **Explore client experiences**
  - Start here: [static/](../static)
  - Also see: [CROSS_PLATFORM.md](CROSS_PLATFORM.md),
    [api_v2_model_catalog.md](api_v2_model_catalog.md)
- **Contribute code confidently**
  - Start here: [CONTRIBUTING.md](../CONTRIBUTING.md)
  - Also see: [TESTING.md](TESTING.md), [TESTING_IMPROVEMENTS.md](TESTING_IMPROVEMENTS.md),
    [STYLE_GUIDE.md](STYLE_GUIDE.md)

## Key checklists

- **Testing:** [TESTING.md](TESTING.md) summarises the suites executed by `run_all_tests.sh`.
- **Security:** [SECURITY_PRIVACY_AUDIT.md](SECURITY_PRIVACY_AUDIT.md) contains the rolling
  hardening checklist and threat model; use
  [SECURITY_REVIEW_CHECKLIST.md](SECURITY_REVIEW_CHECKLIST.md) during release sign-off.
- **Release notes:** Track changes in [CHANGELOG.md](CHANGELOG.md) and stepwise updates in
  [CHANGELOG_STEP.md](CHANGELOG_STEP.md).

## Prompt docs

- [Implement](prompts/codex/implement.md)
- [Automation](prompts/codex/automation.md)
- [Polish](prompts/codex/polish.md)
