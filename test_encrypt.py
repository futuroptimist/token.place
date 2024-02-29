import pytest
from encrypt import generate_keys, encrypt_message_with_public_key, decrypt_message_with_private_key
import os

def test_generate_keys():
    private_key, public_key = generate_keys()
    assert private_key is not None
    assert public_key is not None

def test_encrypt_decrypt():
    private_key, public_key = generate_keys()
    message = "This is a test message."
    encrypted_message = encrypt_message_with_public_key(message.encode('utf-8'), public_key)
    decrypted_message = decrypt_message_with_private_key(encrypted_message, private_key)
    
    # Ensure the decrypted message matches the original message
    assert message == decrypted_message.decode('utf-8')

@pytest.mark.parametrize("message", [
    "Hello, World!",
    "Another message",
    "Testing with numbers 1234567890",
    "Special characters !@#$%^&*()",
])
def test_encrypt_decrypt_various_messages(message):
    private_key, public_key = generate_keys()
    encrypted_message = encrypt_message_with_public_key(message.encode('utf-8'), public_key)
    decrypted_message = decrypt_message_with_private_key(encrypted_message, private_key)
    
    # Ensure the decrypted message matches the original message
    assert message == decrypted_message.decode('utf-8')
