"""
Relay client module for managing communication with relay servers.
"""
import json
import logging
import requests
import time
import base64
from typing import Dict, Optional, Any, List, Union, Tuple

# Import config
from config import get_config

# Get configuration instance
config = get_config()

# Configure logging
logger = logging.getLogger('relay_client')

def log_info(message):
    """Log info only in non-production environments"""
    if not config.is_production:
        logger.info(message)

def log_error(message, exc_info=False):
    """Log errors only in non-production environments"""
    if not config.is_production:
        logger.error(message, exc_info=exc_info)

class RelayClient:
    """
    Client for communicating with relay servers.
    Handles registration, polling, sending and receiving encrypted messages.
    """
    def __init__(self, base_url: str, port: int, crypto_manager, model_manager):
        """
        Initialize the RelayClient.
        
        Args:
            base_url: The base URL of the relay server (e.g., 'http://localhost')
            port: The port number of the relay server
            crypto_manager: Instance of CryptoManager for encryption/decryption
            model_manager: Instance of ModelManager for LLM interaction
        """
        self.base_url = base_url
        self.port = port
        self.crypto_manager = crypto_manager
        self.model_manager = model_manager
        self.relay_url = f"{base_url}:{port}"
        self.stop_polling = False  # Flag to control polling loop for testing
        
    def ping_relay(self) -> Dict[str, Any]:
        """
        Send a ping to the relay server to register this server and check for client requests.
        
        Returns:
            Dict containing relay server response
        """
        try:
            log_info(f"Pinging relay {self.relay_url}/sink with key {self.crypto_manager.public_key_b64[:10]}...")
            
            response = requests.post(
                f'{self.relay_url}/sink', 
                json={'server_public_key': self.crypto_manager.public_key_b64},
                timeout=10  # Add timeout for network resilience
            )
            
            if response.status_code == 200:
                return response.json()
            else:
                log_error(f"Error from relay /sink: {response.status_code} {response.text}")
                return {
                    'error': f"HTTP {response.status_code}",
                    'next_ping_in_x_seconds': 10  # Default to 10 seconds if there's an error
                }
        except requests.RequestException as e:
            log_error(f"Network error when pinging relay: {e}", exc_info=True)
            return {'error': str(e), 'next_ping_in_x_seconds': 10}
        except Exception as e:
            log_error(f"Unexpected error when pinging relay: {e}", exc_info=True)
            return {'error': str(e), 'next_ping_in_x_seconds': 10}
    
    def process_client_request(self, request_data: Dict[str, Any]) -> bool:
        """
        Process a client request from the relay.
        
        Args:
            request_data: Data received from the relay containing the encrypted client request
            
        Returns:
            bool: True if processing succeeded, False otherwise
        """
        try:
            if not all(k in request_data for k in ['client_public_key', 'chat_history', 'cipherkey', 'iv']):
                log_info("Missing required fields in client request")
                return False
                
            client_pub_key_b64 = request_data['client_public_key']
            
            # Decrypt the request
            log_info("Decrypting client request...")
            decrypted_chat_history = self.crypto_manager.decrypt_message(request_data)
            
            if decrypted_chat_history is None:
                log_info("Decryption failed. Skipping.")
                return False
                
            log_info(f"Decrypted request: {decrypted_chat_history}")
            
            # Process with LLM
            log_info("Getting response from LLM...")
            response_history = self.model_manager.llama_cpp_get_response(decrypted_chat_history)
            log_info(f"LLM response history: {response_history}")

            # Encrypt the response for the client
            log_info("Encrypting response for client...")
            client_pub_key = base64.b64decode(client_pub_key_b64)
            
            encrypted_response = self.crypto_manager.encrypt_message(
                response_history,
                client_pub_key
            )

            # Create the payload for the source endpoint
            source_payload = {
                'client_public_key': client_pub_key_b64,
                **encrypted_response  # Include chat_history, cipherkey, and iv
            }
            
            log_info(f"Posting response to {self.relay_url}/source. Payload keys: {list(source_payload.keys())}")
            
            # Send the response to the relay
            source_response = requests.post(
                f'{self.relay_url}/source', 
                json=source_payload,
                timeout=10
            )
            
            log_info(f"Response sent to /source. Status: {source_response.status_code}, Text: {source_response.text}")
            return source_response.status_code == 200
            
        except Exception as e:
            log_error(f"Exception during request processing: {e}", exc_info=True)
            return False
            
    def poll_relay_continuously(self):
        """
        Continuously poll the relay for new chat messages and process them.
        This method runs in an infinite loop and should be called in a separate thread.
        
        In test environments, you can set self.stop_polling = True to exit the loop.
        """
        while not self.stop_polling:
            try:
                # Ping the relay and check for client requests
                relay_response = self.ping_relay()
                
                if 'error' in relay_response:
                    log_error(f"Error from relay: {relay_response['error']}")
                else:
                    log_info(f"Received data from relay: {relay_response}")
                    
                    # Check if there's a client request to process
                    if 'client_public_key' in relay_response and 'chat_history' in relay_response:
                        log_info("Processing client request...")
                        self.process_client_request(relay_response)
                    else:
                        log_info("No client request data in sink response.")
                
                # Sleep before the next ping
                sleep_duration = relay_response.get('next_ping_in_x_seconds', 10)
                log_info(f"Sleeping for {sleep_duration} seconds...")
                time.sleep(sleep_duration)
                
            except Exception as e:
                log_error(f"Exception during polling loop: {e}", exc_info=True)
                time.sleep(10)  # Sleep for 10 seconds on error 