import pytest
from encrypt import generate_keys, encrypt_message, decrypt_message, public_key_from_pem, save_private_key, save_public_key, load_private_key, load_public_key
import os

def test_generate_keys():
    private_key, public_key = generate_keys()
    assert private_key is not None
    assert public_key is not None

def test_key_saving_and_loading(tmp_path):
    private_key, public_key = generate_keys()
    private_key_path = os.path.join(tmp_path, "private.pem")
    public_key_path = os.path.join(tmp_path, "public.pem")
    
    save_private_key(private_key, private_key_path)
    save_public_key(public_key, public_key_path)
    
    loaded_private_key = load_private_key(private_key_path)
    loaded_public_key = load_public_key(public_key_path)
    
    assert private_key == loaded_private_key
    assert public_key == loaded_public_key

def test_encrypt_decrypt():
    private_key, public_key = generate_keys()
    message = "This is a test message."
    encrypted_message = encrypt_message(message.encode('utf-8'), public_key)
    decrypted_message = decrypt_message(encrypted_message, private_key)
    
    assert message == decrypted_message

def test_public_key_from_pem():
    _, public_key = generate_keys()
    pem_str = public_key.save_pkcs1()
    loaded_public_key = public_key_from_pem(pem_str)
    
    assert public_key == loaded_public_key

@pytest.mark.parametrize("message", [
    "Hello, World!",
    "Another message",
    "Testing with numbers 1234567890",
    "Special characters !@#$%^&*()",
])
def test_encrypt_decrypt_various_messages(message):
    private_key, public_key = generate_keys()
    encrypted_message = encrypt_message(message.encode('utf-8'), public_key)
    decrypted_message = decrypt_message(encrypted_message, private_key)
    
    assert message == decrypted_message
