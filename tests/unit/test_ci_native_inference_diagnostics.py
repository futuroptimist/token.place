"""CI metadata stays bounded without native inference initialization."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "ci_native_diagnostics",
    ROOT / "scripts/ci_native_inference_diagnostics.py",
)
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


def test_cpu_metadata_omits_unapproved_fields_and_other_processors():
    result = diag.cpu_metadata(
        "model name : fixture\nflags : "
        + "a" * 5000
        + "\nsecret : private\n\nmodel name : other\n"
    )
    assert result == {"model name": "fixture", "flags": "a" * 4096}


def test_native_metadata_hashes_bytes_without_loading_library(
    monkeypatch, tmp_path
):
    library = tmp_path / "libfixture.so"
    library.write_bytes(b"not an executable library")

    class Distribution:
        version = "1.2.3"
        files = [
            Path("llama_cpp/lib/libfixture.so"),
            Path("secret.so"),
            Path("llama_cpp/private.txt"),
        ]

        def read_text(self, name):
            assert name == "WHEEL"
            return "Tag: cp312-linux_x86_64\nSecret: omit-me\n"

        def locate_file(self, item):
            return library

    monkeypatch.setattr(
        diag.metadata, "distribution", lambda name: Distribution()
    )
    result = diag.package_metadata("llama-cpp-python")
    assert result["wheel_tags"] == ["cp312-linux_x86_64"]
    assert result["native_files"] == [
        {
            "name": "libfixture.so",
            "size": 25,
            "sha256": hashlib.sha256(library.read_bytes()).hexdigest(),
        }
    ]
    assert str(tmp_path) not in json.dumps(
        result
    ) and "omit-me" not in json.dumps(result)


def test_missing_private_target_is_reported_without_fallback(monkeypatch):
    monkeypatch.setattr(diag.metadata, "distributions", lambda **kwargs: [])
    assert diag.package_metadata("llama-cpp-python", "missing") == {
        "name": "llama-cpp-python",
        "available": False,
    }


def test_collector_refuses_non_hosted_execution(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(SystemExit, match="GitHub-hosted"):
        diag.main()


def test_ci_keeps_inference_exit_status_and_bounds_retained_output():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    section = workflow.split("      - name: Verify tiny real GGUF loads", 1)[
        1
    ].split("      - name: Verify encrypted", 1)[0]
    assert "PYTHONFAULTHANDLER: '1'" in section
    assert (
        "python -m pytest -q tests/e2e/test_ui.py -k "
        "'landing_chat_real_inference_with_desktop_bridge_api_v1'" in section
    )
    assert section.index("result=$?") < section.index('exit "$result"')
    assert (
        "tail -c 65536" in section
        and "rm .ci-native-diagnostics/inference-full.log" in section
    )
    assert "if: always()" in section and "continue-on-error" not in section


@pytest.mark.skipif(
    __import__("os").name == "nt", reason="Hosted Linux shell contract"
)
@pytest.mark.parametrize("test_status", [0, 23])
def test_ci_shell_preserves_test_status_when_metadata_fails(
    tmp_path, test_status
):
    import subprocess
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = next(
        item
        for item in workflow["jobs"]["test"]["steps"]
        if item.get("name", "").startswith("Verify tiny real GGUF loads")
    )
    stub = f"""python() {{
      if [ "$1" = scripts/ci_native_inference_diagnostics.py ]; then
        return 9
      fi
      printf '%070000d' 1
      return {test_status}
    }}
