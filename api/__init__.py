"""token.place API package."""

import os
from typing import Optional, Union

from flask import jsonify
from flask_limiter import Limiter
from flask_limiter.errors import RateLimitExceeded
from flask_limiter.util import get_remote_address
from limits import RateLimitItem
from flask_limiter.wrappers import Limit
from prometheus_flask_exporter import PrometheusMetrics

from api.v1 import routes as v1_routes
from api.v2 import routes as v2_routes


RateLimitLike = Union[RateLimitItem, Limit]


def _retry_after_seconds(limit: Optional[RateLimitLike]) -> Optional[int]:
    """Return an approximate wait window for the provided rate limit."""

    if limit is None:
        return None

    limit_item = getattr(limit, "limit", limit)

    try:
        expiry = int(limit_item.get_expiry())
    except (AttributeError, TypeError, ValueError):
        return None

    return max(expiry, 1)


def init_app(app):
    """Initialize the API with the Flask app."""

    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=[
            os.environ.get("API_RATE_LIMIT", "60/hour"),
            os.environ.get("API_DAILY_QUOTA", "1000/day"),
        ],
    )

    @app.errorhandler(RateLimitExceeded)
    def handle_rate_limit(exc: RateLimitExceeded):
        """Ensure rate limit responses match the OpenAI-compatible error schema."""

        payload = {
            "error": {
                "message": f"Rate limit exceeded: {exc.description}",
                "type": "rate_limit_error",
                "code": "rate_limit_exceeded",
            }
        }
        response = jsonify(payload)
        response.status_code = 429

        retry_after = _retry_after_seconds(getattr(exc, "limit", None))
        if retry_after is not None:
            response.headers["Retry-After"] = str(retry_after)

        return response

    PrometheusMetrics(app)
    app.register_blueprint(v1_routes.v1_bp)
    app.register_blueprint(v1_routes.openai_v1_bp)
    app.register_blueprint(v2_routes.v2_bp)
    app.register_blueprint(v2_routes.openai_v2_bp)

    return limiter
