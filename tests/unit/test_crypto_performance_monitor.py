"""Tests for encryption performance monitoring."""

import base64

import pytest

from encrypt import encrypt, generate_keys
from utils.crypto.crypto_manager import CryptoManager
from utils.performance import get_encryption_monitor


@pytest.fixture
def performance_monitor(monkeypatch):
    """Enable the encryption performance monitor for the duration of a test."""

    monkeypatch.setenv('TOKEN_PLACE_PERF_MONITOR', '1')
    monkeypatch.setenv('TOKEN_PLACE_PERF_SAMPLES', '10')
    monitor = get_encryption_monitor()
    monitor.refresh_from_env()
    monitor.clear()
    yield monitor
    monitor.clear()
    monkeypatch.delenv('TOKEN_PLACE_PERF_MONITOR', raising=False)
    monkeypatch.delenv('TOKEN_PLACE_PERF_SAMPLES', raising=False)
    monitor.refresh_from_env()


def test_encrypt_message_records_metrics(performance_monitor):
    manager = CryptoManager()
    _, client_public_key = generate_keys()

    result = manager.encrypt_message({'payload': 'hello world'}, client_public_key)

    assert 'chat_history' in result
    summary = performance_monitor.summary('encrypt')
    assert summary['count'] >= 1
    assert summary['avg_payload_bytes'] >= len('{"payload": "hello world"}'.encode('utf-8'))
    assert summary['avg_duration_ms'] >= 0
    assert summary['throughput_bytes_per_sec'] >= 0


def test_decrypt_message_records_metrics(performance_monitor):
    manager = CryptoManager()
    ciphertext_dict, encrypted_key, iv = encrypt(b'monitor me', manager.public_key)

    payload = {
        'chat_history': base64.b64encode(ciphertext_dict['ciphertext']).decode('utf-8'),
        'cipherkey': base64.b64encode(encrypted_key).decode('utf-8'),
        'iv': base64.b64encode(iv).decode('utf-8'),
    }

    decrypted = manager.decrypt_message(payload)

    assert decrypted == 'monitor me'
    summary = performance_monitor.summary('decrypt')
    assert summary['count'] >= 1
    assert summary['avg_payload_bytes'] >= len(ciphertext_dict['ciphertext'])
    assert summary['avg_duration_ms'] >= 0
    assert summary['throughput_bytes_per_sec'] >= 0
