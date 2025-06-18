from unittest.mock import MagicMock, patch

import server.server_app as sa


def test_parse_args_defaults(monkeypatch):
    import sys
    monkeypatch.setattr(sys, 'argv', ['server.py'])
    args = sa.parse_args()
    assert args.server_port == 3000
    assert args.relay_port == 5000
    assert args.relay_url == "http://localhost"
    assert args.use_mock_llm is False


def test_initialize_llm_mock():
    with patch('server.server_app.get_model_manager') as gm:
        gm.return_value.use_mock_llm = True
        app = sa.ServerApp()
        # initialize_llm called in __init__; ensure method executed
        gm.assert_called()


def test_initialize_llm_download():
    mm = MagicMock()
    mm.use_mock_llm = False
    mm.download_model_if_needed.return_value = True
    with patch('server.server_app.get_model_manager', return_value=mm):
        app = sa.ServerApp()
        mm.download_model_if_needed.assert_called_once()


def test_start_relay_polling():
    with patch('server.server_app.RelayClient') as rc:
        instance = rc.return_value
        instance.poll_relay_continuously = MagicMock()
        app = sa.ServerApp()
        with patch('threading.Thread') as th:
            thread = MagicMock()
            th.return_value = thread
            app.start_relay_polling()
            thread.start.assert_called_once()
