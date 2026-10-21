# Windows installer ownership

Both formats remain available in the unreleased desktop 0.1.25 train. NSIS keeps
its current-user scope and MSI keeps its per-machine scope. No profile scanning,
hive loading, privilege acquisition or automatic cross-format migration occurs.

Fresh installs refuse a visible competing owner or a nonempty unowned target.
The initial NSIS GUI check defers only unowned-target emptiness so the directory
page can select an empty folder; strict checks precede every installation section.
Same-format upgrades require a verified existing registration and unchanged
canonical target. Existing mixed installations may upgrade independently only
when their targets are disjoint. Overlap, nesting, directory aliases, executable
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
The helper reads installation metadata and file identities only. NSIS stores its
canonical result in its private temporary directory. The MSI action recovers the
related product's registered directory before costing and rejects a conflicting
explicit target before maintenance costing can replace it with component history.
It then verifies the resolved
target in the execute sequence before InstallInitialize/RemoveExistingProducts.
Neither `Installed` nor `/UPDATE` grants upgrade authority.

NSIS no longer selects or executes an MSI uninstaller. Before invoking an older
NSIS uninstaller it validates the owner and passes `/UPDATE` to preserve data.
It retains the verified canonical target across that uninstall, rechecks before
copying, and reports an interrupted upgrade distinctly if the later check fails.
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
advancing, observes the verified owner's uninstaller, and sends each enabled
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
