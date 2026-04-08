#!/usr/bin/env python3
"""
token.place LLM server.
Main server application module that integrates all components.
"""
import os
import argparse
import logging
import sys
from urllib.parse import urlparse
from flask import Flask, jsonify
from typing import List, Optional

# Enable mock LLM mode early if flag is present
if "--use_mock_llm" in sys.argv:
    os.environ["USE_MOCK_LLM"] = "1"

from config import get_config
from utils.networking.relay_client import RelayClient
from utils.runtime.compute_node import ComputeNodeRuntime, RuntimeConfig

# Get configuration instance
config = get_config()

# Configure logging
logging.basicConfig(
    level=logging.INFO if not config.is_production else logging.ERROR,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("server_app")


def log_info(message):
    """Log info only in non-production environments"""
    if not config.is_production:
        logger.info(message)


def log_error(message, exc_info=False):
    """Log errors only in non-production environments"""
    if not config.is_production:
        logger.error(message, exc_info=exc_info)


def _first_env(keys: List[str]) -> Optional[str]:
    """Return the first non-empty environment variable in ``keys``."""

    for key in keys:
        value = os.environ.get(key)
        if value:
            stripped = value.strip()
            if stripped:
                return stripped
    return None


def _resolve_relay_url(cli_default: str) -> str:
    """Resolve the relay base URL from CLI or env."""

    env_override = _first_env(
        [
            "TOKENPLACE_RELAY_URL",
            "TOKEN_PLACE_RELAY_URL",
            "TOKENPLACE_RELAY_BASE_URL",
            "TOKEN_PLACE_RELAY_BASE_URL",
            "TOKENPLACE_RELAY_UPSTREAM_URL",
            "TOKEN_PLACE_RELAY_UPSTREAM_URL",
            "RELAY_URL",
        ]
    )
    return env_override or cli_default


def _resolve_relay_port(cli_default: Optional[int], relay_url: str) -> Optional[int]:
    """Resolve the relay port from CLI, env, or the relay URL."""

    env_port = _first_env(["TOKENPLACE_RELAY_PORT", "TOKEN_PLACE_RELAY_PORT", "RELAY_PORT"])

    if env_port is not None:
        try:
            return int(env_port)
        except ValueError:
            log_error(f"Invalid relay port override: {env_port}")
            return cli_default

    parsed = urlparse(relay_url if "://" in relay_url else f"http://{relay_url}")
    if parsed.port:
        return parsed.port

    if cli_default is not None:
        return cli_default

    return None


class ServerApp:
    """HTTP shell around the shared compute-node runtime."""

    def __init__(
        self,
        server_port: int = 3000,
        relay_port: Optional[int] = None,
        relay_url: str = "https://token.place",
        server_host: str = "127.0.0.1",
    ):
        self.server_port = server_port
        self.server_host = server_host
        self.relay_port = relay_port
        self.relay_url = relay_url

        self.runtime = ComputeNodeRuntime(
            RuntimeConfig(
                relay_url=relay_url,
                relay_port=relay_port,
                server_host=server_host,
                server_port=server_port,
            ),
            is_production=config.is_production,
        )

        self.app = Flask(__name__)
        self.setup_routes()
        self.initialize_llm()

    @property
    def relay_client(self):
        """Backwards-compatible access for existing callers/tests."""

        return self.runtime.relay_client

    def initialize_llm(self):
        """Initialize the LLM by downloading the model if needed."""

        self.runtime.initialize_model_readiness()

    def setup_routes(self):
        """Set up Flask routes for the server."""

        @self.app.route("/")
        def index():
            return jsonify({"status": "ok", "message": "token.place server is running"})

        @self.app.route("/health")
        def health():
            return jsonify(
                {
                    "status": "ok",
                    "version": config.get("version", "dev"),
                    "mock_mode": self.runtime.model_manager.use_mock_llm,
                }
            )

    def start_relay_polling(self):
        """Start polling the relay in a background thread."""

        return self.runtime.start_relay_polling()

    def run(self):
        """Run the server application."""

        log_info(f"Starting server on port {self.server_port}")
        self.start_relay_polling()
        self.app.run(
            host=self.server_host,
            port=self.server_port,
            debug=not config.is_production,
            use_reloader=False,
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

    if args.use_mock_llm:
        os.environ["USE_MOCK_LLM"] = "1"
        print("Running in mock LLM mode")

    relay_url = _resolve_relay_url(args.relay_url)
    relay_port = _resolve_relay_port(args.relay_port, relay_url)

    server = ServerApp(
        server_port=args.server_port,
        server_host=args.server_host,
        relay_port=relay_port,
        relay_url=relay_url,
    )
    server.run()


if __name__ == "__main__":  # pragma: no cover
    main()
