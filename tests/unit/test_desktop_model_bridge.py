"""Unit tests for desktop model bridge helpers."""

from unittest.mock import MagicMock, patch

from utils.llm import desktop_model_bridge


def test_metadata_action_prints_json(capsys):
    manager = MagicMock()
    manager.artifact_metadata.return_value = {
        "canonical_family_url": "https://huggingface.co/meta-llama/Meta-Llama-3-8B",
        "artifact_file_name": "model.gguf",
        "artifact_url": "https://example.com/model.gguf",
        "model_path": "/tmp/model.gguf",
        "models_dir": "/tmp",
        "is_downloaded": True,
    }

    with patch("utils.llm.desktop_model_bridge.get_model_manager", return_value=manager):
        rc = desktop_model_bridge.main(["metadata"])

    assert rc == 0
    out = capsys.readouterr().out
    assert '"artifact_file_name": "model.gguf"' in out


def test_download_action_raises_actionable_error():
    manager = MagicMock()
    manager.url = "https://example.com/model.gguf"
    manager.download_model_if_needed.return_value = False

    with patch("utils.llm.desktop_model_bridge.get_model_manager", return_value=manager):
        try:
            desktop_model_bridge.main(["download"])
        except RuntimeError as exc:
            assert "Unable to download" in str(exc)
            assert manager.url in str(exc)
        else:
            raise AssertionError("expected RuntimeError")
