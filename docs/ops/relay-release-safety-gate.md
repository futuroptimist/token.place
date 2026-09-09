# Relay release safety gate

Every production-eligible relay image publication is blocked on the behavioral contract in
`config/relay_release_safety_contract.json`. The gate starts the built candidate image with
fresh isolated containers and tests its HTTP and Prometheus surfaces. The metrics container uses
quota ceilings above its 2,048-request cardinality probe, while each quota assertion uses its own
deliberately small quota so no phase can consume another phase's state. It does not query
staging or production, inspect commit ancestry, or record requests, client identity, credentials,
or sampled paths.

## Run locally

Build the image with immutable revision metadata, then run the same entry point as CI:

```bash
commit=$(git rev-parse HEAD)
docker build --label "org.opencontainers.image.revision=$commit" -t tokenplace-relay:safety .
python scripts/relay_release_safety_gate.py \
  --image tokenplace-relay:safety \
  --platform linux/amd64 \
  --source-commit "$commit" \
  --release-ref "$(git symbolic-ref -q --short HEAD || git rev-parse HEAD)" \
  --release-base main \
  --evidence relay-release-safety-evidence.json
```

For a multi-platform index, resolve each descriptor with `docker buildx imagetools inspect --raw`,
pull the descriptor by its `sha256:` digest and pass both `--index-digest` and
`--platform-digest`. Run once with `--platform linux/amd64` and once with
`--platform linux/arm64`; QEMU/binfmt must be installed when the host cannot execute the selected
platform. A platform absent from the index, a platform mismatch reported by Docker, or either gate
failure blocks the index as a whole.

A passing report contains `passed: true`, the source/ref and immutable image identity, plus a result
for every contract requirement. For publication, CI first pushes a uniquely named non-release
candidate index, pulls each advertised platform by digest, qualifies every platform artifact, and
only then attaches the production-eligible tags to the index. CI uploads one non-secret report per
platform and records the OCI index digest in the
workflow summary. Missing, duplicate, skipped, malformed, mismatched, or false results fail
closed. The report intentionally excludes request bodies, credentials, client identities, logs, and
the randomized unmatched paths used by the probe. Evidence records only bounded series growth,
request counts, privacy-safe HTTP status codes, and immutable identities.

The metrics phase sends two successive batches of 1,024 distinct unmatched paths. Its rate and
daily ceilings exceed the complete 2,052-request probe (including four metric scrapes). It
independently rejects default Flask metric families and raw or attacker-controlled path labels,
examines identities introduced in either batch (including collectors that stop at a fixed cap), and
requires the second batch to add no series identities. Fixed lazy `unknown`, `other`, and
`/{unmatched}` fallback series remain valid. Quota phases prove that only `GET` and `HEAD` requests
to `/`, `/api/v1/meta`, and `/api/v1/version` receive the public-information exemption. After those
public probes, the registered `/api/v1/models` route must return exactly `200` then `429`; unrouted
404/405 responses are never accepted as quota enforcement. A bounded request-context check inside
the inspected image independently requires the existing exemption predicate to accept every exact
public GET/HEAD pair and reject neighboring paths and POST on each actual public path. The check
fails closed if that predicate cannot be inspected. Separate fresh containers prove rate and daily
enforcement on the protected read route `/api/v1/models` and on the
mutating route `/api/v1/relay/requests/cancel`. The mutating probe sends an empty JSON object: its
first `400` response occurs during validation before any state-store mutation, and its second
request must receive `429` from the selected limiter.

The evidence distinguishes `failed` from `not_run` checks and uses sanitized error categories.
`startup_timeout` means the bounded readiness deadline expired (transient connection resets are
retried only during that startup window); `runtime_missing`, `runtime_timeout`, and
`runtime_command_failed` identify local Docker failures. A cleanup failure is recorded separately
and makes the result fail without replacing the original category. HTTP errors, redirects, and
transport failures after readiness are check failures rather than responses that satisfy a probe.

## Publication entrypoints

