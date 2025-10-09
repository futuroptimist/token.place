"""Regression tests for RSA key policy enforcement."""

import pytest

import encrypt


def test_generate_keys_rejects_insecure_rsa_key_size(monkeypatch):
    """Key sizes below 2048 bits should be rejected for security review readiness."""

    monkeypatch.setattr(encrypt, "RSA_KEY_SIZE", 1024, raising=False)
    with pytest.raises(ValueError, match="RSA_KEY_SIZE"):
        encrypt.generate_keys()


def test_generate_keys_accepts_secure_rsa_key_size(monkeypatch):
    """Key sizes at or above 2048 bits continue to work for backwards compatibility."""

    monkeypatch.setattr(encrypt, "RSA_KEY_SIZE", 2048, raising=False)
    private_key, public_key = encrypt.generate_keys()
    assert isinstance(private_key, bytes)
    assert isinstance(public_key, bytes)
    assert b"BEGIN" in private_key and b"BEGIN" in public_key
