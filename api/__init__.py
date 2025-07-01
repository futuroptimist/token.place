"""
token.place API package
This package contains the API implementation for token.place.
"""

# Import API versions
import os
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from prometheus_flask_exporter import PrometheusMetrics
from api.v1 import routes as v1_routes

def init_app(app):
    """Initialize the API with the Flask app."""
    Limiter(
        get_remote_address,
        app=app,
        default_limits=[os.environ.get("API_RATE_LIMIT", "60/hour")],
    )
    PrometheusMetrics(app)
    app.register_blueprint(v1_routes.v1_bp)
    app.register_blueprint(v1_routes.openai_v1_bp)
