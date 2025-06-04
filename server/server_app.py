"""
Main server application module that integrates all components.
"""
import os
import threading
import argparse
import logging
from flask import Flask, request, jsonify
from typing import Dict, Any, List, Optional

# Import our refactored modules
from utils.llm.model_manager import model_manager
from utils.crypto.crypto_manager import crypto_manager
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

class ServerApp:
    """
    Main server application that integrates all components.
    """
    def __init__(self, server_port: int = 3000, relay_port: int = 5000, relay_url: str = "http://localhost"):
        """
        Initialize the server application.
        
        Args:
            server_port: Port to run the server on
            relay_port: Port the relay server is running on
            relay_url: URL of the relay server
        """
        self.server_port = server_port
        self.relay_port = relay_port
        self.relay_url = relay_url
        
        # Create Flask app
        self.app = Flask(__name__)
        
        # Set up endpoints
        self.setup_routes()
        
        # Create relay client
        self.relay_client = RelayClient(
            base_url=relay_url,
            port=relay_port,
            crypto_manager=crypto_manager,
            model_manager=model_manager
        )
        
        # Initialize LLM by downloading model if needed
        self.initialize_llm()
        
    def initialize_llm(self):
        """Initialize the LLM by downloading the model if needed."""
        log_info("Initializing LLM...")
        if model_manager.use_mock_llm:
            log_info("Using mock LLM based on configuration")
        else:
            # Download model if needed
            if model_manager.download_model_if_needed():
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
                'mock_mode': model_manager.use_mock_llm
            })
            
        # Endpoints for direct API access (if needed)
        # These endpoints might be unused if all communication goes through the relay
        
    def start_relay_polling(self):
        """Start polling the relay in a background thread."""
        relay_thread = threading.Thread(
            target=self.relay_client.poll_relay_continuously,
            daemon=True
        )
        relay_thread.start()
        log_info(f"Started relay polling thread for {self.relay_url}:{self.relay_port}")
        
    def run(self):
        """Run the server application."""
        log_info(f"Starting server on port {self.server_port}")
        
        # Start relay polling in a background thread
        self.start_relay_polling()
        
        # Run the Flask app
        self.app.run(
            host='0.0.0.0',
            port=self.server_port,
            debug=not config.is_production,
            use_reloader=False  # Disable reloader to avoid duplicate threads
        )

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="token.place server")
    parser.add_argument("--server_port", type=int, default=3000, help="Port to run the server on")
    parser.add_argument("--relay_port", type=int, default=5000, help="Port the relay server is running on")
    parser.add_argument("--relay_url", type=str, default="http://localhost", help="URL of the relay server")
    parser.add_argument("--use_mock_llm", action="store_true", help="Use mock LLM for testing")
    return parser.parse_args()

def main():
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

if __name__ == "__main__":
    main() 
