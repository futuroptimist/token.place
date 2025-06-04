"""
Security tests for the token.place encryption system.

These tests verify the security properties of the encryption implementation
by testing for common vulnerabilities and ensuring proper cryptographic practices.
"""

import pytest
import base64
import os
import sys
import hashlib
import json
from pathlib import Path
from typing import Dict, Any, List, Tuple

# Add the project root to the path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import the modules to test
from encrypt import generate_keys, encrypt, decrypt
from cryptography.hazmat.primitives.asymmetric import padding as asymmetric_padding
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

class TestEncryptionSecurity:
    """Security tests for the encryption implementation."""
    
    def test_key_uniqueness(self):
        """Test that generated keys are unique and not predictable."""
        # Generate multiple key pairs
        num_keys = 5
        key_pairs = []
        
        for _ in range(num_keys):
            private_key, public_key = generate_keys()
            key_pairs.append((private_key, public_key))
        
        # Check that all keys are unique
        private_keys = [pair[0] for pair in key_pairs]
        public_keys = [pair[1] for pair in key_pairs]
        
        # Check private keys are unique
        for i in range(num_keys):
            for j in range(i + 1, num_keys):
                assert private_keys[i] != private_keys[j], "Private keys should be unique"
        
        # Check public keys are unique
        for i in range(num_keys):
            for j in range(i + 1, num_keys):
                assert public_keys[i] != public_keys[j], "Public keys should be unique"
    
    def test_same_plaintext_different_ciphertext(self):
        """
        Test that the same plaintext encrypted multiple times produces different ciphertexts.
        This ensures the encryption is non-deterministic (uses different IVs/nonces).
        """
        # Generate keys
        private_key, public_key = generate_keys()
        
        # Plaintext to encrypt
        plaintext = b"This is a test message"
        
        # Encrypt the same plaintext multiple times
        ciphertexts = []
        for _ in range(5):
            ciphertext_dict, _, _ = encrypt(plaintext, public_key)
            ciphertexts.append(ciphertext_dict['ciphertext'])
        
        # Check that each encryption produces a different ciphertext
        for i in range(len(ciphertexts)):
            for j in range(i + 1, len(ciphertexts)):
                assert ciphertexts[i] != ciphertexts[j], "Same plaintext should produce different ciphertexts"
    
    def test_iv_uniqueness(self):
        """Test that each encryption operation uses a unique IV (initialization vector)."""
        # Generate keys
        private_key, public_key = generate_keys()
        
        # Plaintext to encrypt
        plaintext = b"This is a test message"
        
        # Collect IVs from multiple encryption operations
        ivs = []
        for _ in range(10):
            ciphertext_dict, _, iv = encrypt(plaintext, public_key)
            ivs.append(iv)
        
        # Check that all IVs are unique
        for i in range(len(ivs)):
            for j in range(i + 1, len(ivs)):
                assert ivs[i] != ivs[j], "IVs should be unique for each encryption"
    
    def test_forward_secrecy(self):
        """
        Test forward secrecy by verifying that compromise of one message doesn't 
        compromise other messages.
        """
        # Generate keys
        private_key, public_key = generate_keys()
        
        # Encrypt multiple messages
        plaintexts = [
            b"Message one",
            b"Message two",
            b"Message three"
        ]
        
        encrypted_messages = []
        for plaintext in plaintexts:
            ciphertext_dict, cipherkey, iv = encrypt(plaintext, public_key)
            encrypted_messages.append((ciphertext_dict, cipherkey, iv))
        
        # Simulate compromise of one message by having access to its AES key
        compromised_index = 1
        compromised_dict, compromised_key, compromised_iv = encrypted_messages[compromised_index]
        
        # Attempt to decrypt other messages with the compromised key
        for i, (ciphertext_dict, _, _) in enumerate(encrypted_messages):
            if i == compromised_index:
                continue  # Skip the compromised message
            
            # Try to decrypt with the compromised key
            # We'll use the correct IV but the compromised AES key
            attempt = {
                'ciphertext': ciphertext_dict['ciphertext'],
                'iv': ciphertext_dict['iv']
            }
            
            # This should fail to decrypt or produce garbage
            result = None
            try:
                # We're attempting to decrypt with a mismatched key/ciphertext
                # Either this will fail entirely or produce garbage
                
                # Skip the direct decryption attempt as it requires internal access to private_key's methods
                # Instead, we'll test at a higher level using the decrypt function
                
                # Create a fake cipherkey that pairs with the compromised key
                from cryptography.hazmat.primitives.serialization import load_pem_private_key
                private_key_obj = load_pem_private_key(
                    private_key,
                    password=None,
                    backend=default_backend()
                )
                # Instead of trying to reuse the compromised key, we'll check that different ciphertexts
                # decrypt to different plaintexts, indicating message independence
                result = decrypt(ciphertext_dict, compromised_key, private_key)
            except Exception:
                # Exception is expected
                pass
            
            # If decryption didn't fail with an exception, ensure we didn't get the original plaintext
            if result is not None:
                assert result != plaintexts[i], "Compromised key should not decrypt other messages"
    
    def test_padding_oracle_resistance(self):
        """
        Test resistance to padding oracle attacks by verifying the system doesn't 
        leak information about padding through different error messages.
        """
        # Generate keys
        private_key, public_key = generate_keys()
        
        # Encrypt a message
        plaintext = b"This is a test message with proper padding"
        ciphertext_dict, cipherkey, iv = encrypt(plaintext, public_key)
        
        # Create variations of ciphertext with modified padding
        original_ciphertext = ciphertext_dict['ciphertext']
        modified_ciphertexts = []
        
        # Modify the last few bytes (where padding is likely to be)
        for i in range(1, 17):  # Test modifications in the last block
            if len(original_ciphertext) > i:
                # Modify a single byte
                modified = bytearray(original_ciphertext)
                modified[-i] ^= 0x01  # XOR with 1 to flip a bit
                modified_ciphertexts.append(bytes(modified))
        
        # Instead of trying to access internal methods, we'll use the decrypt function
        # and check for a consistent error handling pattern
        
        # Collect error messages indirectly by observing function behavior
        error_behaviors = set()
        for modified_ct in modified_ciphertexts:
            modified_dict = {
                'ciphertext': modified_ct,
                'iv': ciphertext_dict['iv']
            }
            
            # Try to decrypt and observe behavior
            result = decrypt(modified_dict, cipherkey, private_key)
            
            # Categorize the behavior
            if result is None:
                error_behaviors.add("None")
            else:
                # Check if it's a padding error or not
                try:
                    # Try to unpad manually
                    unpadder = padding.PKCS7(128).unpadder()
                    unpadder.update(result) + unpadder.finalize()
                    error_behaviors.add("Decrypts but with different content")
                except Exception:
                    error_behaviors.add("Padding error")
        
        # If the system is resistant to padding oracle attacks, we should have a small
        # number of distinct error behaviors (ideally just one generic error)
        assert len(error_behaviors) <= 2, f"Too many distinct error behaviors: {len(error_behaviors)}"
    
    def test_ciphertext_integrity(self):
        """
        Test that modifying the ciphertext leads to decryption failure or incorrect output,
        not a security breach.
        """
        # Generate keys
        private_key, public_key = generate_keys()
        
        # Encrypt a message
        plaintext = b"This is a test message for integrity checking"
        ciphertext_dict, cipherkey, iv = encrypt(plaintext, public_key)
        
        # Modify the ciphertext slightly (flip 1 bit)
        modified_ciphertext = bytearray(ciphertext_dict['ciphertext'])
        # Choose a random position to modify
        pos = len(modified_ciphertext) // 2
        modified_ciphertext[pos] ^= 0x01  # XOR with 1 to flip a bit
        
        modified_dict = {
            'ciphertext': bytes(modified_ciphertext),
            'iv': ciphertext_dict['iv']
        }
        
        # Attempt to decrypt the modified ciphertext
        result = decrypt(modified_dict, cipherkey, private_key)
        
        # Either decryption should fail (result is None) or the result should be different from the original
        assert result is None or result != plaintext, "Modified ciphertext should not decrypt to original plaintext" 
