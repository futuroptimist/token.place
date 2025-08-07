import os
from unittest.mock import patch

from flask import Flask

from api import init_app


@patch.dict(os.environ, {"API_RATE_LIMIT": "1/minute"})
def test_exceeding_rate_limit_returns_429():
    app = Flask(__name__)
    init_app(app)

    with app.test_client() as client:
        first = client.get("/api/v1/models")
        assert first.status_code == 200

        second = client.get("/api/v1/models")
        assert second.status_code == 429
