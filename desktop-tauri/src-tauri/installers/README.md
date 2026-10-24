# Windows installer ownership

Both formats remain available in the unreleased desktop 0.1.25 train. NSIS keeps
its current-user scope and MSI keeps its per-machine scope. No profile scanning,
hive loading, privilege acquisition or automatic cross-format migration occurs.

Fresh installs refuse a visible competing owner or a nonempty unowned target.
The initial NSIS GUI check defers only unowned-target emptiness so the directory
page can select an empty folder; strict checks precede every installation section.
Same-format operations require a verified existing registration and unchanged
canonical target. NSIS upgrades and protected MSI maintenance can proceed in
existing mixed installations only when their targets are disjoint. Overlap,
nesting, directory aliases, executable
hard-link aliases, duplicate registrations and embedded reparse points fail
closed. An inaccessible or stale matching registration is not treated as absent.

Discovery is deliberately limited to HKCU and HKLM. HKLM uses both registry
views; HKCU's shared Software tree is read once rather than counted twice.
Consequently a machine MSI install cannot establish absence of another user's
private NSIS registration. A fresh NSIS install can see a machine MSI and refuses
it. This is a bounded ownership policy, not a global single-installation or
concurrent mixed-version compatibility guarantee.

`ownership.cpp` compiles with the existing MSVC toolchain into an NSIS helper
executable and an MSI custom-action DLL. The CRT is statically linked; no binary
download or new runtime dependency is required. Cargo fails if compilation fails.
The helper reads installation metadata, file identities and matching process image paths. NSIS stores its
canonical result in its private temporary directory. The MSI action recovers the
related product's registered directory before costing and rejects a conflicting
explicit target before maintenance costing can replace it with component history.
It then verifies the resolved
target in the execute sequence before InstallInitialize/RemoveExistingProducts.
Neither `Installed` nor `/UPDATE` grants upgrade authority.

Automatic MSI upgrades from v0.1.24 and all other unaudited predecessor packages
refuse before `RemoveExistingProducts`, with close-app/uninstall-first guidance.
The candidate's `DisableShutdown` property does not control the predecessor's
cached removal session. The guard never opens or edits that cached database:
Microsoft prohibits `MsiOpenDatabase` in custom actions. Instead it requires an
installed per-machine ProductCode/PackageCode pair, no registered transforms,
no patches in any state, and successful metadata queries. It accepts only the
exact current package or an explicitly audited predecessor pair. Current package
identity and shutdown policy come from the active database and summary, not
caller-supplied public properties. There are **no audited predecessor entries**
in this candidate. Exact-package maintenance remains supported; a different
MSI package requires explicit uninstall first until its predecessor is audited.

This is a registered-package compatibility policy, not a cryptographic check of
cached bytes. Substituted cached bytes retaining an allowed identity are outside
this check. A future protected major upgrade requires a reviewed identity entry
with artifact/configuration provenance, plus hosted major-upgrade tests. A version
range, registry marker or generic test override must never grant that trust.
The current hosted matrix tests legacy refusal/preservation and exact-package
maintenance; synthetic policy tests separately exercise audited-pair matching.

NSIS never invokes an external legacy uninstaller: those binaries can terminate
processes by basename, and a preflight cannot eliminate the launch-after-check
race. Passive and update installs use the existing in-place route. An explicit
GUI uninstall/reinstall selection refuses with guidance to select in-place
reinstallation instead. Standalone candidate uninstall remains available.
New NSIS uninstallers always preserve per-user settings and models because the
namespace can be used by another installation. They no longer offer the delete
app-data checkbox. Existing path-scoped shortcut removal remains in place.

These checks run immediately before mutation; they are not a system-wide lock
against concurrent administrator changes. Test fixtures are disposable hosted
Windows state. Single-owner harness assertions remain strict; mixed-owner tests
separately snapshot the competing owner and shared data and test survivor config
readback. Synthetic policy tests compile without registry discovery code.

The hosted interactive harness exercises dialog notification handlers, not physical
mouse input. It waits for maintenance controls and checked-state readback before
advancing, rejects any external uninstaller launch, and sends each enabled
button's documented `WM_COMMAND`/`BN_CLICKED` notification once. Bounded control
text reads and flushed UI traces retain evidence when a page does not progress.

Mixed-owner upgrade fixtures use ordinary ASCII installation paths to establish
healthy legacy owners. In retained hosted run 39552571152, the v0.1.24 NSIS seed
created invalid shortcut targets for a non-ACP path before any candidate upgrade;
the cause is not attributed here and the fixture does not repair those shortcuts.
Native Unicode path/ownership/INI tests and actual PowerShell Unicode serialization
tests cover their respective boundaries, not end-to-end Unicode installer support.

The template source and license are pinned in `upstream.json` and `LICENSE_MIT`.
`template-changes.patch` records every change from that source; a regression
reverses the patch in memory and verifies the pristine upstream hashes. Rebase
the patch deliberately when changing Tauri CLI versions.

Native API references:

- [MsiEnumRelatedProducts](https://learn.microsoft.com/en-us/windows/win32/api/msi/nf-msi-msienumrelatedproductsw)
- [MsiGetProductInfo](https://learn.microsoft.com/en-us/windows/win32/api/msi/nf-msi-msigetproductinfow)
- [MsiGetTargetPath](https://learn.microsoft.com/en-us/windows/win32/api/msiquery/nf-msiquery-msigettargetpathw)
- [GetFinalPathNameByHandle](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfinalpathnamebyhandlew)
- [CostFinalize sequencing](https://learn.microsoft.com/en-us/windows/win32/msi/costfinalize-action)

Running-app checks use Toolhelp discovery plus limited process-image queries.
A matching main-executable basename with inaccessible/unknown identity refuses;
a verified disjoint image may continue. Canonical target membership and hard-link
identity refuse. Neither adapter requests process termination. NSIS embeds the
helper in both installer and uninstaller and checks before hooks/mutation. MSI
checks after costing and before InstallValidate, including uninstall, and authors
MSIRESTARTMANAGERCONTROL=DisableShutdown. These snapshots are not a global
process-launch lock. Cached predecessor policy is handled by the explicit
compatibility restriction above, not assumed to inherit from the candidate.

Hosted survival fixtures temporarily substitute a pipe-controlled sleeper for
only a disposable installed main executable, then restore the original after
normal pipe-close exit. They compare installation/authority/settings/model bytes
and verify both relevant and disjoint processes survive. They do not represent
full application activity or prove absence of a launch-after-preflight race.

- [QueryFullProcessImageName](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-queryfullprocessimagenamew)
- [MSIRESTARTMANAGERCONTROL](https://learn.microsoft.com/en-us/windows/win32/msi/msirestartmanagercontrol)
- [RemoveExistingProducts](https://learn.microsoft.com/en-us/windows/win32/msi/removeexistingproducts-action)
- [Functions prohibited in custom actions](https://learn.microsoft.com/en-us/windows/win32/msi/functions-not-for-use-in-custom-actions)
- [Registered product information](https://learn.microsoft.com/en-us/windows/win32/api/msi/nf-msi-msigetproductinfoexw)
- [Package identity rules](https://learn.microsoft.com/en-us/windows/win32/msi/package-codes)
