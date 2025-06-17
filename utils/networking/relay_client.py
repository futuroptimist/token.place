"""
Relay client module for managing communication with relay servers.
"""
import json
import logging
import requests
import time
import base64
import jsonschema
from typing import Dict, Optional, Any, List, Union, Tuple

# Configure logging
logger = logging.getLogger('relay_client')

def get_config_lazy():
    """Lazy import of config to avoid circular imports"""
    from config import get_config
    return get_config()

# Define JSON schema for messages
MESSAGE_SCHEMA = {
    "type": "object",
    "required": ["client_public_key", "chat_history", "cipherkey", "iv"],
    "properties": {
        "client_public_key": {"type": "string"},
        "chat_history": {"type": "string"},
        "cipherkey": {"type": "string"},
        "iv": {"type": "string"}
    }
}

# Define relay response schema
RELAY_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["next_ping_in_x_seconds"],
    "properties": {
        "next_ping_in_x_seconds": {"type": "number"},
        "client_public_key": {"type": "string"},
        "chat_history": {"type": "string"},
        "cipherkey": {"type": "string"},
        "iv": {"type": "string"},
        "error": {"type": "string"}
    }
}

def log_info(message, *args):
    """Log info only in non-production environments using consistent formatting"""
    try:
        config = get_config_lazy()
        if not config.is_production:
            if args:
                logger.info(message.format(*args))
            else:
                logger.info(message)
    except:
        # Fallback to always log if config is not available
        if args:
            logger.info(message.format(*args))
        else:
            logger.info(message)

def log_error(message, *args, exc_info=False):
    """Log errors only in non-production environments using consistent formatting"""
    try:
        config = get_config_lazy()
        if not config.is_production:
            if args:
                logger.error(message.format(*args), exc_info=exc_info)
            else:
                logger.error(message, exc_info=exc_info)
    except:
        # Fallback to always log if config is not available
        if args:
            logger.error(message.format(*args), exc_info=exc_info)
        else:
            logger.error(message, exc_info=exc_info)

