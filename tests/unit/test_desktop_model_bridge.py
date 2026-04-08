from types import SimpleNamespace

from utils.llm import desktop_model_bridge as bridge


class FakeManager(SimpleNamespace):
    def __init__(self, *, download_ok=True):
        super().__init__(
            file_name="foo.gguf",
            url="https://example.com/foo.gguf",
            model_path="/tmp/models/foo.gguf",
            models_dir="/tmp/models",
        )
        self._download_ok = download_ok

    def download_model_if_needed(self):
        return self._download_ok


def test_model_metadata(monkeypatch):
    monkeypatch.setattr(bridge, "get_model_manager", lambda: FakeManager())

    payload = bridge.model_metadata()

    assert payload["canonical_model_family_url"] == bridge.CANONICAL_MODEL_FAMILY_URL
    assert payload["artifact_filename"] == "foo.gguf"
    assert payload["artifact_url"] == "https://example.com/foo.gguf"
    assert payload["resolved_model_path"] == "/tmp/models/foo.gguf"


def test_ensure_downloaded_success(monkeypatch):
    monkeypatch.setattr(bridge, "get_model_manager", lambda: FakeManager())
    monkeypatch.setattr(bridge.os.path, "exists", lambda _: False)

    payload = bridge.ensure_downloaded()

    assert payload["status"] == "downloaded"
    assert payload["resolved_model_path"] == "/tmp/models/foo.gguf"


def test_ensure_downloaded_failure(monkeypatch):
    monkeypatch.setattr(bridge, "get_model_manager", lambda: FakeManager(download_ok=False))

    try:
        bridge.ensure_downloaded()
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "Failed to download model artifact" in str(exc)
