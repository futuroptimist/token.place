"""
Model manager module for handling LLM model downloading, initialization and inference.
"""
import os
import time
import logging
import requests
import json
from pathlib import Path
from threading import Lock
from unittest.mock import MagicMock
from typing import Dict, List, Any, Optional, Union, Tuple

# Configure logging
logger = logging.getLogger('model_manager')

class ModelManager:
    """
    Manages LLM model downloading, initialization, and inference.
    """
    def __init__(self, config=None):
        """Initialize the ModelManager with configuration."""
        # Import config lazily to avoid circular imports
        if config is None:
            from config import get_config
            config = get_config()
            
        self.config = config

        # Llama model configuration
        self.file_name = config.get('model.filename', 'llama-3-8b-instruct.Q4_K_M.gguf')
        self.url = config.get('model.url', 'https://huggingface.co/TheBloke/Llama-3-8B-Instruct-GGUF/resolve/main/llama-3-8b-instruct.Q4_K_M.gguf')
        self.chunk_size_mb = config.get('model.download_chunk_size_mb', 10)
        # Network timeout for model downloads (seconds)
        self.download_timeout = config.get('model.download_timeout', 30)
        self.models_dir = config.get('paths.models_dir')
        self.model_path = os.path.join(self.models_dir, self.file_name)
        
        # LLM instance and lock for thread safety
        self.llm = None
        self.llm_lock = Lock()
        
        # Check if mock mode is enabled
        self.use_mock_llm = config.get('model.use_mock', False) or os.getenv('USE_MOCK_LLM') == '1'
        
    def log_info(self, message):
        """Log info only in non-production environments"""
        if not self.config.is_production:
            logger.info(message)

    def log_warning(self, message):
        """Log warnings only in non-production environments"""
        if not self.config.is_production:
            logger.warning(message)

    def log_error(self, message, exc_info=False):
        """Log errors only in non-production environments"""
        if not self.config.is_production:
            logger.error(message, exc_info=exc_info)
        
    def create_models_directory(self) -> str:
        """Create the models directory if it doesn't exist."""
        if not os.path.exists(self.models_dir):
            os.makedirs(self.models_dir)
        return self.models_dir

    def download_file_in_chunks(self, file_path: str, url: str, chunk_size_mb: int) -> bool:
        """
        Download a file in chunks with progress reporting.
        
        Args:
            file_path: The path to save the file to
            url: The URL to download from
            chunk_size_mb: The chunk size in MB
            
        Returns:
            bool: True if download was successful, False otherwise
        """
        chunk_size_bytes = chunk_size_mb * 1024 * 1024  # Convert MB to bytes
        response = requests.get(url, stream=True, timeout=self.download_timeout)

        if response.status_code != 200:
            self.log_error(f"Error: Unable to download file, status code {response.status_code}")
            return False

        total_size_in_bytes = int(response.headers.get('content-length', 0))
        if total_size_in_bytes == 0:
            self.log_error("Error: Content-Length header is missing or zero.")
            return False

        total_size_in_mb = total_size_in_bytes / (1024 * 1024)
        progress = 0
        start_time = time.time()
        times = []
        bytes_downloaded = []

        try:
            with open(file_path, 'wb') as file:
                for data in response.iter_content(chunk_size=chunk_size_bytes):
                    if not data:
                        self.log_warning("Warning: Received empty data chunk.")
                        continue

                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())

                    elapsed_time = time.time() - start_time
                    progress += len(data)
                    times.append(elapsed_time)
                    bytes_downloaded.append(progress)

                    # Keep only the last 10 seconds of data
                    times = [t for t in times if elapsed_time - t <= 10]
                    bytes_downloaded = bytes_downloaded[-len(times):]

                    # Calculate speed and estimated time remaining
                    speed = sum(bytes_downloaded) / sum(times) if times else 0
                    eta = (total_size_in_bytes - progress) / speed if speed else 0

                    downloaded_mb = progress / (1024 * 1024)
                    done = int(50 * progress / total_size_in_bytes)
                    if not self.config.is_production:
                        # Progress output is cosmetic and difficult to test
                        print(
                            f'\r[{"=" * done}{" " * (50-done)}] {progress * 100 / total_size_in_bytes:.2f}% ({downloaded_mb:.2f}/{total_size_in_mb:.2f} MB) ETA: {eta:.2f}s',
                            end='\r',
                        )  # pragma: no cover
        except Exception as e:
            self.log_error(f"Error during file download: {e}")
            return False

        if os.path.exists(file_path) and os.path.getsize(file_path) == total_size_in_bytes:
            self.log_info(f"File Size Immediately After Download: {os.path.getsize(file_path)} bytes")
            return True
        else:
            self.log_error("Download failed or file size does not match.")
            return False

    def download_model_if_needed(self) -> bool:
        """
        Download the model file if it doesn't exist.
        
        Returns:
            bool: True if the model file exists (either already present or successfully downloaded),
                 False if download failed
        """
        self.create_models_directory()
        
        if not os.path.exists(self.model_path):
            self.log_info(f"Downloading {self.file_name}...")
            if self.download_file_in_chunks(self.model_path, self.url, self.chunk_size_mb):
                self.log_info("Download completed!")
                return True
            else:
                self.log_error("Download failed or file is empty.")
                return False
        else:
            self.log_info(f"Model file {self.file_name} already exists.")
            return True
    
    def get_llm_instance(self):
        """
        Gets the Llama instance, initializing it if necessary (thread-safe),
        or returns a mock if USE_MOCK_LLM is set.
        
        Returns:
            A Llama instance or a MagicMock object
        """
        # Check if mocking is enabled via configuration
        if self.use_mock_llm:
            self.log_info("Using Mock LLM instance based on USE_MOCK_LLM configuration.")
            mock_llama_instance = MagicMock()
            mock_response = {
                'choices': [
                    {
                        'message': {
                            'role': 'assistant',
                            # Make the mock response more specific for easier debugging
                            'content': 'Mock Response: The capital of France is Paris.'
                        }
                    }
                ]
            }
            mock_llama_instance.create_chat_completion.return_value = mock_response
            return mock_llama_instance

        # Quick check without lock
        if self.llm is None:
            # Acquire lock only if we might need to initialize
            with self.llm_lock:
                # Double-check after acquiring lock
                if self.llm is None:
                    if not os.path.exists(self.model_path):
                        self.log_error(f"Error: Model file {self.model_path} does not exist. LLM not initialized.")
                        return None
                    else:
                        try:
                            # Dynamically import Llama only when needed
                            from llama_cpp import Llama
                            
                            self.log_info(f"Initializing Llama model from {self.model_path}...")
                            self.llm = Llama(
                                model_path=self.model_path,
                                n_gpu_layers=-1,
                                n_ctx=self.config.get('model.context_size', 8192),
                                chat_format=self.config.get('model.chat_format', 'llama-3')
                            )
                            self.log_info("Llama model initialized successfully.")
                        except Exception as e:
                            self.log_error(f"Failed to initialize Llama model: {e}", exc_info=True)
                            return None
        
        return self.llm
    
    def llama_cpp_get_response(self, chat_history: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """
        Get a response from the LLM given a chat history.
        
        Args:
            chat_history: List of chat messages with 'role' and 'content' keys
            
        Returns:
            Updated chat history with the model's response appended
        """
        llm_instance = self.get_llm_instance()
        if llm_instance is None:
            # Return a simple error response if LLM initialization failed
            chat_history.append({
                "role": "assistant",
                "content": "Sorry, I'm having trouble accessing my language capabilities right now."
            })
            return chat_history
        
        try:
            # If we got a list of chat messages, convert it to the format expected by the Llama API
            self.log_info(f"Generating response for chat history: {chat_history}")
            
            # Create a copy of the chat history to avoid modifying the original
            result = chat_history.copy()
            
            # Generate the completion
            completion = llm_instance.create_chat_completion(
                messages=chat_history,
                max_tokens=self.config.get('model.max_tokens', 512),
                temperature=self.config.get('model.temperature', 0.7),
                top_p=self.config.get('model.top_p', 0.9),
                stop=self.config.get('model.stop_tokens', []),
            )
            
            # Extract the assistant's response
            assistant_message = completion['choices'][0]['message']
            self.log_info(f"Generated response: {assistant_message}")
            
            # Append the assistant's response to the chat history
            result.append(assistant_message)
            
            return result
            
        except Exception as e:
            self.log_error(f"Error during LLM inference: {e}", exc_info=True)
            # Return an error message
            chat_history.append({
                "role": "assistant",
                "content": "I'm sorry, I encountered an error while processing your request."
            })
            return chat_history

# Create a singleton instance
# Delay instantiation to avoid circular imports
model_manager = None

def get_model_manager():
    """Get the global model manager instance, creating it if necessary."""
    global model_manager
    if model_manager is None:
        model_manager = ModelManager()
    return model_manager 
