"""
Performance benchmark tests for the encryption and decryption operations.

These tests measure:
1. Encryption speed with different payload sizes
2. Decryption speed with different payload sizes
3. End-to-end encryption/decryption cycles
4. Key generation performance
"""

import pytest
import time
import base64
import os
import sys
import json
from typing import List, Dict, Any, Tuple

# Add the project root to the path for imports
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import the modules to test
from encrypt import encrypt, decrypt, generate_keys
from utils.crypto.crypto_manager import CryptoManager

try:
    # Try to import pytest-benchmark
    import pytest_benchmark
    BENCHMARK_AVAILABLE = True
except ImportError:
    BENCHMARK_AVAILABLE = False
    print("pytest-benchmark not available, running simplified benchmarks")

# Skip mark for when benchmark plugin is not available
benchmark_skip = pytest.mark.skipif(
    not BENCHMARK_AVAILABLE,
    reason="pytest-benchmark not installed"
)

# Set up test data sizes
TINY_DATA = "x" * 100  # 100 bytes
SMALL_DATA = "x" * 1024  # 1 KB
MEDIUM_DATA = "x" * (10 * 1024)  # 10 KB
LARGE_DATA = "x" * (100 * 1024)  # 100 KB
VERY_LARGE_DATA = "x" * (1024 * 1024)  # 1 MB

# Sample JSON data to simulate real-world usage
SAMPLE_JSON = {
    "messages": [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Tell me about the history of encryption."}
    ],
    "model": "llama-3-8b-instruct",
    "temperature": 0.7,
    "max_tokens": 500
}

@pytest.fixture
def crypto_keys() -> Dict[str, bytes]:
    """Generate a pair of RSA keys for testing."""
    private_key, public_key = generate_keys()
    return {
        "private_key": private_key,
        "public_key": public_key
    }

@pytest.fixture
def crypto_manager() -> CryptoManager:
    """Create a CryptoManager instance."""
    return CryptoManager()

@benchmark_skip
@pytest.mark.benchmark(
    group="encryption",
    min_rounds=5,
    max_time=2.0
)
def test_encryption_performance_small(benchmark, crypto_keys):
    """Benchmark encryption performance with small (1KB) payload."""
    data = SMALL_DATA.encode('utf-8')
    public_key = crypto_keys["public_key"]
    
    # Run the benchmark
    result = benchmark(encrypt, data, public_key)
    
    # Verify the result
    ciphertext_dict, cipherkey, iv = result
    assert ciphertext_dict is not None
    assert cipherkey is not None
    assert iv is not None

@benchmark_skip
@pytest.mark.benchmark(
    group="encryption",
    min_rounds=5,
    max_time=2.0
)
def test_encryption_performance_medium(benchmark, crypto_keys):
    """Benchmark encryption performance with medium (10KB) payload."""
    data = MEDIUM_DATA.encode('utf-8')
    public_key = crypto_keys["public_key"]
    
    # Run the benchmark
    result = benchmark(encrypt, data, public_key)
    
    # Verify the result
    ciphertext_dict, cipherkey, iv = result
    assert ciphertext_dict is not None
    assert cipherkey is not None
    assert iv is not None

@benchmark_skip
@pytest.mark.benchmark(
    group="encryption",
    min_rounds=3,
    max_time=2.0
)
def test_encryption_performance_large(benchmark, crypto_keys):
    """Benchmark encryption performance with large (100KB) payload."""
    data = LARGE_DATA.encode('utf-8')
    public_key = crypto_keys["public_key"]
    
    # Run the benchmark
    result = benchmark(encrypt, data, public_key)
    
    # Verify the result
    ciphertext_dict, cipherkey, iv = result
    assert ciphertext_dict is not None
    assert cipherkey is not None
    assert iv is not None

@benchmark_skip
@pytest.mark.benchmark(
    group="decryption",
    min_rounds=5,
    max_time=2.0
)
def test_decryption_performance(benchmark, crypto_keys):
    """Benchmark decryption performance with medium (10KB) payload."""
    data = MEDIUM_DATA.encode('utf-8')
    private_key = crypto_keys["private_key"]
    public_key = crypto_keys["public_key"]
    
    # First encrypt the data
    ciphertext_dict, cipherkey, iv = encrypt(data, public_key)
    
    # Run the benchmark
    result = benchmark(decrypt, ciphertext_dict, cipherkey, private_key)
    
    # Verify the result
    assert result == data

