from pathlib import Path

import pytest

from utils import signing
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


@pytest.mark.unit
def test_signing_package_exposes_relay_attributes():
    # Accessing these attributes exercises the ``__getattr__`` proxy on the
    # package and ensures that ``__dir__`` contains the public API.
    assert "relay_signature" in signing.__dir__()
    assert signing.DEFAULT_PUBLIC_KEY_PATH == relay_signature.DEFAULT_PUBLIC_KEY_PATH
    assert signing.verify_file_signature is relay_signature.verify_file_signature


@pytest.mark.unit
def test_signing_package_rejects_unknown_attributes():
    with pytest.raises(AttributeError):
        _ = signing.does_not_exist  # type: ignore[attr-defined]


@pytest.mark.unit
def test_load_signature_rejects_invalid_base64(tmp_path):
    signature_path = tmp_path / "invalid.sig"
    signature_path.write_text("!!!not-base64!!!")

    with pytest.raises(ValueError):
        relay_signature._load_signature(signature_path)


@pytest.mark.unit
def test_cli_reports_success(capsys, tmp_path):
    artifact = Path("relay.py")
    signature = Path("config/signing/relay.py.sig")
    public_key = Path("config/signing/relay_signing_public_key.pem")

    exit_code = relay_signature.main(
        [str(artifact), str(signature), "--public-key", str(public_key)]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Signature OK for" in captured.out
    assert captured.err == ""


@pytest.mark.unit
def test_cli_reports_failure(capsys, tmp_path):
    artifact = Path("relay.py")
    tampered_copy = tmp_path / "relay.py"
    tampered_copy.write_bytes(artifact.read_bytes() + b"\ntampered\n")
    signature = Path("config/signing/relay.py.sig")

    exit_code = relay_signature.main([str(tampered_copy), str(signature)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Signature verification FAILED" in captured.out
    assert captured.err == ""
