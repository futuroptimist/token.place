# K046: explicit API v1 distributed routing

This is a breaking routing change. API v1 now resolves the operator's provider
mode before reading any distributed destination configuration. This source
change is not a deployment or a qualification of a running installation.
For this revision, this note supersedes the routing-precedence section in the
[earlier onboarding guide](relay_sugarkube_onboarding.md), which still describes
the removed request-origin/implicit routing compatibility behavior. That guide
was designated read-only in the approved K046 scope; use this note when migrating.

## Mode and request behavior

`TOKENPLACE_API_V1_COMPUTE_PROVIDER` is trimmed and lowercased:

| Value | Behavior |
| --- | --- |
| `local` | Local execution only. No distributed target validation, provider cache access, construction or HTTP activity. |
| Unset or blank | Local by default. A public/internal relay URL does not enable distributed execution. |
| `distributed` | Resolve the configured destination and fallback policy. |
| Other nonempty value | Safe configuration error, before reading destinations. |

The existing operator guardrail flag `TOKENPLACE_API_V1_ENFORCE_RELAY_DISTRIBUTED=1`
sets an unset mode to `distributed` at relay bootstrap, before request routing.
It preserves an explicit `local` mode. Intended bridge launchers should still
supply their mode, strict profile and concrete target explicitly.

Both metadata markers (`inference_target=desktop_bridge_api_v1_e2ee` and
`relay_path=api_v1_e2ee`) declare bridge intent. They cannot grant distributed
permission. Full bridge intent requires a valid encrypted request and, after
validation and moderation, explicit distributed mode. A single marker is inert.
Under distributed mode, full bridge intent tightens fallback to false.

Request Host, forwarded-host headers, peer address and metadata URLs never
supply a destination. Loopback self-dispatch requires an operator-selected URL.
A recorded immutable routing decision is used for provider selection; error
formatting does not re-resolve configuration. Distributed provider caches include
mode, target, source, profile and fallback policy. Local requests bypass them.
The existing distributed timeout setting is read when a provider is constructed;
restart the process when changing it to avoid retaining a cached timeout.

## Destination profiles and precedence

`TOKENPLACE_API_V1_DESTINATION_PROFILE` selects `general` (default), `bridge`, or
`trusted_lan`. Local/unset mode ignores this setting and all destination inputs.

Explicit distributed mode uses this priority:

1. `TOKENPLACE_API_V1_DISTRIBUTED_RELAY_URL`, then
   `TOKENPLACE_DISTRIBUTED_RELAY_URL`, then `TOKENPLACE_DISTRIBUTED_COMPUTE_URL`.
2. `TOKENPLACE_RELAY_INTERNAL_URL`, then `TOKEN_PLACE_RELAY_INTERNAL_URL`, then
   `RELAY_INTERNAL_URL`.
3. `TOKENPLACE_RELAY_PUBLIC_URL`, then `TOKEN_PLACE_RELAY_PUBLIC_URL`, then
   `RELAY_PUBLIC_URL`.
4. Non-default `api.relay_url`, then non-default `relay.server_url`.
5. `https://token.place` only for explicitly distributed `general` mode when
   `TOKEN_PLACE_ENV=production`.

HTTP(S) custom federation destinations and `/api/v1` base paths remain supported.
Malformed URLs, userinfo, query strings and fragments are configuration errors;
put any required transport authentication in an independently approved mechanism.
Diagnostics record mode, profile and source class, never configured URLs.

`general` retains the existing `TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK` switch
(default enabled). It may use local execution with no target or after a
`ComputeProviderError`. This behavior is labeled distributed with local fallback;
it cannot retract a payload already dispatched.

`bridge` and `trusted_lan` force fallback off, including when the generic fallback
switch is `1`. Neither permits a production-default destination or local retry.
Bridge strictness concerns destination selection and fallback. The `general` and
`bridge` profiles retain existing redirect, ambient proxy and netrc behavior;
only `trusted_lan` disables those HTTP behaviors as described below.