@benchmark_skip
@pytest.mark.benchmark(
    group="crypto_manager",
    min_rounds=5,
    max_time=2.0
)
def test_crypto_manager_encrypt_performance(benchmark, crypto_manager):
    """Benchmark CryptoManager encryption performance with JSON data."""
    # Create sample message
    message = SAMPLE_JSON
    
    # Generate a client public key
    _, client_public_key = generate_keys()
    
    # Run the benchmark
    result = benchmark(crypto_manager.encrypt_message, message, client_public_key)
    
    # Verify the result
    assert "chat_history" in result
    assert "cipherkey" in result
    assert "iv" in result

@benchmark_skip
@pytest.mark.benchmark(
    group="key_generation",
    min_rounds=3,
    max_time=5.0
)
def test_key_generation_performance(benchmark):
    """Benchmark RSA key pair generation performance."""
    # Run the benchmark
    result = benchmark(generate_keys)
    
    # Verify the result
    private_key, public_key = result
    assert private_key is not None
    assert public_key is not None
    assert b"PRIVATE KEY" in private_key
    assert b"PUBLIC KEY" in public_key

# Add fallback tests that work without the benchmark plugin
def test_basic_encryption_performance():
    """Basic encryption performance test without benchmark plugin."""
    if BENCHMARK_AVAILABLE:
        pytest.skip("Skipping basic test since benchmark plugin is available")
    
    private_key, public_key = generate_keys()
    
    data_sizes = {
        "tiny": TINY_DATA,
        "small": SMALL_DATA,
        "medium": MEDIUM_DATA,
        "large": LARGE_DATA
    }
    
    results = {}
    
    for name, data in data_sizes.items():
        start_time = time.time()
        ciphertext_dict, cipherkey, iv = encrypt(data.encode('utf-8'), public_key)
        encrypt_time = time.time() - start_time
        
        start_time = time.time()
        decrypted = decrypt(ciphertext_dict, cipherkey, private_key)
        decrypt_time = time.time() - start_time
        
        results[name] = {
            "data_size": len(data),
            "encrypt_time": encrypt_time,
            "decrypt_time": decrypt_time,
            "total_time": encrypt_time + decrypt_time
        }
        
        assert decrypted.decode('utf-8') == data
    
    # Print results in a table
    print("\nEncryption/Decryption Performance:")
    print(f"{'Size':<10} {'Data Size':<12} {'Encrypt (s)':<12} {'Decrypt (s)':<12} {'Total (s)':<12}")
    print("-" * 60)
    for name, result in results.items():
        print(f"{name:<10} {result['data_size']:<12} {result['encrypt_time']:.6f}  {result['decrypt_time']:.6f}  {result['total_time']:.6f}")

def test_large_payload_handling():
    """Test handling of very large payloads to identify potential issues."""
    private_key, public_key = generate_keys()
    
    # Define a series of increasingly larger payloads
    payloads = [
        SMALL_DATA,
        MEDIUM_DATA,
        LARGE_DATA
    ]
    
    for i, payload in enumerate(payloads):
        try:
            # Time the encryption and decryption
            start_time = time.time()
            ciphertext_dict, cipherkey, iv = encrypt(payload.encode('utf-8'), public_key)
            encrypt_time = time.time() - start_time
            
            start_time = time.time()
            decrypted = decrypt(ciphertext_dict, cipherkey, private_key)
            decrypt_time = time.time() - start_time
            
            # Assert the decryption worked correctly
            assert decrypted.decode('utf-8') == payload
            
            print(f"Size {len(payload)} bytes: Encrypt {encrypt_time:.4f}s, Decrypt {decrypt_time:.4f}s")
        except Exception as e:
            print(f"Failed at payload size {len(payload)} bytes: {e}")
            raise

if __name__ == "__main__":
    # Run the basic performance test when script is executed directly
    test_basic_encryption_performance()
    test_large_payload_handling() 