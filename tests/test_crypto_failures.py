"""
Tests for failure scenarios in the encryption/decryption operations.

These tests focus on how the core encryption functions handle error cases,
without requiring server startup.
"""

import pytest
import base64
import os
import json
import sys
from pathlib import Path
from typing import Dict, Any

# Add the project root to the path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import the modules to test
from encrypt import generate_keys, encrypt, decrypt

class TestCryptoFailures:
    """Tests for encryption/decryption failure scenarios."""
    
    def test_decryption_with_invalid_key(self):
        """Test that decryption with an invalid key fails gracefully."""
        # Generate two different key pairs
        valid_private_key, valid_public_key = generate_keys()
        invalid_private_key, invalid_public_key = generate_keys()  # Different key pair
        
        # Test data
        plaintext = "Test message for encryption"
        
        # Encrypt with valid key
        ciphertext_dict, cipherkey, iv = encrypt(plaintext.encode(), valid_public_key)
        
        # Try to decrypt with a different key
        result = decrypt(ciphertext_dict, cipherkey, invalid_private_key)
        
        # The decrypt function should return None for invalid keys
        assert result is None, "Decryption with invalid key should return None"
    
    def test_decryption_with_corrupted_ciphertext(self):
        """Test decryption behavior with corrupted ciphertext."""
        # Generate keys
        private_key, public_key = generate_keys()
        
        # Test data
        plaintext = "Test message for encryption"
        
        # Encrypt with valid key
        ciphertext_dict, cipherkey, iv = encrypt(plaintext.encode(), public_key)
        
        # Corrupt the ciphertext
        corrupted_ciphertext_dict = {
            'ciphertext': os.urandom(len(ciphertext_dict['ciphertext'])),  # Random data
            'iv': ciphertext_dict['iv']  # Keep the same IV
        }
        
        # Try to decrypt with corrupted data
        result = decrypt(corrupted_ciphertext_dict, cipherkey, private_key)
        
        # The decrypt function should return None for corrupted data
        assert result is None, "Decryption with corrupted ciphertext should return None"
    
    def test_decryption_with_corrupted_iv(self):
        """Test decryption behavior with corrupted initialization vector."""
        # Generate keys
        private_key, public_key = generate_keys()
        
        # Test data
        plaintext = "Test message for encryption"
        
        # Encrypt with valid key
        ciphertext_dict, cipherkey, iv = encrypt(plaintext.encode(), public_key)
        
        # Corrupt the IV
        corrupted_ciphertext_dict = {
            'ciphertext': ciphertext_dict['ciphertext'],
            'iv': os.urandom(len(ciphertext_dict['iv']))  # Random IV
        }
        
        # Try to decrypt with corrupted IV
        result = decrypt(corrupted_ciphertext_dict, cipherkey, private_key)
        
        # A corrupted IV will result in garbage output, not None
        # When using CBC mode, the IV only impacts the first block, so decryption produces garbage but doesn't fail
        assert result is not None, "Decryption with corrupted IV should produce output"
        assert result != plaintext.encode(), "Decryption with corrupted IV should produce incorrect plaintext"
    
    def test_decryption_with_corrupted_cipherkey(self):
        """Test decryption behavior with corrupted cipher key."""
        # Generate keys
        private_key, public_key = generate_keys()
        
        # Test data
        plaintext = "Test message for encryption"
        
        # Encrypt with valid key
        ciphertext_dict, cipherkey, iv = encrypt(plaintext.encode(), public_key)
        
        # Corrupt the cipherkey
        corrupted_cipherkey = os.urandom(len(cipherkey))
        
        # Try to decrypt with corrupted cipherkey
        result = decrypt(ciphertext_dict, corrupted_cipherkey, private_key)
        
        # The decrypt function should return None for corrupted cipherkey
        assert result is None, "Decryption with corrupted cipherkey should return None"
    
    def test_decryption_with_missing_ciphertext(self):
        """Test decryption behavior when ciphertext is missing."""
        # Generate keys
        private_key, public_key = generate_keys()
        
        # Test data
        plaintext = "Test message for encryption"
        
        # Encrypt with valid key
        ciphertext_dict, cipherkey, iv = encrypt(plaintext.encode(), public_key)
        
        # Create dictionary with missing ciphertext
        incomplete_dict = {
            'iv': ciphertext_dict['iv']
            # ciphertext is intentionally missing
        }
        
        # Try to decrypt with missing ciphertext
        result = decrypt(incomplete_dict, cipherkey, private_key)
        
        # The decrypt function should return None for missing data
        assert result is None, "Decryption with missing ciphertext should return None"
    
    def test_decryption_with_missing_iv(self):
        """Test decryption behavior when IV is missing."""
        # Generate keys
        private_key, public_key = generate_keys()
        
        # Test data
        plaintext = "Test message for encryption"
        
        # Encrypt with valid key
        ciphertext_dict, cipherkey, iv = encrypt(plaintext.encode(), public_key)
        
        # Create dictionary with missing IV
        incomplete_dict = {
            'ciphertext': ciphertext_dict['ciphertext']
            # iv is intentionally missing
        }
        
        # Try to decrypt with missing IV
        result = decrypt(incomplete_dict, cipherkey, private_key)
        
        # The decrypt function should return None for missing IV
        assert result is None, "Decryption with missing IV should return None"
    
    def test_encryption_with_invalid_public_key(self):
        """Test encryption behavior with an invalid public key."""
        # Generate invalid data to use as public key
        invalid_public_key = os.urandom(128)  # Random bytes, not a valid key
        
        # Test data
        plaintext = "Test message for encryption"
        
        # Try to encrypt with invalid public key
        try:
            # This should fail
            encrypt(plaintext.encode(), invalid_public_key)
            assert False, "Encryption should fail with invalid public key"
        except Exception as e:
            # Verify we got an appropriate error
            assert "key" in str(e).lower(), f"Expected key-related error, got: {e}"
    
    def test_decryption_with_invalid_private_key(self):
        """Test decryption behavior with an invalid private key."""
        # Generate valid key pair for encryption
        private_key, public_key = generate_keys()
        
        # Generate invalid data to use as private key
        invalid_private_key = os.urandom(256)  # Random bytes, not a valid key
        
        # Test data
        plaintext = "Test message for encryption"
        
        # Encrypt with valid key
        ciphertext_dict, cipherkey, iv = encrypt(plaintext.encode(), public_key)
        
        # Try to decrypt with invalid private key
        result = decrypt(ciphertext_dict, cipherkey, invalid_private_key)
        
        # The decrypt function should return None for invalid private key
        assert result is None, "Decryption with invalid private key should return None"
    
    def test_zero_length_message(self):
        """Test encryption and decryption of zero-length messages."""
        # Generate keys
        private_key, public_key = generate_keys()
        
        # Empty message
        plaintext = b""
        
        # Encrypt empty message
        ciphertext_dict, cipherkey, iv = encrypt(plaintext, public_key)
        
        # Verify encryption produced valid output
        assert ciphertext_dict is not None
        assert 'ciphertext' in ciphertext_dict
        assert 'iv' in ciphertext_dict
        assert cipherkey is not None
        
        # Decrypt the empty message
        decrypted = decrypt(ciphertext_dict, cipherkey, private_key)
        
        # Verify decryption worked correctly
        assert decrypted == plaintext
        assert len(decrypted) == 0 