class RelayClient:
    """
    Client for communicating with relay servers.
    Handles registration, polling, sending and receiving encrypted messages.
    
    Example:
        ```python
        # Create a relay client
        relay = RelayClient(
            base_url="http://localhost",
            port=8080,
            crypto_manager=crypto_manager_instance,
            model_manager=model_manager_instance
        )
        
        # Start polling in a separate thread
        import threading
        polling_thread = threading.Thread(target=relay.poll_relay_continuously)
        polling_thread.daemon = True
        polling_thread.start()
        
        # Later, to stop polling cleanly:
        relay.stop()
        polling_thread.join(timeout=15)  # Wait for thread to finish
        ```
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
        self.stop_polling = True  # Flag to control polling loop - starts as True so loop won't run until explicitly started
        try:
            config = get_config_lazy()
            self._request_timeout = config.get('relay.request_timeout', 10)  # Get timeout from config or use default
        except:
            self._request_timeout = 10  # Fallback default
        
    def start(self):
        """Start the polling loop by setting stop_polling to False"""
        self.stop_polling = False
        
    def stop(self):
        """Stop the polling loop by setting stop_polling to True"""
        log_info("Stopping relay polling")
        self.stop_polling = True
        
    def ping_relay(self) -> Dict[str, Any]:
        """
        Send a ping to the relay server to register this server and check for client requests.
        
        Returns:
            Dict containing relay server response
        
        Raises:
            requests.ConnectionError: If connection to relay fails
            requests.Timeout: If the request times out
            requests.RequestException: For other request-related errors
            ValueError: If the server response is not valid JSON or fails schema validation
        """
        try:
            log_info("Pinging relay {}/sink with key {}...", self.relay_url, self.crypto_manager.public_key_b64[:10])
            
            response = requests.post(
                f'{self.relay_url}/sink', 
                json={'server_public_key': self.crypto_manager.public_key_b64},
                timeout=self._request_timeout
            )
            
            if response.status_code == 200:
                relay_response = response.json()
                
                # Validate response against schema
                try:
                    jsonschema.validate(instance=relay_response, schema=RELAY_RESPONSE_SCHEMA)
                except jsonschema.exceptions.ValidationError as e:
                    log_error("Invalid relay response format: {}", str(e))
                    return {
                        'error': f"Invalid response format: {str(e)}",
                        'next_ping_in_x_seconds': self._request_timeout
                    }
                
                return relay_response
            else:
                log_error("Error from relay /sink: {} {}", response.status_code, response.text)
                return {
                    'error': f"HTTP {response.status_code}",
                    'next_ping_in_x_seconds': self._request_timeout
                }
        except requests.ConnectionError as e:
            log_error("Connection error when pinging relay: {}", str(e), exc_info=True)
            return {'error': str(e), 'next_ping_in_x_seconds': self._request_timeout}
        except requests.Timeout as e:
            log_error("Request timeout when pinging relay: {}", str(e), exc_info=True)
            return {'error': str(e), 'next_ping_in_x_seconds': self._request_timeout}
        except requests.RequestException as e:
            log_error("Request exception when pinging relay: {}", str(e), exc_info=True)
            return {'error': str(e), 'next_ping_in_x_seconds': self._request_timeout}
        except json.JSONDecodeError as e:
            log_error("Invalid JSON response from relay: {}", str(e), exc_info=True)
            return {'error': str(e), 'next_ping_in_x_seconds': self._request_timeout}
        except Exception as e:
            log_error("Unexpected error when pinging relay: {}", str(e), exc_info=True)
            return {'error': str(e), 'next_ping_in_x_seconds': self._request_timeout}
    
    def process_client_request(self, request_data: Dict[str, Any]) -> bool:
        """
        Process a client request from the relay.
        
        Args:
            request_data: Data received from the relay containing the encrypted client request
            
        Returns:
            bool: True if processing succeeded, False otherwise
        
        Example:
            ```python
            # Process data from relay
            request_data = {
                'client_public_key': 'base64_encoded_client_key',
                'chat_history': 'encrypted_data',
                'cipherkey': 'encrypted_key',
                'iv': 'initialization_vector'
            }
            success = relay_client.process_client_request(request_data)
            ```
        """
        try:
            # Validate request data against schema
            try:
                jsonschema.validate(instance=request_data, schema=MESSAGE_SCHEMA)
            except jsonschema.exceptions.ValidationError as e:
                log_error("Invalid request data format: {}", str(e))
                return False
                
            client_pub_key_b64 = request_data['client_public_key']
            
            # Decrypt the request
            log_info("Decrypting client request...")
            decrypted_chat_history = self.crypto_manager.decrypt_message(request_data)
            
            if decrypted_chat_history is None:
                log_info("Decryption failed. Skipping.")
                return False
                
            log_info("Decrypted request: {}", decrypted_chat_history)
            
            # Process with LLM
            log_info("Getting response from LLM...")
            response_history = self.model_manager.llama_cpp_get_response(decrypted_chat_history)
            log_info("LLM response history: {}", response_history)

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
            
            # Validate the outgoing payload
            try:
                jsonschema.validate(instance=source_payload, schema=MESSAGE_SCHEMA)
            except jsonschema.exceptions.ValidationError as e:
                log_error("Invalid response payload format: {}", str(e))
                return False
            
            log_info("Posting response to {}/source. Payload keys: {}", self.relay_url, list(source_payload.keys()))
            
            # Send the response to the relay
            try:
                source_response = requests.post(
                    f'{self.relay_url}/source', 
                    json=source_payload,
                    timeout=self._request_timeout
                )
                
                log_info("Response sent to /source. Status: {}, Text: {}", source_response.status_code, source_response.text)
                
                # Validate response beyond just status code
                if source_response.status_code != 200:
                    log_error("Error status from /source: {}", source_response.status_code)
                    return False
                    
                # Check if response has valid content
                response_content = source_response.text.strip()
                if not response_content:
                    log_error("Empty response from /source")
                    return False
                    
                return True
                
            except requests.ConnectionError as e:
                log_error("Connection error when posting to /source: {}", str(e), exc_info=True)
                return False
            except requests.Timeout as e:
                log_error("Request timeout when posting to /source: {}", str(e), exc_info=True)
                return False
            except requests.RequestException as e:
                log_error("Request exception when posting to /source: {}", str(e), exc_info=True)
                return False
            
        except Exception as e:
            log_error("Exception during request processing: {}", str(e), exc_info=True)
            return False
            
    def poll_relay_continuously(self):
        """
        Continuously poll the relay for new chat messages and process them.
        This method runs in an infinite loop and should be called in a separate thread.
        
        Call start() before running this method to set stop_polling to False.
        Call stop() to terminate the polling loop cleanly.
        
        Example:
            ```python
            import threading
            
            # Create a thread for polling
            relay_client.start()  # Allow polling to run
            thread = threading.Thread(target=relay_client.poll_relay_continuously)
            thread.daemon = True  # Thread will exit when main program exits
            thread.start()
            
            # Main program continues...
            
            # Later when you want to stop polling:
            relay_client.stop()
            thread.join(timeout=10)  # Wait for thread to finish
            ```
        """
        if self.stop_polling:
            log_info("Starting relay polling")
            self.stop_polling = False
            
        while not self.stop_polling:
            try:
                # Ping the relay and check for client requests
                relay_response = self.ping_relay()
                
                # Validate the relay response contains expected fields
                if not isinstance(relay_response, dict):
                    log_error("Invalid relay response type: {}", type(relay_response))
                    time.sleep(self._request_timeout)
                    continue
                    
                if 'next_ping_in_x_seconds' not in relay_response:
                    log_error("Missing 'next_ping_in_x_seconds' in relay response")
                    time.sleep(self._request_timeout)
                    continue
                
                if 'error' in relay_response:
                    log_error("Error from relay: {}", relay_response['error'])
                else:
                    log_info("Received data from relay: {}", relay_response)
                    
                    # Check if there's a client request to process
                    required_fields = ['client_public_key', 'chat_history', 'cipherkey', 'iv']
                    if all(field in relay_response for field in required_fields):
                        log_info("Processing client request...")
                        self.process_client_request(relay_response)
                    else:
                        log_info("No client request data in sink response.")
                
                # Sleep before the next ping
                sleep_duration = relay_response.get('next_ping_in_x_seconds', self._request_timeout)
                log_info("Sleeping for {} seconds...", sleep_duration)
                time.sleep(sleep_duration)
                
            except Exception as e:
                log_error("Exception during polling loop: {}", str(e), exc_info=True)
                time.sleep(self._request_timeout)  # Sleep for 10 seconds on error 
