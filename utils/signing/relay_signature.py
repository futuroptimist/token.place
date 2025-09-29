"""Helpers for verifying signed relay releases."""

from __future__ import annotations

import argparse
import base64
import binascii
from pathlib import Path
from typing import Iterable, Union

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

DEFAULT_PUBLIC_KEY_PATH = Path("config/signing/relay_signing_public_key.pem")

PathLike = Union[str, Path]


def load_public_key(path: PathLike = DEFAULT_PUBLIC_KEY_PATH) -> Ed25519PublicKey:
    """Load the relay signing public key from disk.

    Args:
        path: Path to a PEM-encoded Ed25519 public key file.

    Returns:
        An :class:`Ed25519PublicKey` instance ready for signature verification.

    Raises:
        ValueError: If the file does not contain an Ed25519 public key.
    """

    pem_bytes = Path(path).read_bytes()
    public_key = serialization.load_pem_public_key(pem_bytes)
    if not isinstance(public_key, Ed25519PublicKey):  # pragma: no cover - defensive guard
        raise ValueError("Relay signing key must be an Ed25519 public key")
    return public_key


def _load_signature(signature_path: PathLike) -> bytes:
    """Read and decode a base64-encoded signature from disk."""

    raw = Path(signature_path).read_bytes().strip()
    try:
        return base64.b64decode(raw, validate=True)
    except binascii.Error as exc:  # pragma: no cover - invalid signature encoding
        raise ValueError("Relay signature files must contain base64-encoded data") from exc


def verify_signature_bytes(
    artifact_bytes: bytes,
    signature: bytes,
    public_key: Ed25519PublicKey,
) -> bool:
    """Verify that *signature* matches *artifact_bytes* using *public_key*."""

    try:
        public_key.verify(signature, artifact_bytes)
    except InvalidSignature:
        return False
    return True


def verify_file_signature(
    artifact_path: PathLike,
    signature_path: PathLike,
    public_key_path: PathLike = DEFAULT_PUBLIC_KEY_PATH,
) -> bool:
    """Verify a relay artifact signature using the bundled public key.

    Args:
        artifact_path: Path to the signed relay artifact (e.g., ``relay.py`` or an executable).
        signature_path: Path to the base64-encoded Ed25519 signature file.
        public_key_path: Optional override for the public key location.

    Returns:
        ``True`` if the signature is valid for the artifact, otherwise ``False``.
    """

    public_key = load_public_key(public_key_path)
    signature = _load_signature(signature_path)
    artifact_bytes = Path(artifact_path).read_bytes()
    return verify_signature_bytes(artifact_bytes, signature, public_key)


def _format_cli_paths(path: PathLike) -> str:
    return str(Path(path))


def main(argv: Iterable[str] | None = None) -> int:
    """CLI entry point for signature verification."""

    parser = argparse.ArgumentParser(description="Verify token.place relay release signatures")
    parser.add_argument(
        "artifact",
        type=Path,
        help="Path to the relay artifact (e.g. relay.py or a packaged binary)",
    )
    parser.add_argument(
        "signature",
        type=Path,
        nargs="?",
        default=Path("config/signing/relay.py.sig"),
        help="Path to the accompanying .sig file (default: config/signing/relay.py.sig)",
    )
    parser.add_argument(
        "--public-key",
        type=Path,
        default=DEFAULT_PUBLIC_KEY_PATH,
        help="Override the public key used for verification",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    ok = verify_file_signature(args.artifact, args.signature, args.public_key)
    if ok:
        print(
            "Signature OK for",
            _format_cli_paths(args.artifact),
            "using",
            _format_cli_paths(args.signature),
        )
        return 0

    print(
        "Signature verification FAILED for",
        _format_cli_paths(args.artifact),
        "using",
        _format_cli_paths(args.signature),
    )
    return 1


if __name__ == "__main__":  # pragma: no cover - manual invocation
    raise SystemExit(main())
