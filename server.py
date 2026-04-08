#!/usr/bin/env python3
"""
token.place LLM server.
Main server application module that integrates all components.
"""
import os
import argparse
import logging
import sys
from flask import Flask, request, jsonify
from typing import Optional

# Enable mock LLM mode early if flag is present
if "--use_mock_llm" in sys.argv:
    os.environ["USE_MOCK_LLM"] = "1"

# Import our refactored modules
from utils.llm.model_manager import get_model_manager
from utils.compute_node_runtime import (
    ComputeNodeRuntime,
    ComputeNodeRuntimeConfig,
    first_env,
    format_relay_target,
    resolve_relay_port,
    resolve_relay_url,
)
from utils.networking.relay_client import RelayClient

# Import config
from config import get_config

# Get configuration instance
config = get_config()

# Configure logging
logging.basicConfig(level=logging.INFO if not config.is_production else logging.ERROR,
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('server_app')

def log_info(message):
    """Log info only in non-production environments"""
    if not config.is_production:
        logger.info(message)

def log_error(message, exc_info=False):
    """Log errors only in non-production environments"""
    if not config.is_production:
        logger.error(message, exc_info=exc_info)


def _first_env(keys):
    """Backward-compatible wrapper for runtime env helper."""

    return first_env(keys)


def _resolve_relay_url(cli_default: str) -> str:
    """Backward-compatible wrapper for runtime relay URL resolution."""

    return resolve_relay_url(cli_default)


def _resolve_relay_port(cli_default: Optional[int], relay_url: str) -> Optional[int]:
    """Backward-compatible wrapper for runtime relay port resolution."""

    return resolve_relay_port(cli_default, relay_url)


def _format_relay_target(relay_url: str, relay_port: Optional[int]) -> str:
    """Backward-compatible wrapper for runtime relay target formatting."""

    return format_relay_target(relay_url, relay_port)


class ServerApp:
    """
    Main server application that integrates all components.
    """
    def __init__(
        self,
        server_port: int = 3000,
        relay_port: Optional[int] = None,
        relay_url: str = "https://token.place",
        server_host: str = "127.0.0.1",
    ):
        """
        Initialize the server application.

        Args:
            server_port: Port to run the server on
            relay_port: Port the relay server is running on
            relay_url: URL of the relay server
        """
        self.server_port = server_port
        self.server_host = server_host
        self.relay_port = relay_port
        self.relay_url = relay_url

        # Create Flask app
        self.app = Flask(__name__)

        # Set up endpoints
        self.setup_routes()

        self.runtime = ComputeNodeRuntime(
            ComputeNodeRuntimeConfig(relay_url=relay_url, relay_port=relay_port)
        )

        # Preserve legacy attribute for callers/tests expecting relay_client on ServerApp.
        self.relay_client = self.runtime.relay_client

        # Initialize LLM by downloading model if needed
        self.initialize_llm()

    def initialize_llm(self):
        """Initialize the LLM by downloading the model if needed."""
        self.runtime.ensure_model_ready()

    def setup_routes(self):
        """Set up Flask routes for the server."""
        # Root endpoint
        @self.app.route('/')
        def index():
            return jsonify({
                'status': 'ok',
                'message': 'token.place server is running'
            })

        # Health check endpoint
        @self.app.route('/health')
        def health():
            return jsonify({
                'status': 'ok',
                'version': config.get('version', 'dev'),
                'mock_mode': get_model_manager().use_mock_llm
            })

        # Endpoints for direct API access (if needed)
        # These endpoints might be unused if all communication goes through the relay

    def start_relay_polling(self):
        """Start polling the relay in a background thread."""
        self.runtime.start_relay_polling()

    def run(self):
        """Run the server application."""
        log_info(f"Starting server on port {self.server_port}")

        # Start relay polling in a background thread
        self.start_relay_polling()

        # Run the Flask app
        self.app.run(
            host=self.server_host,
            port=self.server_port,
            debug=not config.is_production,
            use_reloader=False  # Disable reloader to avoid duplicate threads
        )

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="token.place server")
    parser.add_argument("--server_port", type=int, default=3000, help="Port to run the server on")
    parser.add_argument("--server_host", default="127.0.0.1", help="Host interface to bind the server")
    parser.add_argument(
        "--relay_port",
        type=int,
        default=None,
        help="Port the relay server is running on",
    )
    parser.add_argument(
        "--relay_url",
        type=str,
        default=config.get("relay.server_url", "https://token.place"),
        help="URL of the relay server",
    )
    parser.add_argument("--use_mock_llm", action="store_true", help="Use mock LLM for testing")
    return parser.parse_args()

def main():
    """Main entry point for the server application."""
    args = parse_args()

    # Set USE_MOCK_LLM environment variable if flag is set
    if args.use_mock_llm:
        os.environ['USE_MOCK_LLM'] = '1'
        print("Running in mock LLM mode")

    relay_url = _resolve_relay_url(args.relay_url)
    relay_port = _resolve_relay_port(args.relay_port, relay_url)

    # Create and run the server
    server = ServerApp(
        server_port=args.server_port,
        server_host=args.server_host,
        relay_port=relay_port,
        relay_url=relay_url
    )
    server.run()

if __name__ == "__main__":  # pragma: no cover
    main()
