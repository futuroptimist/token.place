import server.server_app as sa
from unittest.mock import MagicMock, patch


def test_health_route():
    mm = MagicMock()
    mm.use_mock_llm = True
    mm.download_model_if_needed.return_value = True
    with patch('server.server_app.get_model_manager', return_value=mm), \
         patch('server.server_app.RelayClient'):
        app = sa.ServerApp()
        client = app.app.test_client()
        res = client.get('/health')
        assert res.status_code == 200
        data = res.get_json()
        assert data['mock_mode'] is True


def test_root_route():
    """Verify that the root route returns a simple status."""
    mm = MagicMock()
    mm.use_mock_llm = True
    mm.download_model_if_needed.return_value = True
    with patch('server.server_app.get_model_manager', return_value=mm), \
         patch('server.server_app.RelayClient'):
        app = sa.ServerApp()
        client = app.app.test_client()
        res = client.get('/')
        assert res.status_code == 200
        data = res.get_json()
        assert data['status'] == 'ok'


def test_run_method(monkeypatch):
    """Ensure the run method starts polling and launches Flask."""
    mm = MagicMock()
    mm.use_mock_llm = True
    with patch('server.server_app.get_model_manager', return_value=mm), \
         patch('server.server_app.RelayClient'):
        app = sa.ServerApp()

        poller = MagicMock()
        monkeypatch.setattr(app, 'start_relay_polling', poller)
        flask_runner = MagicMock()
        monkeypatch.setattr(app.app, 'run', flask_runner)

        app.run()

        poller.assert_called_once()
        flask_runner.assert_called_once_with(
            host='0.0.0.0',
            port=app.server_port,
            debug=not sa.config.is_production,
            use_reloader=False,
        )
