import pytest
import os
import platform
import tempfile
from pathlib import Path
import sys

# Add the project root to the path 
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from utils.path_handling import (
    get_user_home_dir, get_app_data_dir, get_config_dir, get_cache_dir, 
    get_models_dir, get_logs_dir, ensure_dir_exists, normalize_path,
    get_relative_path, IS_WINDOWS, IS_MACOS, IS_LINUX
)

class TestPathHandling:
    """Test suite for the path_handling module"""
    
    def test_platform_detection(self, platform_info):
        """Test that the platform is correctly detected"""
        system = platform.system().lower()
        
        # Check that exactly one platform flag is True
        platform_flags = [IS_WINDOWS, IS_MACOS, IS_LINUX]
        assert sum(platform_flags) == 1, "Exactly one platform flag should be True"
        
        # Check that the correct flag is True
        if system == "windows":
            assert IS_WINDOWS is True
            assert platform_info["is_windows"] is True
        elif system == "darwin":
            assert IS_MACOS is True
            assert platform_info["is_macos"] is True
        elif system == "linux":
            assert IS_LINUX is True
            assert platform_info["is_linux"] is True
    
    def test_get_user_home_dir(self):
        """Test that the user home directory is correctly detected"""
        home_dir = get_user_home_dir()
        assert home_dir.exists(), "Home directory should exist"
        assert home_dir.is_dir(), "Home directory should be a directory"
        
        # Check against environment variables
        if IS_WINDOWS:
            expected = os.environ.get("USERPROFILE")
        else:
            expected = os.environ.get("HOME")
        
        assert str(home_dir) == expected, f"Home directory should be {expected}, got {home_dir}"
    
    def test_get_app_data_dir(self):
        """Test that the application data directory is correctly constructed"""
        app_data_dir = get_app_data_dir()
        
        # The directory may not exist yet, so we don't check that
        
        # Check the platform-specific structure
        if IS_WINDOWS:
            assert "AppData\\Roaming\\token.place" in str(app_data_dir)
        elif IS_MACOS:
            assert "Library/Application Support/token.place" in str(app_data_dir)
        elif IS_LINUX:
            assert ".local/share/token.place" in str(app_data_dir)
    
    def test_get_config_dir(self):
        """Test that the configuration directory is correctly constructed"""
        config_dir = get_config_dir()
        
        # Check the platform-specific structure
        if IS_WINDOWS:
            assert "AppData\\Roaming\\token.place\\config" in str(config_dir)
        elif IS_MACOS:
            assert "Library/Application Support/token.place/config" in str(config_dir)
        elif IS_LINUX:
            assert ".config/token.place" in str(config_dir)
    
    def test_ensure_dir_exists(self, temp_dir):
        """Test that ensure_dir_exists creates directories correctly"""
        # Create a nested directory structure
        test_dir = temp_dir / "test" / "nested" / "dirs"
        
        # Directory should not exist yet
        assert not test_dir.exists()
        
        # Ensure it exists
        result = ensure_dir_exists(test_dir)
        
        # Check that it now exists
        assert test_dir.exists()
        assert test_dir.is_dir()
        
        # Check that the function returns the path
        assert result == test_dir
    
    def test_normalize_path(self):
        """Test that normalize_path correctly normalizes paths"""
        # Test with a tilde expansion
        home_path = normalize_path("~/test")
        assert "~" not in str(home_path)
        assert get_user_home_dir().name in str(home_path)
        
        # Test with relative paths
        rel_path = normalize_path("./test")
        assert rel_path.is_absolute()
        
        # Test with an absolute path
        abs_path = Path("/tmp" if not IS_WINDOWS else "C:\\Windows").absolute()
        norm_abs_path = normalize_path(abs_path)
        assert norm_abs_path == abs_path
    
    def test_get_relative_path(self, temp_dir):
        """Test that get_relative_path correctly computes relative paths"""
        # Create a nested directory structure
        base_dir = temp_dir
        nested_dir = base_dir / "nested" / "dirs"
        ensure_dir_exists(nested_dir)
        
        # Get a relative path
        rel_path = get_relative_path(nested_dir, base_dir)
        assert not rel_path.is_absolute()
        assert str(rel_path) == os.path.join("nested", "dirs")
        
        # Get a relative path when the path is not relative to the base
        other_dir = ensure_dir_exists(temp_dir.parent / "other")
        rel_path_2 = get_relative_path(other_dir, base_dir)
        assert rel_path_2.is_absolute()
        assert rel_path_2 == other_dir 
