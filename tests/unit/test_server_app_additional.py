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
