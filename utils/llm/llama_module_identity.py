"""Privacy-preserving llama_cpp module identity helpers."""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Optional

LLAMA_MODULE_IDENTITY_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
LLAMA_MODULE_IDENTITY_DOMAIN = "token.place.llama_cpp.module_path.v1"


def strip_windows_extended_path_prefix(path_text: str) -> str:
    if path_text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path_text[8:]
    if path_text.startswith("\\\\?\\"):
        return path_text[4:]
    return path_text


def canonical_llama_module_identity_input(module_path: Any) -> Optional[str]:
    if not module_path:
        return None
    try:
        raw_path = str(module_path).strip()
        if raw_path.lower() in {'missing', 'unknown'}:
            return None
        path_text = strip_windows_extended_path_prefix(raw_path)
    except (TypeError, ValueError, OSError):
        return None
    try:
        canonical = os.path.normcase(os.path.normpath(os.path.realpath(os.path.abspath(path_text))))
    except (TypeError, ValueError, OSError):
        try:
            canonical = os.path.normcase(os.path.normpath(path_text))
        except (TypeError, ValueError, OSError):
            return None
    return canonical.replace('\\', '/')


def llama_module_identity_from_path(module_path: Any) -> Optional[str]:
    canonical = canonical_llama_module_identity_input(module_path)
    if not canonical or canonical in {'missing', 'unknown'}:
        return None
    digest = hashlib.sha256(f"{LLAMA_MODULE_IDENTITY_DOMAIN}\0{canonical}".encode('utf-8')).hexdigest()
    return f"sha256:{digest}"


def valid_llama_module_identity(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if LLAMA_MODULE_IDENTITY_RE.fullmatch(text) else None


def llama_module_identity_supplied(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ''
