"""
token.place API package
This package contains the API implementation for token.place.
"""

# Import API versions
from api.v1 import routes as v1_routes

def init_app(app):
    """Initialize the API with the Flask app"""
    # Register the v1 blueprint directly
    app.register_blueprint(v1_routes.v1_bp) 