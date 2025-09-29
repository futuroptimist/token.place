from pathlib import Path

import pytest

from utils.signing import relay_signature


@pytest.mark.unit
def test_relay_release_signature_verifies():
    relay_script = Path('relay.py')
    signature_file = Path('config/signing/relay.py.sig')
    public_key_file = Path('config/signing/relay_signing_public_key.pem')

    assert relay_signature.verify_file_signature(
        relay_script,
        signature_file,
        public_key_file,
    )


@pytest.mark.unit
def test_relay_signature_rejects_tampering(tmp_path):
    relay_script = Path('relay.py')
    tampered_copy = tmp_path / 'relay.py'
    tampered_copy.write_bytes(relay_script.read_bytes() + b"\n# tampered\n")
    signature_file = Path('config/signing/relay.py.sig')
    public_key_file = Path('config/signing/relay_signing_public_key.pem')

    assert not relay_signature.verify_file_signature(
        tampered_copy,
        signature_file,
        public_key_file,
    )