"""
    result = subprocess.run(
        ["bash", "-c", stub + step["run"]], cwd=tmp_path, capture_output=True
    )
    assert result.returncode == test_status
    assert (
        tmp_path / ".ci-native-diagnostics/inference-tail.log"
    ).stat().st_size == 65536
    assert not (
        tmp_path / ".ci-native-diagnostics/inference-full.log"
    ).exists()


def worker_diagnostic_prefix():
    import ast

    source = (ROOT / "utils/llm/model_manager.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    worker = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            getattr(t, "id", "") == "_LLAMA_CPP_RUNTIME_WORKER_CODE"
            for t in node.targets
        )
    )
    return worker.split("_active_command_id = None", 1)[0]


def test_worker_mapping_snapshot_is_bounded_and_excludes_unrelated_paths():
    namespace = {}
    exec(worker_diagnostic_prefix(), namespace)
    parse = namespace["_ci_native_mappings"]
    mapping = "100-200 r-xp 0 08:01 12 /opt/lib/llama_cpp/lib/libllama.so"
    unrelated = "200-300 r-xp 0 08:01 13 /private/secret.so"
    records = parse(mapping + "\n" + mapping + "\n" + unrelated)
    assert records == [
        {
            "path": "/opt/lib/llama_cpp/lib/libllama.so",
            "device": "08:01",
            "inode": "12",
        }
    ]
    assert "100-200" not in json.dumps(records)
    assert (
        len(
            parse(
                "\n".join(
                    f"0-1 r-xp 0 08:01 {n} /opt/llama_cpp/lib/libggml-{n}.so"
                    for n in range(40)
                )
            )
        )
        == 32
    )


@pytest.mark.parametrize(
    "missing", ["platform", "actions", "runner", "directory"]
)
def test_worker_diagnostics_require_every_gate(monkeypatch, tmp_path, missing):
    import sys

    monkeypatch.setattr(
        sys, "platform", "win32" if missing == "platform" else "linux"
    )
    monkeypatch.setenv(
        "GITHUB_ACTIONS", "false" if missing == "actions" else "true"
    )
    monkeypatch.setenv(
        "RUNNER_ENVIRONMENT",
        "self-hosted" if missing == "runner" else "github-hosted",
    )
    monkeypatch.setenv(
        "TOKEN_PLACE_CI_NATIVE_DIAGNOSTICS",
        "" if missing == "directory" else str(tmp_path),
    )
    namespace = {}
    exec(worker_diagnostic_prefix(), namespace)
    assert namespace["_ci_fault_stream"] is None
    assert list(tmp_path.iterdir()) == []


def test_worker_diagnostic_io_failure_does_not_block_worker(
    monkeypatch, tmp_path
):
    import sys

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    monkeypatch.setenv(
        "TOKEN_PLACE_CI_NATIVE_DIAGNOSTICS", str(tmp_path / "missing")
    )
    namespace = {}
    exec(worker_diagnostic_prefix(), namespace)
    namespace["_ci_record_native_mappings"]()
    assert namespace["_ci_diagnostic_directory"] is None


def test_worker_artifact_retention_is_bounded(tmp_path):
    for n in range(20):
        (tmp_path / f"worker-{n}.fault").write_bytes(b"x" * 70000)
    (tmp_path / "worker-0.maps.json").write_text("{}")
    diag.bound_worker_output(tmp_path)
    assert (tmp_path / "worker-0.maps.json.tail").read_text() == "{}"
    tails = list(tmp_path.glob("*.fault.tail"))
    assert len(tails) == 8
    assert all(path.stat().st_size == 65536 for path in tails)


@pytest.mark.skipif(
    __import__("sys").platform != "linux",
    reason="Linux worker diagnostic contract",
)
def test_dedicated_fault_descriptor_survives_stderr_redirection(tmp_path):
    import os
    import subprocess
    import sys

    env = os.environ.copy()
    env.update(
        GITHUB_ACTIONS="true",
        RUNNER_ENVIRONMENT="github-hosted",
        TOKEN_PLACE_CI_NATIVE_DIAGNOSTICS=str(tmp_path),
    )
    # Synthetic child only: no model/native library and no fatal signal.
    code = (
        worker_diagnostic_prefix()
        + "\n"
        + "\n".join(
            [
                "assert _ci_fault_stream is not None",
                "with open(os.devnull, 'w') as sink:",
                "    os.dup2(sink.fileno(), 2)",
                "faulthandler.dump_traceback(file=_ci_fault_stream)",
                "print('protocol-unchanged')",
            ]
        )
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout == "protocol-unchanged\n" and result.stderr == ""
    assert "<module>" in next(tmp_path.glob("*.fault")).read_text()


def test_worker_enables_fault_handler_on_retained_dedicated_stream(
    monkeypatch, tmp_path
):
    import faulthandler
    import sys

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    monkeypatch.setenv("TOKEN_PLACE_CI_NATIVE_DIAGNOSTICS", str(tmp_path))
    calls = []
    monkeypatch.setattr(
        faulthandler, "enable", lambda **kwargs: calls.append(kwargs)
    )
    namespace = {}
    exec(worker_diagnostic_prefix(), namespace)
    stream = namespace["_ci_fault_stream"]
    try:
        assert calls == [{"file": stream, "all_threads": True}]
        assert not stream.closed
        assert stream.fileno() > 2
    finally:
        stream.close()
