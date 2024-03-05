import pytest
from encrypt import generate_keys, encrypt, decrypt

def test_generate_keys():
    private_key, public_key = generate_keys()
    assert private_key is not None
    assert public_key is not None

def test_encrypt_decrypt():
    private_key, public_key = generate_keys()
    message = "This is a test message."
    ciphertext, cipherkey = encrypt(message.encode('utf-8'), public_key)
    decrypted_message = decrypt(ciphertext, cipherkey, private_key)
    
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
    ciphertext, cipherkey = encrypt(message.encode('utf-8'), public_key)
    decrypted_message = decrypt(ciphertext, cipherkey, private_key)
    
    # Ensure the decrypted message matches the original message
    assert message == decrypted_message.decode('utf-8')