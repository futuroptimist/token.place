# Relay release safety gate

Every relay image published from `main` or a `v*` release tag must pass the artifact-level contract
in `config/relay_release_safety_contract.json`. The gate starts the already-built candidate image,
uses deliberately low local quotas, and checks bounded metrics and public-information exemptions.
It does not inspect fix-commit ancestry, so a behavior-equivalent maintenance backport passes when
its observable artifact behavior satisfies every requirement.

## Run locally

```bash
docker build -t tokenplace-relay:safety .
SOURCE_COMMIT="$(git rev-parse HEAD)" RELEASE_REF="$(git branch --show-current)" \
  ./scripts/run_relay_release_safety_gate.sh tokenplace-relay:safety
```

The command creates `relay-release-safety-evidence.json`, containing only non-secret provenance and
one boolean per contract requirement. A missing, malformed, unknown, skipped, or failed check makes
the command non-zero. Probe paths are random and are never written to evidence or standard output;
the artifact must not expose them in metrics.

## Maintain the contract

To add a mandatory requirement:

1. Add a stable ID and reviewable description to the JSON contract.
2. Add that ID to `IMPLEMENTED_CHECKS` and implement an artifact-level assertion in
   `run_contract`; the strict ID comparison prevents a declaration without an implementation.
3. Add positive and negative deterministic tests, including a test that disabling the behavior
   fails closed.
4. Run the local image gate and focused tests before merging.

An equivalent backport needs no ancestry exception or bypass. Reviewers approve it by reviewing the
implementation and normal pull request, while the same contract tests its built image. Do not edit
evidence booleans or weaken thresholds to approve a backport. A failure names only the stable check
ID: reproduce locally, inspect the candidate source, and correct the behavior before publication.
