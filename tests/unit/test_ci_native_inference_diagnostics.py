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
        "'landing_chat_real_inference_with_desktop_bridge_api_v1'"
        in section
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