`.github/workflows/ci-image.yml` is the sole relay-image publication entrypoint. Pull requests and
manual dispatches validate only; pushes to `main` and immutable semantic-version tags build a
unique candidate index. The workflow qualifies the two descriptors from that exact digest before
`imagetools create` attaches release tags. Manual runs record the requested `ref` and reviewed
`base`, rather than substituting the workflow file's ref. Extending the advertised platform list
requires adding the platform to the build, exact manifest-membership assertion, qualification loop,
workflow regression tests, and operator documentation in the same change.

## Read-only qualification of an existing OCI index

`.github/workflows/qualify-existing-relay-oci.yml` is a manual, read-only executor for an
already-published index in the fixed repository `ghcr.io/futuroptimist/tokenplace-relay`. It checks
out the gate from `main` and records that gate implementation commit independently of the supplied
artifact source commit. The dispatch accepts a lowercase full `sha256:` index digest, a lowercase
full 40-character source commit, bounded release ref and base values, and a bounded evidence label.
All values are validated before use. The executor does not build, publish, promote, retag, deploy,
or inspect an external environment.

The executor sanitizes the raw index, rejects malformed descriptors and executable platforms other
than exactly one `linux/amd64` and one `linux/arm64`, and pulls each accepted descriptor by digest.
It runs the current safety gate once per platform on separate loopback port ranges. Qualification
fails closed unless both evidence documents parse, bind to all supplied and resolved identities,
contain every mandatory contract result exactly once with a passing state, and report successful
cleanup. The uploaded artifact is uniquely named
`relay-oci-qualification-<evidence-label>-<run-id>-<run-attempt>` and contains:

- `index-manifest.sanitized.json` — bounded descriptor and platform data, with annotations omitted;
- `amd64-evidence.json` and `arm64-evidence.json` — the gate's privacy-safe schema-version 2 reports;
- `metadata.json` — source, release, index, descriptor, gate-commit, cleanup, and result bindings;
- `SHA256SUMS` — hashes for the four JSON records above.

The workflow uploads available evidence even when qualification fails, then fails the job if either
platform evidence is missing or invalid or local cleanup did not succeed. Evidence must remain free
of credentials, authorization headers, generated paths, request or response bodies, client
identities, keys, tokens, ciphertext, prompts, and model output.

### Planned Step 05b dispatches

These are the two reviewed parameter sets planned for separate dispatches **after this executor is
merged**. Listing them here records intent only; neither artifact is claimed to have passed.

| Input | Qwen candidate | Corrected Llama rollback |
| --- | --- | --- |
| `index_digest` | `sha256:b32ef19840dabe44caf7240b787af14dcf439b39357833e21a92c3dd511effd4` | `sha256:543fde33aff45253630090b52d16163e3586da12c973f5c4a658ddc8927d0a68` |
| `source_commit` | `8618c9aba4b5dfe7980c2fe861095a92311145f2` | `6c39adc64e7bed4f85d07164aa2860e637919ca9` |
| `release_ref` | `main-8618c9a` | `sha-6c39adc` |
| `release_base` | `main` | `release/relay-0.1.1` |
| `evidence_label` | `step-05b-qwen-8618c9a` | `step-05b-llama-6c39adc` |

## Maintain the contract

To add a mandatory requirement:

1. Add a stable ID and description to `config/relay_release_safety_contract.json`.
2. Add the same ID to `EXPECTED_IDS` and the appropriate executable metrics or quota check.
3. Add passing and failing fixtures in `tests/unit/test_relay_release_safety_gate.py`.
4. Run the unit tests and the local image command above.

The one-to-one ID validation prevents a review-only declaration or an undeclared executable check.
Keep probes deterministic in their assertions, bounded in traffic, and free of sensitive evidence.

## Behavior-equivalent backports

The contract approves observed behavior rather than requiring particular commits to be ancestors.
A maintenance backport is therefore eligible when reviewers explicitly approve its equivalence and
the built image passes every current contract result with matching revision metadata. New artifacts
must carry the complete source SHA. A historical 7--39 character label is accepted only when the
caller supplies the independently resolved full revision and it exactly equals the pinned source;
arbitrary prefix matching is forbidden. Reviewers should link that approval, both platform evidence
artifacts, and the immutable index and descriptor digests in the pull request or release record. An
ancestry claim is neither needed nor accepted as a substitute for the behavioral gate.