For `trusted_lan`, set `TOKENPLACE_API_V1_TRUSTED_LAN_TARGETS` to a JSON array of
exact approved HTTP(S) base URLs. The selected destination must be enumerated
and come from explicit, internal, or non-default config inputs. Public URL
variables cannot supply this profile. An out-of-profile higher-priority value
fails; it does not fall through to a lower-priority LAN value. Empty, malformed
or duplicate lists fail. Public IP literals fail; supported literal addresses
are loopback, RFC 1918 IPv4 and IPv6 unique-local addresses. Enumerated hostnames
are operator assertions about topology, not evidence of where DNS resolves.

Every trusted-LAN selection, submit, retrieve and cancellation request disables
redirects and ambient proxy/netrc configuration. A redirect is a configuration
error. This is a bounded client configuration contract, **not an end-to-end LAN
security guarantee**: DNS changes, relay federation and relay-selected compute
nodes require independent network and recipient qualification. No trusted-LAN
runtime is activated by this change.

## Explicit launch examples

The canonical [relay chart](../charts/tokenplace/templates/deployment.yaml)
defaults to `distributed`, fallback `0`, and profile `bridge`, using its existing
same-pod internal URL. `env` and `extraEnv` overrides still apply afterward;
an explicit `local` override remains local. Helm rendering and rollout have not
been performed for this change.

Generic Docker and manual entrypoints remain mode-neutral. An operator choosing
a bridge launch can use this example, replacing the synthetic destination with
the approved self-dispatch or federation endpoint. An existing explicit local
mode is preserved:

```sh
K046_RELAY_TARGET='http://127.0.0.1:5010'
TOKENPLACE_API_V1_COMPUTE_PROVIDER="${TOKENPLACE_API_V1_COMPUTE_PROVIDER:-distributed}" \
TOKENPLACE_API_V1_DESTINATION_PROFILE=bridge \
TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK=0 \
TOKENPLACE_API_V1_DISTRIBUTED_RELAY_URL="$K046_RELAY_TARGET" \
python relay.py
```

For a container, supply the same four `TOKENPLACE_API_V1_*` environment values
to the existing relay image/entrypoint. Select the target in the container's
network namespace; host loopback is not container loopback. No container or
setup script was executed during this implementation.

The real desktop E2E relay, desktop parity and `server.py` integration launchers
already declare distributed mode and a concrete URL. Their source contracts are
preserved. The generic mock E2E branch now declares local mode. The in-process
bridge integration fixture now configures mode and target instead of patching a
provider helper to bypass the policy gate. These process/socket fixtures were
prepared but not executed.

## Public routing errors

| HTTP | Code | Meaning |
| --- | --- | --- |
| 400 | `distributed_routing_disabled` | Full valid bridge intent under explicit local mode. |
| 400 | `explicit_distributed_mode_required` | Full valid bridge intent under unset/blank mode. |
| 400 | `encrypted_request_required` | Plaintext claims full encrypted bridge intent. |
| 400 | Existing envelope validation codes | Invalid encrypted envelope or decoded messages. |
| 400 | `untrusted_relay_origin` | Strict bridge has no configured target. |
| 503 | `invalid_compute_provider_configuration` | Unknown operator mode. |
| 503 | `distributed_target_configuration_error` | Invalid destination/profile configuration. |
| 503 | `no_registered_compute_nodes` / `compute_node_unreachable` | Existing safe no-capacity/unreachable classes. |
| 504 | `compute_node_timeout` / `compute_node_request_cancelled` / `compute_node_bridge_timeout` | Existing bounded timeout/cancellation classes. |

Other transport/protocol errors retain their 502 classes. No-capacity errors use
`no_registered_compute_nodes` consistently; the old implicit-public-only
`no_compute_node_available` remapping is removed. Public messages omit configured
values, raw upstream bodies and exception details. Unrelated API error handling
is outside this change.

## Verification and acceptance

The approved design was verified byte-for-byte: 32,008 UTF-8 bytes, SHA-256
`fb84366080cb832cd8bb6fa37dc47474e902ddd16781883742ad917896cce32a`.
The clean checkout and remote `main` both matched inspection commit
`a42a58f824f21d5b4c51a22db838116899d49983` before edits.

