# Ownership preflight prototype: synthetic evidence only

This standalone C++17 experiment tests ownership decisions with disposable,
in-memory multi-user fixtures. It is not linked to Tauri, MSI, NSIS, the app, or
release packaging. It cannot inspect a host: there is no real registry/profile
backend, DLL loader, profile-path input, account creation, or privilege API.
It does not claim that real loaded/offline hive inspection has been validated.
No desktop version bump is needed for this CI-only experiment.

Run with an existing C++ toolchain and CMake (no dependency installation):

```text
cmake -S experiments/windows-ownership-preflight -B build/ownership-preflight
cmake --build build/ownership-preflight --config Release
ctest --test-dir build/ownership-preflight --build-config Release --verbose
```

## Contract under test

The reader supplies complete profile inventories before and after inspection,
machine ownership, and separate loaded/offline observations. Adapters must bind
ownership to authoritative token.place registrations, scope and product identity;
matching executable paths is not enough. Inventory revisions must reflect changes
in identity and state, not just the number of profiles. This prototype checks
revision/state consistency but cannot eliminate future changes after a check.

Local, roaming, mandatory, temporary and service profiles are not silently skipped.
Signed-out profiles are inspected through the offline interface, not automatically
rejected. Missing records, denied access, unavailable locations, corrupt/busy hives,
unsupported classifications, duplicate identities, exceptions and profile changes
cannot become a clean result. A known competing owner takes precedence over unknown
evidence, while preserving the unknown count. Diagnostics do not expose identities
or claim an uninstall is required when inspection is merely incomplete.

Same-format ownership is allowed when the remaining inventory is verified clear.
An existing same-format owner does not bypass a conflict in another profile. Fixture
app/settings/model data and ownership records must remain unchanged on every outcome.
These comparisons prove only the synthetic policy is non-mutating, not real installer
preservation. Existing installer CI remains the source for same-user native evidence.

## Offreg dependency gate (not integrated)

Microsoft documents Offreg as a dynamically linked binary redistributable supplied
in the WDK. OROpenHive requires file-read access, rejects invalid hives, and does not
repair them. This establishes a candidate API, not approval for an arbitrary DLL.

No approved Offreg distribution, version/hash manifest, applicable redistribution
terms, or architecture-specific binary is present in this repository. This experiment
does not download a WDK, accept terms, use third-party DLL mirrors, or load a system DLL
by a bare filename. Real offline-adapter integration is blocked until those facts are
verified and any explicit license acceptance is approved. No dependency is silently
substituted. Source references:

- https://learn.microsoft.com/en-us/windows/win32/devnotes/offline-registry-library-portal
- https://learn.microsoft.com/en-us/windows/win32/devnotes/oropenhive
- https://learn.microsoft.com/en-us/windows/win32/devnotes/about-the-offline-registry-library

## Proposed later integration scope (requires approval)

After the dependency gate, implement a native read-only adapter against disposable CI
hive fixtures: loaded versus clean offline evidence, access denial, dirty/corrupt
hives, login/logout races, redirected/unavailable profiles and alternate execution
identities. Validate file identity, no repair/save/mount operations, bounded reads,
secure absolute DLL loading, original-hive hashes and unchanged fixture app/model
data. Do not copy whole user hives into diagnostic artifacts. A missing/corrupt hive
is an inspection failure, not proof that a competing installer is present or absent.

Before production integration, prove that the check finishes before uninstall/file
mutation. The current MSI schedules MajorUpgrade after InstallInitialize. Immediate
custom actions normally use caller privileges; an elevated deferred action is not
automatically early enough. Do not add NoImpersonate, a bootstrapper, backup/restore
privileges, scope changes or new upgrade sequencing to work around this without
approval. Real-profile enumeration and Windows API access are outside this prototype.

- https://learn.microsoft.com/en-us/windows/win32/msi/custom-action-security
- https://learn.microsoft.com/en-us/windows/win32/msi/deferred-execution-custom-actions
- https://learn.microsoft.com/en-us/windows/win32/msi/removeexistingproducts-action
- https://learn.microsoft.com/en-us/previous-versions/windows/desktop/userprofileprov/win32-userprofile

Both formats remain supported by product decision. No automatic cross-format
migration is proposed; same-format upgrades and the user's successful in-place
upgrades remain compatibility requirements. Actual inspection failure should stop
without mutation and explain the access/consistency problem, but this policy and
the real adapter must be reviewed before shipping.
