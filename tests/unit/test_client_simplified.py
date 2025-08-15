import builtins
import io
from unittest.mock import patch, MagicMock
import client_simplified as cs


def test_format_message_user_and_assistant():
    user_msg = {"role": "user", "content": "hi"}
    assistant_msg = {"role": "assistant", "content": "hello"}
    other_msg = {"role": "system", "content": "info"}
    assert "User:" in cs.format_message(user_msg)
    assert "Assistant:" in cs.format_message(assistant_msg)
    assert "System:" in cs.format_message(other_msg)


def test_no_unused_imports():
    """Ensure no leftover modules remain imported unintentionally."""
    for name in ["os", "subprocess", "time"]:
        assert name not in cs.__dict__, f"{name} should not be imported"


def test_main_single_message(monkeypatch, capsys):
    mock_client = MagicMock()
    mock_client.fetch_server_public_key.return_value = True
    mock_client.send_chat_message.return_value = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok"},
    ]

    with patch.object(cs, "CryptoClient", return_value=mock_client):
        monkeypatch.setattr(cs.sys, "argv", ["client_simplified.py", "--message", "hi"])
        cs.main()
    out = capsys.readouterr().out
    assert "Assistant: ok" in out
    mock_client.fetch_server_public_key.assert_called_once()
    mock_client.send_chat_message.assert_called_once()


def test_clear_screen_skips_when_not_tty(monkeypatch):
    class FakeStdout(io.StringIO):
        def isatty(self):
            return False

    fake = FakeStdout()
    monkeypatch.setattr(cs.sys, "stdout", fake)
    cs.clear_screen()
    assert fake.getvalue() == ""


def test_chat_loop_single_iteration(monkeypatch, capsys):
    mock_client = MagicMock()
    mock_client.fetch_server_public_key.return_value = True
    mock_client.send_chat_message.return_value = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok"},
    ]

    inputs = iter(["hi", "exit"])
    monkeypatch.setattr(builtins, "input", lambda _: next(inputs))
    monkeypatch.setattr(cs, "clear_screen", lambda: None)

    cs.chat_loop(mock_client)
    out = capsys.readouterr().out
    assert "Assistant is thinking" in out
    assert "ok" in out


def test_chat_loop_eof(monkeypatch, capsys):
    """Gracefully exit when stdin closes."""
    mock_client = MagicMock()
    mock_client.fetch_server_public_key.return_value = True

    def raise_eof(_):
        raise EOFError

    monkeypatch.setattr(builtins, "input", raise_eof)
    monkeypatch.setattr(cs, "clear_screen", lambda: None)

    cs.chat_loop(mock_client)

    out = capsys.readouterr().out
    assert "Chat session ended" in out
    mock_client.send_chat_message.assert_not_called()
