import encrypt as encrypt_module
from encrypt import decrypt, encrypt, generate_keys


def test_decrypt_caches_deserialized_private_key(monkeypatch):
    private_key_pem, public_key_pem = generate_keys()
    ciphertext_dict, encrypted_key, _ = encrypt(b"hello", public_key_pem)

    encrypt_module._load_private_key_cached.cache_clear()

    load_calls = []
    original_loader = encrypt_module.serialization.load_pem_private_key

    def counting_loader(pem, password=None, backend=None):
        load_calls.append(pem)
        return original_loader(pem, password=password, backend=backend)

    monkeypatch.setattr(
        encrypt_module.serialization,
        "load_pem_private_key",
        counting_loader,
    )

    try:
        first = decrypt(ciphertext_dict, encrypted_key, private_key_pem)
        second = decrypt(ciphertext_dict, encrypted_key, private_key_pem)
    finally:
        encrypt_module._load_private_key_cached.cache_clear()

    assert first == b"hello"
    assert second == b"hello"
    assert len(load_calls) == 1
