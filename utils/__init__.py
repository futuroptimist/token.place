"""
Utilities package for token.place.
"""

from utils.path_handling import get_temp_dir

__all__ = ['get_model_manager', 'get_crypto_manager', 'get_temp_dir', 'RelayClient']


def __getattr__(name):
    """Lazily expose convenience imports without front-loading runtime dependencies."""

    if name == 'get_model_manager':
        from utils.llm.model_manager import get_model_manager

        return get_model_manager
    if name == 'get_crypto_manager':
        from utils.crypto.crypto_manager import get_crypto_manager

        return get_crypto_manager
    if name == 'RelayClient':
        from utils.networking.relay_client import RelayClient

        return RelayClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
