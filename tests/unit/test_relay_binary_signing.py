import base64
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from utils import signing
from utils.signing import relay_signature


def _write_signed_artifact(tmp_path: Path):
    artifact = tmp_path / "relay.py"
    artifact.write_text("print(\"relay\")\n", encoding="utf-8")

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    signature_bytes = private_key.sign(artifact.read_bytes())
    signature_path = tmp_path / "relay.py.sig"
    signature_path.write_bytes(base64.b64encode(signature_bytes) + b"\n")

    public_key_path = tmp_path / "relay_signing_public_key.pem"
    public_key_path.write_bytes(
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    return artifact, signature_path, public_key_path


@pytest.mark.unit
def test_relay_release_signature_verifies(tmp_path):
    relay_script, signature_file, public_key_file = _write_signed_artifact(tmp_path)

    assert relay_signature.verify_file_signature(
        relay_script,
        signature_file,
        public_key_file,
    )


@pytest.mark.unit
def test_relay_signature_rejects_tampering(tmp_path):
    relay_script, signature_file, public_key_file = _write_signed_artifact(tmp_path)
    tampered_copy = tmp_path / 'relay_tampered.py'
    tampered_copy.write_bytes(relay_script.read_bytes() + b"\n# tampered\n")

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
    artifact, signature, public_key = _write_signed_artifact(tmp_path)

    exit_code = relay_signature.main(
        [str(artifact), str(signature), "--public-key", str(public_key)]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Signature OK for" in captured.out
    assert captured.err == ""


@pytest.mark.unit
def test_cli_reports_failure(capsys, tmp_path):
    artifact, signature, public_key = _write_signed_artifact(tmp_path)
    tampered_copy = tmp_path / "relay_tampered.py"
    tampered_copy.write_bytes(artifact.read_bytes() + b"\ntampered\n")

    exit_code = relay_signature.main([str(tampered_copy), str(signature), "--public-key", str(public_key)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Signature verification FAILED" in captured.out
    assert captured.err == ""
