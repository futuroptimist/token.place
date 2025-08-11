import importlib
import sys


def test_utils_star_import_exports_expected():
    """Star import should expose high-level helpers from utils."""
    if 'utils' in sys.modules:
        importlib.reload(sys.modules['utils'])
    namespace = {}
    exec('from utils import *', namespace)
    assert 'get_model_manager' in namespace
    assert 'get_crypto_manager' in namespace
    assert 'RelayClient' in namespace
