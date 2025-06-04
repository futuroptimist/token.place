import pytest
import os
import json
import tempfile
from pathlib import Path
import sys

# Add the project root to the path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import Config, get_config
from utils.path_handling import IS_WINDOWS, IS_MACOS, IS_LINUX, ensure_dir_exists

class TestConfig:
    """Test suite for the configuration system"""
    
    def test_global_config(self):
        """Test that the global config instance is created properly"""
        config = get_config()
        assert config is not None
        assert isinstance(config, Config)
        
        # Check that environment is set properly
        assert config.env in ['development', 'testing', 'production']
        
        # Check that platform is detected correctly
        assert config.platform in ['windows', 'darwin', 'linux']
        
        if IS_WINDOWS:
            assert config.is_windows is True
        elif IS_MACOS:
            assert config.is_macos is True
        elif IS_LINUX:
            assert config.is_linux is True
    
    def test_config_initialization(self, temp_data_dir):
        """Test that the Config class initializes properly"""
        # Create a custom config for testing
        config = Config(env="testing")
        
        # Check that environment is set
        assert config.env == "testing"
        assert config.is_testing is True
        
        # Check that default values are set
        assert config.get("server.port") is not None
        assert config.get("relay.port") is not None
        assert config.get("api.port") is not None
        
        # Check that environment-specific overrides are applied
        assert config.get("server.debug") is True  # testing env has debug=True
    
    def test_config_get_set(self):
        """Test the get and set methods of the Config class"""
        config = Config(env="testing")
        
        # Test get with existing key
        assert config.get("server.port") == 8001  # From testing environment
        
        # Test get with non-existent key
        assert config.get("non.existent.key") is None
        assert config.get("non.existent.key", "default") == "default"
        
        # Test set
        config.set("custom.key", "value")
        assert config.get("custom.key") == "value"
        
        # Test nested set
        config.set("custom.nested.key", 42)
        assert config.get("custom.nested.key") == 42
    
    def test_config_merge(self, temp_dir):
        """Test that configs are merged correctly"""
        # Create a test config file
        config_path = temp_dir / "test_config.json"
        config_content = {
            "server": {
                "port": 9000,
                "custom_setting": "test"
            },
            "custom_section": {
                "test": True
            }
        }
        
        with open(config_path, "w") as f:
            json.dump(config_content, f)
        
        # Create a config instance with the test config
        config = Config(env="development", config_path=str(config_path))
        
        # Check that the custom values are merged
        assert config.get("server.port") == 9000  # Overridden
        assert config.get("server.custom_setting") == "test"  # Added
        assert config.get("custom_section.test") is True  # Added
        
        # Check that non-overridden values are preserved
        assert config.get("relay.port") is not None  # From defaults
    
    def test_config_save(self, temp_dir):
        """Test saving the configuration to a file"""
        # Create a new config
        config = Config(env="testing")
        
        # Set some custom values
        config.set("custom.key", "value")
        config.set("server.port", 9999)
        
        # Save to a file
        config_path = temp_dir / "saved_config.json"
        config.save_user_config(str(config_path))
        
        # Check that the file exists
        assert config_path.exists()
        
        # Load the saved config and check values
        with open(config_path, "r") as f:
            saved_config = json.load(f)
        
        assert saved_config["custom"]["key"] == "value"
        assert saved_config["server"]["port"] == 9999
    
    def test_platform_specific_paths(self, temp_data_dir):
        """Test that platform-specific paths are configured correctly"""
        # Create a config with the temp data dir
        config = Config(env="testing")
        
        # Override paths for testing
        for key in ["data_dir", "models_dir", "logs_dir", "cache_dir", "keys_dir"]:
            path = temp_data_dir / key
            ensure_dir_exists(path)
            config.set(f"paths.{key}", str(path))
        
        # Check that all directories exist
        for key in ["data_dir", "models_dir", "logs_dir", "cache_dir", "keys_dir"]:
            path = Path(config.get(f"paths.{key}"))
            assert path.exists(), f"{key} directory should exist"
            assert path.is_dir(), f"{key} should be a directory" 