The new N01–N25 test groups reconstruct the supplied coverage summary. They are
not the unavailable historical 25-case matrix. Parameter expansion yields more
than 25 tests. The runner installs a fail-fast socket/DNS/process audit guard
before collection and mocks crypto, HTTP, clocks and model execution:

```sh
source /workspace/token-place-state/activate.sh
python tests/unit/k046/run.py
python tests/unit/k046/run.py tests/unit/test_api_v1_compute_provider.py
```

Offline verification on 2026-10-12 UTC:

- `python tests/unit/k046/run.py tests/unit/test_api_v1_compute_provider.py`:
  220 passed, including 184 new matrix/guard cases and 36 existing provider tests;
  two pre-existing `datetime.utcnow()` deprecation warnings.
- `python tests/unit/k046/run.py tests/unit/test_api_v1_launch_contract.py -k
  'n01 or relay_only_public_chat or compute_error_does_not_resolve or
  route_level_generate_response'`: 26 passed, 174 deselected; three deprecation
  warnings. This includes 22 N01 cases already counted above.
- `python tests/unit/k046/run.py tests/test_api.py -p pytest_base_url.plugin
  --base-url='' -k 'api_v1_distributed_provider or desktop_bridge_explicit_route or
  desktop_bridge_production_prefers or desktop_bridge_request_origin or
  offline_guard_is_mandatory'`: 20 passed, 247 deselected. This includes the
  mandatory guard case already counted in the matrix and explicitly enables the
  existing plugin that exposed the CI fixture-name collision.
- `python tests/unit/k046/run.py tests/unit/test_api_v1_routes_additional.py -k
  'model_error_preserves_status_and_type or echoes_request_metadata or
  sets_provider_path_and_stream_mode_headers or
  encrypted_response_sets_provider_headers or legacy_completion_sets_provider_headers
  or legacy_completion_rejects_streaming'`: 7 passed, 198 deselected;
  seven deprecation warnings.
- `python -m bandit -lll api/v1/compute_provider.py api/v1/routes.py`: no findings.
- Changed Python files compile; `python scripts/check_doc_links.py` and
  `git diff --check` pass.

Earlier diagnostic runs exposed fixture issues (constructor mocking, real-crypto
assumptions and inherited config) that were corrected before these final results.
The first remote CI run exposed eight API setup errors because the test parameter
`base_url` shadowed pytest-base-url's session fixture. It is now named
`request_origin`; the plugin-enabled guarded run above verifies the correction.
No live HTTP/DNS, real model, GPU/device, process-based integration, E2E, Helm
rendering or operational rollout is part of the offline evidence. Full existing
API/launch suites remain unrun; the selected tests above are the exact evidence.
For this K046 draft only, the owner approved the offline-tested commit/push/PR
exception on 2026-10-12 at 21:41:59 UTC. The waived local gates are
`./run_all_tests.sh` before committing and `pre-commit run --all-files` before
pushing (including its `scripts/checks.sh` full-suite hook). The pre-commit
hook environments are uncached: pre-commit-hooks v6.0.0, codespell v2.4.1,
vulture v2.14, and Bandit 1.7.5 with pbr. Full local suites require real keys,
browser/server processes and loopback networking; Valkey is absent. Chromium
is cached, while the native llama runtime and CI GGUF model are absent; the
local real-model guardrail is conditional. No installation or extra local
runtime testing was authorized or performed. Repository check configuration
remains unchanged. Existing CI and in-scope review must pass on the final head
before this draft can be called ready; this exception does not authorize merge,
deployment or an operational security claim.

Before deployment, inventory every relay/API launcher and external bridge
client, including consumers of unset-plus-public-URL inference and loopback
request-origin routing. Assign an explicit mode, profile and approved target;
verify staged configuration against the exact artifact. Qualify network and
compute boundaries independently. This patch does not change desktop hardware
mode, registration credentials, API v2, federation topology or release machinery.

Rollback is operator-owned and release-specific: reverting this source change
restores the old implicit routing behavior. Keep the launcher configuration and
routing implementation as one reviewed migration unit. Do not mark K046
remediated from source preparation or offline tests alone.
