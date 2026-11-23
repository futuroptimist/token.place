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
    config = json.loads((ROOT / "desktop-tauri/src-tauri/tauri.conf.json").read_text())
    assert "libgomp1" in config["bundle"]["linux"]["deb"]["depends"]
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


def test_declared_notices_are_materialized_hashed_and_required(tmp_path):
    manifest = linux.load_manifest()
    linux.write_runtime_notices(tmp_path, manifest)
    linux.validate_runtime_notices(tmp_path, manifest)
    inventory = linux.file_hashes(tmp_path)
    for notice in manifest["runtime_notices"]:
        assert notice["path"] in inventory
        path = tmp_path / notice["path"]
        original = path.read_text()
        path.unlink()
        with pytest.raises(linux.shared.RuntimePrepError, match="runtime notice"):
            linux.validate_runtime_notices(tmp_path, manifest)
        path.write_text(original)
    first = tmp_path / manifest["runtime_notices"][0]["path"]
    first.write_text("incomplete")
    with pytest.raises(linux.shared.RuntimePrepError, match="runtime notice"):
        linux.validate_runtime_notices(tmp_path, manifest)


@pytest.mark.parametrize("failure", [None, "validation", "swap", "double", "backup"])
def test_preparation_preserves_previous_runtime_on_failure(tmp_path, monkeypatch, failure):
    output = tmp_path / "python-runtime"
    output.mkdir()
    (output / "previous").write_text("healthy")
    monkeypatch.setattr(linux.shared, "OUTPUT", output)
    monkeypatch.setattr(linux.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linux.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(linux.shared, "download_verified", lambda *a: tmp_path / "archive")

    def extract(archive, manifest, temporary, **kwargs):
        root = temporary / "python"
        make_runtime(root)
        return root

    monkeypatch.setattr(linux.shared, "extract_archive", extract)
    monkeypatch.setattr(linux, "prove_interpreter", lambda *a: None)
    calls = []
    monkeypatch.setattr(linux, "run_python", lambda *a, **kw: calls.append((a, kw)))

    def validate(root, manifest):
        if failure == "validation":
            raise linux.shared.RuntimePrepError("invalid prepared runtime")
        return manifest["required_packages"]

    monkeypatch.setattr(linux, "validate_runtime", validate)
    rename = Path.rename

    def checked_rename(source, target):
        if failure in ("swap", "double") and source.name == "python" and target == output:
            raise OSError("simulated swap failure")
        if failure == "backup" and source == output:
            raise OSError("simulated backup rename failure")
        if failure == "double" and source.name == "old-runtime":
            raise OSError("simulated restore failure")
        return rename(source, target)

    monkeypatch.setattr(Path, "rename", checked_rename)
    if failure:
        with pytest.raises((OSError, linux.shared.RuntimePrepError)) as error:
            linux.prepare(tmp_path)
        if failure == "double":
            backups = list(tmp_path.glob("linux-runtime-backup-*/old-runtime/previous"))
            assert len(backups) == 1
            assert backups[0].read_text() == "healthy"
            assert str(backups[0].parent) in str(error.value)
        else:
            assert (output / "previous").read_text() == "healthy"
    else:
        linux.prepare(tmp_path)
        assert not (output / "previous").exists()
        data = json.loads((output / linux.shared.PROVENANCE).read_text())
        assert data["files"] == linux.file_hashes(output)
        linux.validate_runtime_notices(output, linux.load_manifest())
        args, kwargs = calls[0]
        assert "--no-binary=llama-cpp-python" in args[1]
        assert "-DGGML_NATIVE=OFF" in kwargs["env"]["CMAKE_ARGS"]
    assert not [
        p
        for p in tmp_path.glob("linux-runtime-*")
        if not p.name.startswith("linux-runtime-backup-")
    ]
    if failure != "double":
        assert not list(tmp_path.glob("linux-runtime-backup-*"))


@pytest.mark.parametrize(
    "field,value",
    [
        (None, None),
        ("version", "3.12.0"),
        ("machine", "arm64"),
        ("prefix", "outside"),
        ("executable", "outside"),
    ],
)
def test_interpreter_identity_and_containment(tmp_path, monkeypatch, field, value):
    root = tmp_path / "runtime"
    data = dict(
        version="3.11.13", machine="x86_64", prefix=str(root), executable=str(root / "bin/python3")
    )
    if field:
        data[field] = str(tmp_path / "outside") if value == "outside" else value
    monkeypatch.setattr(
        linux, "run_python", lambda *a, **kw: SimpleNamespace(stdout=json.dumps(data))
    )
    if field:
        with pytest.raises(linux.shared.RuntimePrepError):
            linux.prove_interpreter(root / "bin/python3", root, linux.load_manifest())
    else:
        linux.prove_interpreter(root / "bin/python3", root, linux.load_manifest())


@pytest.mark.parametrize(
    "invalid", [None, "provenance", "identity", "binary_count", "runtime_count", "notice"]
)
def test_package_requires_complete_matching_identity(tmp_path, monkeypatch, invalid):
    runtime = tmp_path / "usr/lib/token-place-desktop-tauri/python-runtime"
    make_runtime(runtime)
    manifest = linux.load_manifest()
    linux.write_runtime_notices(runtime, manifest)
    if invalid == "notice":
        (runtime / manifest["runtime_notices"][0]["path"]).unlink()
    data = dict(
        runtime_id=linux.RUNTIME_ID,
        expected_backend="cpu",
        build_profile=linux.PROFILE,
        source_archive_sha256=manifest["sha256"],
        target_triple=manifest["target_triple"],
        files=linux.file_hashes(runtime),
    )
    if invalid == "provenance":
        data["expected_backend"] = "cuda"
    (runtime / linux.shared.PROVENANCE).write_text(json.dumps(data))
    binary = tmp_path / "usr/bin/desktop"
    binary.parent.mkdir(parents=True)
    binary.write_text("synthetic")
    if invalid == "binary_count":
        (binary.parent / "unexpected").write_text("extra")
    if invalid == "runtime_count":
        (tmp_path / "usr/lib/other/python-runtime").mkdir(parents=True)
    identity = dict(
        app_version="0.1.25",
        build_id="123456789abc",
        target_triple=manifest["target_triple"],
        bundled_runtime_id=linux.RUNTIME_ID,
    )
    if invalid == "identity":
        identity["app_version"] = "0.1.24"
    monkeypatch.setattr(linux, "validate_runtime", lambda *a: None)
    monkeypatch.setattr(
        linux.shared, "run", lambda *a, **kw: SimpleNamespace(stdout=json.dumps(identity))
    )
    if invalid:
        with pytest.raises((RuntimeError, linux.shared.RuntimePrepError)):
            validator.validate_tree(tmp_path, "0.1.25", "123456789abc")
    else:
        validator.validate_tree(tmp_path, "0.1.25", "123456789abc")


def test_package_cli_extracts_without_installing(tmp_path, monkeypatch):
    package = tmp_path / "candidate.deb"
    package.write_text("synthetic archive")
    extracted = []
    validated = []
    checked = []
    monkeypatch.setattr(validator, "validate_dependencies", lambda path: checked.append(path))
    monkeypatch.setattr(
        validator.subprocess, "run", lambda args, **kw: extracted.append((args, kw))
    )
    monkeypatch.setattr(validator, "validate_tree", lambda *args: validated.append(args))
    validator.main(
        [
            "--deb",
            str(package),
            "--expected-version",
            "0.1.25",
            "--expected-build-id",
            "123456789abc",
        ]
    )
    assert checked == [package]
    args, kwargs = extracted[0]
    assert args[:3] == ["dpkg-deb", "--extract", str(package.resolve())]
    assert kwargs == {"check": True}
    assert validated == [(Path(args[3]), "0.1.25", "123456789abc")]
    assert not Path(args[3]).exists()


@pytest.mark.parametrize(
    "kind", ["file", "directory", "broken", "external", "external-broken", "cycle"]
)
def test_symlink_inventory_detects_targets_and_rejects_invalid_links(tmp_path, kind):
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "a").mkdir()
    (root / "b").mkdir()
    (root / "a/file").write_text("same bytes")
    (root / "b/file").write_text("same bytes")
    link = root / "link"
    targets = {
        "file": "a/file",
        "directory": "a",
        "broken": "missing",
        "external": str(tmp_path),
        "external-broken": str(tmp_path / "missing"),
        "cycle": "link",
    }
    try:
        link.symlink_to(targets[kind], target_is_directory=kind in ("directory", "external"))
    except OSError as exc:
        if sys.platform == "win32" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows host does not grant symlink creation; exercised on Linux CI")
        raise
    if kind in ("file", "directory"):
        before = linux.file_hashes(root)
        assert before["link"] == {"symlink": targets[kind]}
        link.unlink()
        link.symlink_to(
            "b/file" if kind == "file" else "b", target_is_directory=kind == "directory"
        )
        assert linux.file_hashes(root) != before
        if kind == "file":
            linux.materialize_runtime_file_links(root)
            assert not link.is_symlink()
            assert link.read_text() == "same bytes"
            assert linux.file_hashes(root)["link"] == linux.file_hashes(root)["b/file"]
        else:
            with pytest.raises(linux.shared.RuntimePrepError, match="directory runtime link"):
                linux.materialize_runtime_file_links(root)
            assert link.is_symlink()
    else:
        with pytest.raises(linux.shared.RuntimePrepError, match="runtime link"):
            linux.file_hashes(root)


@pytest.mark.parametrize("missing", [None, "libgomp1", "libgtk-3-0", "libwebkit2gtk-4.1-0"])
def test_debian_control_requires_all_runtime_libraries(tmp_path, monkeypatch, missing):
    names = validator.REQUIRED_DEB_DEPENDENCIES - ({missing} if missing else set())
    fields = [f"{name} (>= 1.0)" for name in sorted(names)]
    if missing:
        fields.append(f"{missing} | unrelated-alternative")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=", ".join(fields))

    monkeypatch.setattr(validator.subprocess, "run", run)
    deb = tmp_path / "candidate.deb"
    if missing:
        with pytest.raises(RuntimeError, match=missing):
            validator.validate_dependencies(deb)
    else:
        validator.validate_dependencies(deb)
    assert calls == [["dpkg-deb", "--field", str(deb.resolve()), "Depends"]]
