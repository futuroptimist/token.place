#!/usr/bin/env python3
"""Remove local desktop build artifacts left behind by an interrupted build.

A machine restarting or losing power mid `cargo build`/`cargo fetch` can leave
partially-written Cargo registry cache entries or `.rmeta` build artifacts that
fail with cryptic errors ("key with no value, expected `=`", "found invalid
metadata files", "corrupt metadata encountered") on the next build, instead of
a clean "try again" error. This clears the rebuildable local artifacts so the
next `build_local.py` run starts from a known-clean state.

Usage:
    python3 desktop-tauri/scripts/clean_local_build.py [--all] [--node-modules]
        [--runtime] [--cargo-registry] [--dry-run]

With no flags, removes only rebuildable project-local artifacts (Rust build
output, Tauri-generated schemas, local installer staging output, frontend
build output) -- always safe, always fast to regenerate.

--node-modules, --runtime, and --cargo-registry are opt-in since they're
either slower to rebuild (a fresh `npm ci` or a full embedded-runtime
re-download) or, for --cargo-registry, global: it clears
~/.cargo/registry, which is shared across every Rust project on the
machine, not just this repo. --all enables all three at once.
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

DESKTOP_TAURI_DIR = Path(__file__).resolve().parents[1]


def _remove(path: Path, *, dry_run: bool) -> None:
    if not path.exists():
        return
    if dry_run:
        print(f"[dry-run] would remove {path}")
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)
    print(f"removed {path}")


def _remove_contents_except(path: Path, keep: set[str], *, dry_run: bool) -> None:
    if not path.is_dir():
        return
    for child in sorted(path.iterdir()):
        if child.name in keep:
            continue
        _remove(child, dry_run=dry_run)


def project_artifact_paths(desktop_tauri_dir: Path = DESKTOP_TAURI_DIR) -> list[Path]:
    return [
        desktop_tauri_dir / "src-tauri" / "target",
        desktop_tauri_dir / "src-tauri" / "gen",
        desktop_tauri_dir / "release-artifacts",
        desktop_tauri_dir / "dist",
    ]


def clean(
    desktop_tauri_dir: Path = DESKTOP_TAURI_DIR,
    *,
    node_modules: bool = False,
    runtime: bool = False,
    cargo_registry: bool = False,
    dry_run: bool = False,
) -> None:
    for target in project_artifact_paths(desktop_tauri_dir):
        _remove(target, dry_run=dry_run)

    if node_modules:
        _remove(desktop_tauri_dir / "node_modules", dry_run=dry_run)

    if runtime:
        _remove_contents_except(
            desktop_tauri_dir / "src-tauri" / "python-runtime",
            keep={".gitkeep"},
            dry_run=dry_run,
        )

    if cargo_registry:
        _remove(Path.home() / ".cargo" / "registry", dry_run=dry_run)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--node-modules", action="store_true", help="also remove desktop-tauri/node_modules (npm ci will reinstall)")
    parser.add_argument(
        "--runtime",
        action="store_true",
        help="also remove the embedded Python runtime (next build_local.py run re-downloads/re-prepares it)",
    )
    parser.add_argument(
        "--cargo-registry",
        action="store_true",
        help=(
            "also remove the GLOBAL ~/.cargo/registry cache -- shared across ALL Rust "
            "projects on this machine, not just token.place. Only needed if you're "
            "seeing Cargo errors like 'key with no value, expected `=`' or "
            "'found invalid metadata files' / 'corrupt metadata encountered'"
        ),
    )
    parser.add_argument("--all", action="store_true", help="shorthand for --node-modules --runtime --cargo-registry")
    parser.add_argument("--dry-run", action="store_true", help="print what would be removed without removing anything")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    clean(
        node_modules=args.node_modules or args.all,
        runtime=args.runtime or args.all,
        cargo_registry=args.cargo_registry or args.all,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
