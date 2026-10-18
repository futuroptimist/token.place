"""Offline Linux package integrity and target-selection regression tests."""

import importlib
import json
import sys
import tarfile
from types import SimpleNamespace
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "desktop-tauri/scripts"
sys.path.insert(0, str(SCRIPTS))
linux = importlib.import_module("prepare_linux_embedded_python_runtime")
validator = importlib.import_module("validate_linux_desktop_package")


@pytest.mark.parametrize(
    "target,accepted",
    [("../x/xterm", True), ("../../../../outside", False), ("/tmp/outside", False)],
)
def test_linux_archive_parent_links_remain_inside_root(target, accepted):
    member = tarfile.TarInfo("python/share/terminfo/1/1178")
    member.type = tarfile.SYMTYPE
    member.linkname = target
    if accepted:
        linux.shared.validate_tar_member(member, "python", allow_internal_parent_links=True)
        with pytest.raises(linux.shared.RuntimePrepError):
            linux.shared.validate_tar_member(member, "python")
    else:
        with pytest.raises(linux.shared.RuntimePrepError):
            linux.shared.validate_tar_member(member, "python", allow_internal_parent_links=True)


def test_linux_probe_overrides_host_libpython_and_uses_isolated_mode(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(linux.shared, "run", lambda args, **kwargs: calls.append((args, kwargs)))
    py = tmp_path / "bin/python3"
    linux.run_python(py, ["-c", "pass"])
    args, kwargs = calls[0]
    assert args == [str(py), "-I", "-B", "-c", "pass"]
    assert kwargs["env"]["LD_LIBRARY_PATH"] == str(tmp_path / "lib")
    assert kwargs["env"]["LD_PRELOAD"] == ""


@pytest.mark.parametrize(
    "key,value",
    [
        ("target_triple", "aarch64-unknown-linux-gnu"),
        ("expected_architecture", "arm64"),
        ("expected_backend", "cuda"),
        ("sha256", "unpinned"),
        ("archive_url", "https://example.test/python.tar.gz"),
        ("cpython_version", "3.12.0"),
        ("required_packages", {}),
    ],
)
def test_rejects_wrong_or_unpinned_manifest(tmp_path, key, value):
    manifest = linux.load_manifest()
    manifest[key] = value
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(linux.shared.RuntimePrepError):
        linux.load_manifest(path)


def make_runtime(root):
    for name in linux.STDLIB:
        path = root / "lib/python3.11" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic integrity fixture\n")


@pytest.mark.parametrize("version,valid", [("0.3.32", True), ("0.3.31", False)])
def test_distribution_names_are_normalized_but_versions_remain_exact(
    tmp_path, monkeypatch, version, valid
):
    make_runtime(tmp_path)
    manifest = linux.load_manifest()
    packages = {
        name.lower().replace("-", "_"): value
        for name, value in manifest["required_packages"].items()
    }
    packages["llama_cpp_python"] = version
    monkeypatch.setattr(linux, "prove_interpreter", lambda *args: None)
    monkeypatch.setattr(
        linux, "run_python", lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(packages))
    )
    if valid:
        assert linux.validate_runtime(tmp_path, manifest) == packages
    else:
        with pytest.raises(linux.shared.RuntimePrepError, match="wrong bundled package version"):
            linux.validate_runtime(tmp_path, manifest)


@pytest.mark.parametrize("missing", linux.STDLIB)
def test_each_missing_stdlib_is_rejected_before_python_spawn(tmp_path, monkeypatch, missing):
    make_runtime(tmp_path)
    (tmp_path / "lib/python3.11" / missing).unlink()
    monkeypatch.setattr(linux.shared, "run", lambda *a, **kw: pytest.fail("must not spawn Python"))
    with pytest.raises(linux.shared.RuntimePrepError, match="standard library"):
        linux.validate_runtime(tmp_path, linux.load_manifest())


def test_content_mutation_changes_integrity_manifest(tmp_path):
    make_runtime(tmp_path)
    before = linux.file_hashes(tmp_path)
    (tmp_path / "lib/python3.11/os.py").write_text("# corrupt\n")
    assert linux.file_hashes(tmp_path) != before


def test_corrupt_package_rejected_before_execution(tmp_path, monkeypatch):
    runtime = tmp_path / "usr/lib/token-place-desktop-tauri/python-runtime"
    make_runtime(runtime)
    m = linux.load_manifest()
    data = {
        "runtime_id": linux.RUNTIME_ID,
        "expected_backend": "cpu",
        "build_profile": linux.PROFILE,
        "source_archive_sha256": m["sha256"],
        "target_triple": m["target_triple"],
        "files": linux.file_hashes(runtime),
    }
    (runtime / linux.shared.PROVENANCE).write_text(json.dumps(data))
    (runtime / "lib/python3.11/os.py").write_text("# corrupt\n")
    monkeypatch.setattr(
        linux.shared, "run", lambda *a, **kw: pytest.fail("must not execute damaged package")
    )
    with pytest.raises(RuntimeError, match="integrity mismatch"):
        validator.validate_tree(tmp_path, "0.1.25", "123456789abc")


def test_preparation_refuses_other_host_architectures(monkeypatch, tmp_path):
    monkeypatch.setattr(linux.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linux.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(
        linux.shared, "download_verified", lambda *a: pytest.fail("must not download")
    )
    with pytest.raises(linux.shared.RuntimePrepError, match="x86_64 host"):
        linux.prepare(tmp_path)


def test_linux_release_and_pr_ci_share_runtime_and_validator():
    release = yaml.safe_load((ROOT / ".github/workflows/desktop-release.yml").read_text())
    matrix = release["jobs"]["build"]["strategy"]["matrix"]["include"]
    linux_entries = [entry for entry in matrix if entry["tauri_target"].endswith("linux-gnu")]
    assert linux_entries == [
        {
            "os": "ubuntu-22.04",
            "artifact_name": "linux-x64-bundles",
            "tauri_target": "x86_64-unknown-linux-gnu",
            "checksum_file": "SHA256SUMS-linux-x64.txt",
        }
    ]
    for filename in ("desktop-release.yml", "desktop-linux-package.yml"):
        workflow = (ROOT / ".github/workflows" / filename).read_text()
        assert "python scripts/prepare_linux_embedded_python_runtime.py" in workflow
        assert "python scripts/validate_linux_desktop_package.py" in workflow
        assert "--bundles deb" in workflow


def test_linux_cpu_policy_preserves_existing_gpu_platform_plans():
    # Linux packaging must not change Windows CUDA or Apple Silicon Metal policy.
    sys.path.insert(0, str(ROOT / "desktop-tauri/src-tauri/python"))
    from desktop_gpu_packaging import llama_cpp_install_plan

    requirements = ROOT / "requirements.txt"
    assert llama_cpp_install_plan(platform="linux", requirements_path=requirements).backend == "cpu"
    assert (
        llama_cpp_install_plan(platform="win32", requirements_path=requirements).backend == "cuda"
    )
    assert (
        llama_cpp_install_plan(platform="darwin", requirements_path=requirements).backend == "metal"
    )
