"""
Main server application module that integrates all components.
"""
import os
import threading
import argparse
import logging
from urllib.parse import urlparse
from flask import Flask, request, jsonify
from typing import Dict, Any, List, Optional

# Import our refactored modules
from utils.llm.model_manager import get_model_manager
from utils.crypto.crypto_manager import get_crypto_manager
from utils.networking.relay_client import RelayClient
from utils.system import collect_resource_usage

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


def _default_relay_url() -> str:
    """Resolve the relay base URL from environment overrides or defaults."""

    env_candidates = [
        os.environ.get("TOKENPLACE_RELAY_URL"),
        os.environ.get("RELAY_URL"),
    ]
    for candidate in env_candidates:
        if candidate and candidate.strip():
            return candidate.strip()
    return "http://localhost"


def _default_relay_port() -> int:
    """Resolve the relay port, preferring environment hints and URL metadata."""

    env_candidates = [
        os.environ.get("TOKENPLACE_RELAY_PORT"),
        os.environ.get("RELAY_PORT"),
    ]
    for candidate in env_candidates:
        if candidate:
            try:
                return int(candidate)
            except (TypeError, ValueError):
                continue

    parsed = urlparse(os.environ.get("TOKENPLACE_RELAY_URL") or os.environ.get("RELAY_URL") or "")
    if parsed.port:
        return parsed.port

    return 5000

class ServerApp:
    """
    Main server application that integrates all components.
    """
    def __init__(
        self,
        server_port: int = 3000,
        relay_port: Optional[int] = None,
        relay_url: Optional[str] = None,
    ):
        """
        Initialize the server application.

        Args:
            server_port: Port to run the server on
            relay_port: Port the relay server is running on
            relay_url: URL of the relay server
        """
        self.server_port = server_port
        self.relay_port = relay_port if relay_port is not None else _default_relay_port()
        self.relay_url = relay_url or _default_relay_url()
        self.server_host = config.get('server.host', '127.0.0.1')

        # Create Flask app
        self.app = Flask(__name__)

        # Set up endpoints
        self.setup_routes()

        # Create relay client
        self.relay_client = RelayClient(
            base_url=relay_url,
            port=relay_port,
            crypto_manager=get_crypto_manager(),
            model_manager=get_model_manager()
        )

        # Initialize LLM by downloading model if needed
        self.initialize_llm()

    def initialize_llm(self):
        """Initialize the LLM by downloading the model if needed."""
        log_info("Initializing LLM...")
        model_mgr = get_model_manager()
        if model_mgr.use_mock_llm:
            log_info("Using mock LLM based on configuration")
        else:
            # Download model if needed
            if model_mgr.download_model_if_needed():
                log_info("Model ready for inference")
            else:
                log_error("Failed to download or verify model")

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

        @self.app.route('/metrics/resource')
        def resource_metrics():
            """Expose basic CPU and memory usage metrics for cross-platform monitoring."""
            usage = collect_resource_usage()
            return jsonify(usage)

        # Endpoints for direct API access (if needed)
        # These endpoints might be unused if all communication goes through the relay

    def start_relay_polling(self):  # pragma: no cover
        """Start polling the relay in a background thread."""
        relay_thread = threading.Thread(
            target=self.relay_client.poll_relay_continuously,
            daemon=True
        )
        relay_thread.start()
        log_info(f"Started relay polling thread for {self.relay_url}:{self.relay_port}")

    def run(self):  # pragma: no cover
        """Run the server application."""
        log_info(f"Starting server on {self.server_host}:{self.server_port}")

        # Start relay polling in a background thread
        self.start_relay_polling()

        # Run the Flask app. Bind to localhost by default to avoid exposing the
        # service unintentionally; allow override via configuration.
        host = config.get('server.host', '127.0.0.1')
        self.app.run(
            host=self.server_host,
            port=self.server_port,
            debug=not config.is_production,
            use_reloader=False  # Disable reloader to avoid duplicate threads
        )

def parse_args():  # pragma: no cover
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="token.place server")
    parser.add_argument("--server_port", type=int, default=3000, help="Port to run the server on")
    parser.add_argument(
        "--relay_port",
        type=int,
        default=_default_relay_port(),
        help="Port the relay server is running on",
    )
    parser.add_argument(
        "--relay_url",
        type=str,
        default=_default_relay_url(),
        help="URL of the relay server",
    )
    parser.add_argument("--use_mock_llm", action="store_true", help="Use mock LLM for testing")
    return parser.parse_args()

def main():  # pragma: no cover
    """Main entry point for the server application."""
    args = parse_args()

    # Set USE_MOCK_LLM environment variable if flag is set
    if args.use_mock_llm:
        os.environ['USE_MOCK_LLM'] = '1'
        print("Running in mock LLM mode")

    # Create and run the server
    server = ServerApp(
        server_port=args.server_port,
        relay_port=args.relay_port,
        relay_url=args.relay_url
    )
    server.run()

if __name__ == "__main__":  # pragma: no cover
    main()
