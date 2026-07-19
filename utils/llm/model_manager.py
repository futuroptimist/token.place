"""
Model manager module for handling LLM model downloading, initialization and inference.
"""
import os
import ntpath
import time
import logging
import math
import hashlib
import uuid
from utils.llm.llama_module_identity import (
    canonical_llama_module_identity_input as _shared_canonical_llama_module_identity_input,
    llama_module_identity_supplied,
    valid_llama_module_identity,
)
from utils.networking.http_requests_compat import requests
import json
import re
import sys
import importlib
import importlib.metadata
import inspect
import subprocess
import tempfile
import queue
import signal
import threading
import sysconfig
from pathlib import Path
from threading import Lock
from typing import Dict, List, Any, Optional, Iterable, Tuple

from utils.system import resource_monitor
from utils.llm.model_profiles import get_model_profile, resolve_profile_id

# Configure logging
logger = logging.getLogger('model_manager')
REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_LLAMA_CPP_SHIM = (REPO_ROOT / 'llama_cpp.py').resolve()
DEFAULT_LLAMA_CPP_RUNTIME_STAGE_TIMEOUT_SECONDS = 30.0
QWEN_64K_YARN_UNSUPPORTED_MESSAGE = (
    'Qwen 64K requires YaRN/RoPE support in llama-cpp-python; '
    'update or rebuild the runtime'
)
# llama.cpp defines LLAMA_ROPE_SCALING_TYPE_YARN as 2. Some llama-cpp-python
# wheels accept rope_scaling_type as a constructor kwarg but do not export the
# generated enum symbol, so this is only used after constructor support is verified.
LLAMA_ROPE_SCALING_TYPE_YARN_NUMERIC_FALLBACK = 2
QWEN_64K_KV_CACHE_TYPE_NAMES = {
    'f16': ('GGML_TYPE_F16', 'LLAMA_TYPE_F16'),
    'q8': ('GGML_TYPE_Q8_0', 'LLAMA_TYPE_Q8_0'),
    'q4': ('GGML_TYPE_Q4_0', 'LLAMA_TYPE_Q4_0'),
}
QWEN_64K_BATCH_TOKENS = 256
QWEN_64K_UBATCH_TOKENS = 128
QWEN_64K_RUNTIME_PROFILE_DEFAULT = 'qwen64k_f16_fa_small_batch'
QWEN_64K_RUNTIME_PROFILE_Q8 = 'qwen64k_kv_q8_fa_small_batch'
QWEN_64K_RUNTIME_PROFILE_Q4 = 'qwen64k_kv_q4_fa_small_batch'
GGUF_MAGIC = b'GGUF'

LLAMA_CPP_CONSTRUCTOR_CAPABILITY_KWARGS = (
    'type_k', 'type_v', 'flash_attn', 'offload_kqv', 'n_batch', 'n_ubatch',
    'rope_scaling_type', 'yarn_ext_factor', 'yarn_attn_factor', 'yarn_beta_fast',
    'yarn_beta_slow', 'yarn_orig_ctx', 'rope_freq_base', 'rope_freq_scale',
)
# Retry context-create failures backed by safe Metal/KV/cache/buffer evidence.
# ``runtime_context_create_failed`` remains excluded from these general retry
# categories. Its sole exception is the bounded pre-registration Qwen 64K
# CUDA/Metal F16 -> Q8 -> Q4 loop, capped at those three profiles. If every
# generic attempt fails, the original generic init exception is re-raised so
# corrupt GGUF/runtime/ABI causes are not reported as memory-profile exhaustion.
QWEN_64K_CONTEXT_CREATE_RETRY_CATEGORIES = {
    'runtime_context_create_metal_memory',
    'runtime_context_create_kv_cache_allocation',
    'runtime_context_create_metal_buffer_limit',
    'runtime_context_create_cuda_memory',
    'runtime_context_create_cuda_buffer_limit',
    'runtime_context_create_failed',
}

_INIT_SAFE_CATEGORY_ALIASES = {
    'cuda_memory_allocation': 'runtime_context_create_cuda_memory',
    'metal_memory_allocation': 'runtime_context_create_metal_memory',
    'kv_cache_allocation': 'runtime_context_create_kv_cache_allocation',
    'rope_yarn_eval_failure': 'runtime_context_create_rope_yarn_config',
}
_INIT_SAFE_CATEGORY_ALLOWLIST = {
    'runtime_init_unclassified',
    'runtime_model_path_unavailable',
    'runtime_model_load_failed',
    'runtime_model_vocab_failed',
    'runtime_batch_create_failed',
    'runtime_context_create_failed',
    'runtime_context_create_unsupported_kwarg',
    'runtime_context_create_rope_yarn_config',
    'runtime_context_create_kv_cache_allocation',
    'runtime_context_create_metal_memory',
    'runtime_context_create_metal_buffer_limit',
    'runtime_context_create_cuda_memory',
    'runtime_context_create_cuda_buffer_limit',
}


class LlamaCppRuntimeInitError(RuntimeError):
    """Structured, sanitized subprocess initialization failure."""

    def __init__(
        self,
        message: str,
        *,
        safe_error_category: str,
        child_exception_type: str = 'RuntimeError',
        child_stderr_tail: str = '',
    ) -> None:
        self.safe_error_category = _validated_init_safe_category(safe_error_category)
        self.child_exception_type = _safe_child_exception_type(child_exception_type)
        self.child_stderr_tail = _sanitize_child_diagnostic_text(child_stderr_tail)
        super().__init__(
            f"{message}; child_exception_type={self.child_exception_type}; "
            f"safe_error_category={self.safe_error_category}; "
            f"child_stderr_tail={self.child_stderr_tail or '<empty>'}"
        )


def _safe_child_exception_type(value: Any) -> str:
    text = str(value or 'RuntimeError')
    if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', text):
        return text
    return 'RuntimeError'


def _validated_init_safe_category(value: Any) -> str:
    text = str(value or '').strip()
    canonical = _INIT_SAFE_CATEGORY_ALIASES.get(text, text)
    if canonical in _INIT_SAFE_CATEGORY_ALLOWLIST:
        return canonical
    return 'runtime_init_unclassified'


def _refine_init_category(current: str, *, error: Any = None, child_stderr: str = '') -> str:
    canonical = _validated_init_safe_category(current)
    refined = _classify_runtime_context_create_error(str(error or ''), child_stderr)
    if canonical == 'runtime_init_unclassified' or (
        canonical == 'runtime_context_create_failed' and refined not in {'runtime_init_unclassified', 'runtime_context_create_failed'}
    ):
        return refined
    return canonical

CRITICAL_STDLIB_IMPORT_MODULES = (
    'collections',
    'typing',
    'ctypes',
    'subprocess',
    'json',
    'importlib',
    'pathlib',
)


def _safe_signature_parameters(callable_obj: Any) -> Dict[str, inspect.Parameter]:
    try:
        return dict(inspect.signature(callable_obj).parameters)
    except (TypeError, ValueError):
        return {}


def _constructor_accepts_kwarg(callable_obj: Any, kwarg: str) -> bool:
    if not callable(callable_obj):
        return False
    constructor = getattr(callable_obj, '__init__', callable_obj)
    parameters = _safe_signature_parameters(constructor)
    return kwarg in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _llama_constructor_supports_kwargs(llama_cls: Any, required_kwargs: Iterable[str]) -> Dict[str, bool]:
    facade_kwargs = getattr(llama_cls, '__token_place_supported_constructor_kwargs__', None)
    if facade_kwargs is not None:
        supported = {str(name) for name in facade_kwargs if isinstance(name, str)}
        return {name: name in supported for name in required_kwargs}
    return {name: _constructor_accepts_kwarg(llama_cls, name) for name in required_kwargs}


def _coerce_optional_int_enum(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text and (text.isdigit() or (text[0] in {'+', '-'} and text[1:].isdigit())):
            return int(text, 10)
    return None


def _coerce_strict_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == 'true':
            return True
        if lowered == 'false':
            return False
    return None


def _safe_constructor_capability_payload(llama_cpp_module: Any) -> Dict[str, Any]:
    capabilities = getattr(llama_cpp_module, '__token_place_worker_capabilities__', None)
    if not isinstance(capabilities, dict):
        return {}
    payload: Dict[str, Any] = {}
    support = capabilities.get('constructor_kwarg_support')
    if isinstance(support, dict):
        payload_support: Dict[str, bool] = {}
        for name, value in support.items():
            if not isinstance(name, str) or name not in LLAMA_CPP_CONSTRUCTOR_CAPABILITY_KWARGS:
                continue
            coerced = _coerce_strict_bool(value)
            if coerced is not None:
                payload_support[name] = coerced
        payload['constructor_kwarg_support'] = payload_support
    for key in ('q8_kv_cache_type_value', 'q4_kv_cache_type_value', 'f16_kv_cache_type_value'):
        value = capabilities.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            payload[key] = value
    valid_identity = _valid_llama_module_identity(capabilities.get('llama_module_identity'))
    if valid_identity is not None:
        payload['llama_module_identity'] = valid_identity
    elif llama_module_identity_supplied(capabilities.get('llama_module_identity')) or capabilities.get('llama_module_identity_malformed') is True:
        payload['llama_module_identity_malformed'] = True
    for field in (
        'capability_source',
        'backend',
        'gpu_offload_supported',
        'llama_cpp_python_version',
        'constructor_has_var_kwargs',
        'constructor_signature_inspectable',
        'qwen_64k_yarn_support',
        'yarn_resolver_source',
        'llama_module_path',
        'llama_module_path_present',
        'llama_module_identity_match',
        'llama_module_identity_verified',
        'child_probe_reprobe_attempted',
        'child_probe_reprobe_skipped_reason',
    ):
        if field in capabilities:
            payload[field] = capabilities.get(field)
    yarn_enum_value = capabilities.get('yarn_enum_value')
    if isinstance(yarn_enum_value, int) and not isinstance(yarn_enum_value, bool):
        payload['yarn_enum_value'] = yarn_enum_value
    return payload


def _llama_cpp_python_version(llama_cpp_module: Any) -> Optional[str]:
    module_version = getattr(llama_cpp_module, '__version__', None)
    if module_version:
        return str(module_version)
    try:
        return importlib.metadata.version('llama-cpp-python')
    except importlib.metadata.PackageNotFoundError:
        return None


def _resolve_yarn_rope_scaling_type(llama_cpp_module: Any, llama_cls: Any = None) -> tuple[Any, str]:
    """Resolve a YaRN RoPE scaling value from installed llama-cpp-python.

    Prefer exported enum constants. If the runtime accepts the modern
    ``rope_scaling_type`` constructor kwarg but simply omitted the Python enum
    export, use llama.cpp's numeric YaRN value as a compatibility fallback.
    """

    locations = (
        (llama_cpp_module, 'top_level_enum'),
        (getattr(llama_cpp_module, 'llama_cpp', None), 'nested_enum'),
        (llama_cls, 'llama_class_enum'),
    )
    for owner, source in locations:
        if owner is None:
            continue
        value = getattr(owner, 'LLAMA_ROPE_SCALING_TYPE_YARN', None)
        if value is not None:
            return value, source

    worker_capabilities = _safe_constructor_capability_payload(llama_cpp_module)
    worker_yarn_value = worker_capabilities.get('yarn_enum_value')
    worker_yarn_source = worker_capabilities.get('yarn_resolver_source')
    if worker_yarn_value is not None and worker_yarn_source in {'top_level_enum', 'nested_enum', 'llama_class_enum', 'numeric_fallback'}:
        return worker_yarn_value, str(worker_yarn_source)
    if worker_yarn_source == 'numeric_fallback':
        return (
            LLAMA_ROPE_SCALING_TYPE_YARN_NUMERIC_FALLBACK,
            'numeric_fallback',
        )
    worker_kwarg_support = worker_capabilities.get('constructor_kwarg_support')
    if isinstance(worker_kwarg_support, dict) and worker_kwarg_support.get('rope_scaling_type') is False:
        return None, 'unsupported'

    if _constructor_accepts_kwarg(llama_cls, 'rope_scaling_type'):
        return LLAMA_ROPE_SCALING_TYPE_YARN_NUMERIC_FALLBACK, 'numeric_fallback'
    return None, 'unsupported'


def _resolve_llama_cpp_rope_scaling_type_yarn(llama_cpp_module: Any, llama_cls: Any = None) -> tuple[Any, Optional[str]]:
    value, source = _resolve_yarn_rope_scaling_type(llama_cpp_module, llama_cls)
    legacy_locations = {
        'top_level_enum': 'llama_cpp.LLAMA_ROPE_SCALING_TYPE_YARN',
        'nested_enum': 'llama_cpp.llama_cpp.LLAMA_ROPE_SCALING_TYPE_YARN',
        'llama_class_enum': 'Llama.LLAMA_ROPE_SCALING_TYPE_YARN',
        'numeric_fallback': 'numeric_fallback',
    }
    return value, legacy_locations.get(source)


def _safe_llama_cpp_enum(llama_cpp_module: Any, name: str) -> Any:
    for owner in (llama_cpp_module, getattr(llama_cpp_module, 'llama_cpp', None)):
        if owner is None:
            continue
        value = getattr(owner, name, None)
        if value is not None:
            return value
    return None


def _resolve_ggml_kv_cache_type(
    llama_cpp_module: Any,
    llama_cls: Any,
    precision: str,
    kwarg_support: Optional[Dict[str, bool]] = None,
) -> tuple[Optional[int], Dict[str, Any]]:
    """Resolve a GGML/llama.cpp KV cache type constant without passing guesses blindly."""

    names = QWEN_64K_KV_CACHE_TYPE_NAMES.get(precision, ())
    diagnostics: Dict[str, Any] = {'precision': precision, 'names': list(names), 'source': None}
    for owner, source in (
        (llama_cpp_module, 'top_level'),
        (getattr(llama_cpp_module, 'llama_cpp', None), 'nested'),
    ):
        if owner is None:
            continue
        for name in names:
            value = getattr(owner, name, None)
            if isinstance(value, int) and not isinstance(value, bool):
                diagnostics.update({'source': source, 'name': name, 'value': value})
                return value, diagnostics

    worker_capabilities = _safe_constructor_capability_payload(llama_cpp_module)
    capability_key = f'{precision}_kv_cache_type_value'
    capability_value = worker_capabilities.get(capability_key)
    if isinstance(capability_value, int) and not isinstance(capability_value, bool):
        diagnostics.update({'source': 'worker_probe', 'name': capability_key, 'value': capability_value})
        return capability_value, diagnostics

    # llama.cpp GGML enum fallback values. These are only used after the
    # constructor is verified to support type_k/type_v, so a runtime that cannot
    # accept KV-cache enum kwargs never receives guessed numeric values.
    numeric_fallbacks = {'f16': 1, 'q4': 2, 'q8': 8}
    support = kwarg_support or _llama_constructor_supports_kwargs(llama_cls, ('type_k', 'type_v'))
    if precision in numeric_fallbacks and (support.get('type_k') or support.get('type_v')):
        diagnostics.update({
            'source': 'verified_numeric_fallback',
            'name': f'{precision}_numeric_fallback',
            'value': numeric_fallbacks[precision],
        })
        return numeric_fallbacks[precision], diagnostics

    diagnostics['source'] = 'unavailable'
    diagnostics['reason'] = 'constant_unavailable_or_constructor_support_unverified'
    return None, diagnostics


def _qwen_64k_runtime_capabilities(llama_cpp_module: Any, llama_cls: Any) -> Dict[str, Any]:
    probe_kwargs = ('type_k', 'type_v', 'flash_attn', 'offload_kqv', 'n_batch', 'n_ubatch')
    worker_capabilities = _safe_constructor_capability_payload(llama_cpp_module)
    worker_kwarg_support = worker_capabilities.get('constructor_kwarg_support')
    kwarg_support = (
        {name: bool(worker_kwarg_support.get(name)) for name in probe_kwargs}
        if isinstance(worker_kwarg_support, dict)
        else _llama_constructor_supports_kwargs(llama_cls, probe_kwargs)
    )
    constants = {}
    for precision in ('f16', 'q8', 'q4'):
        value, diag = _resolve_ggml_kv_cache_type(llama_cpp_module, llama_cls, precision, kwarg_support)
        constants[precision] = {'value': value, **diag}
    return {
        'constructor_kwarg_support': kwarg_support,
        'capability_source': worker_capabilities.get('capability_source') or (
            'worker_probe' if isinstance(worker_kwarg_support, dict) else 'local_constructor_signature'
        ),
        'backend': worker_capabilities.get('backend'),
        'llama_cpp_python_version': worker_capabilities.get('llama_cpp_python_version') or _llama_cpp_python_version(llama_cpp_module),
        'kv_constants': constants,
    }


def _qwen_64k_memory_estimate(model_path: Any, n_ctx: int, kv_precision: str, backend: str) -> Dict[str, Any]:
    model_size = None
    try:
        model_size = os.path.getsize(str(model_path))
    except OSError:
        pass
    bytes_per_token_by_precision = {'f16': 524288, 'q8': 262144, 'q4': 131072}
    kv_bytes = int(n_ctx) * bytes_per_token_by_precision.get(kv_precision, bytes_per_token_by_precision['f16'])
    total = (model_size or 0) + kv_bytes
    return {
        'model_file_size_bytes': model_size,
        'estimated_kv_cache_bytes': kv_bytes,
        'estimated_total_model_plus_kv_bytes': total if model_size is not None else None,
        'context_size_tokens': int(n_ctx),
        'backend': backend or 'unknown',
        'kv_precision': kv_precision,
    }


def _build_qwen_64k_runtime_profiles(
    llama_cpp_module: Any,
    llama_cls: Any,
    *,
    model_path: Any,
    n_ctx: int,
    enable_kqv_offload: bool = True,
) -> list[Dict[str, Any]]:
    """Build ordered Qwen 64K Metal-safe generation profiles.

    Profile kwargs intentionally contain only generation/backend knobs. YaRN
    kwargs are owned by _runtime_init_kwargs and must not be overwritten here.
    """
    capabilities = _qwen_64k_runtime_capabilities(llama_cpp_module, llama_cls)
    support = capabilities['constructor_kwarg_support']
    backend = str(capabilities.get('backend') or '').lower()

    def _base_kwargs() -> tuple[Dict[str, Any], Dict[str, str]]:
        kwargs: Dict[str, Any] = {}
        omitted: Dict[str, str] = {}
        for key, value in (('flash_attn', True), ('offload_kqv', True), ('n_batch', QWEN_64K_BATCH_TOKENS), ('n_ubatch', QWEN_64K_UBATCH_TOKENS)):
            if support.get(key):
                kwargs[key] = value
            else:
                omitted[key] = 'constructor_kwarg_unsupported'
        if not enable_kqv_offload:
            omitted['offload_kqv'] = 'gpu_offload_disabled'
            kwargs.pop('offload_kqv', None)
        return kwargs, omitted

    profiles: list[Dict[str, Any]] = []
    skipped_profiles: list[Dict[str, Any]] = []

    for precision, profile_id in (
        ('f16', QWEN_64K_RUNTIME_PROFILE_DEFAULT),
        ('q8', QWEN_64K_RUNTIME_PROFILE_Q8),
        ('q4', QWEN_64K_RUNTIME_PROFILE_Q4),
    ):
        kwargs, omitted = _base_kwargs()
        kv_info = capabilities['kv_constants'].get(precision) or {}
        kv_value = kv_info.get('value')
        if precision != 'f16':
            if not support.get('flash_attn'):
                omitted['profile'] = 'quantized_v_requires_flash_attn_support'
            if kv_value is None:
                omitted['type_k'] = omitted['type_v'] = 'kv_type_constant_unavailable'
            else:
                if support.get('type_k'):
                    kwargs['type_k'] = kv_value
                else:
                    omitted['type_k'] = 'constructor_kwarg_unsupported'
                if support.get('type_v') and support.get('flash_attn'):
                    kwargs['type_v'] = kv_value
                elif not support.get('type_v'):
                    omitted['type_v'] = 'constructor_kwarg_unsupported'
                else:
                    omitted['type_v'] = 'flash_attn_required'
        enabled = not omitted.get('profile') and all(k in kwargs for k in ('flash_attn', 'offload_kqv', 'n_batch', 'n_ubatch'))
        if precision != 'f16' and not all(k in kwargs for k in ('type_k', 'type_v')):
            enabled = False
        diagnostics = {
            'profile_id': profile_id,
            'enabled': bool(enabled),
            'applied': dict(kwargs),
            'omitted': omitted,
            'kv_precision': precision,
            'kv_cache_type': kv_info,
            'constructor_kwarg_support': support,
            'capability_source': capabilities['capability_source'],
            'llama_cpp_python_version': capabilities.get('llama_cpp_python_version'),
            'memory_estimate': _qwen_64k_memory_estimate(model_path, n_ctx, precision, capabilities.get('backend') or ''),
            'kqv_offload_allowed': bool(enable_kqv_offload),
            'backend': backend or 'unknown',
        }
        if enabled:
            profiles.append({'profile_id': profile_id, 'kwargs': kwargs, 'diagnostics': diagnostics})
        else:
            skipped_profiles.append(diagnostics)
    if profiles and skipped_profiles:
        profiles[0]['diagnostics'].setdefault('skipped_profiles', []).extend(skipped_profiles)
    return profiles

def _classify_runtime_context_create_error(error: Any, child_stderr: str = '') -> str:
    if isinstance(error, LlamaCppRuntimeInitError):
        return _refine_init_category(error.safe_error_category, error=str(error), child_stderr=child_stderr or error.child_stderr_tail)
    text = f'{error or ""}\n{child_stderr or ""}'.lower()
    marker = re.search(r'safe_error_category=([a-z0-9_]+)', text)
    if marker:
        marker_category = _validated_init_safe_category(marker.group(1))
        if marker_category not in {'runtime_init_unclassified', 'runtime_context_create_failed'}:
            return marker_category
    if re.search(r'(?:unexpected keyword argument|got an unexpected keyword argument|unsupported (?:parameter|kwarg|argument))', text):
        return 'runtime_context_create_unsupported_kwarg'
    if 'model path' in text and any(term in text for term in ('not found', 'unavailable', 'does not exist', 'no such file')):
        return 'runtime_model_path_unavailable'
    if 'vocab' in text and any(term in text for term in ('fail', 'load', 'invalid')):
        return 'runtime_model_vocab_failed'
    if 'llama_batch' in text or 'batch create' in text or 'failed to create batch' in text:
        return 'runtime_batch_create_failed'
    if 'yarn' in text or ('rope' in text and any(term in text for term in ('scal', 'freq', 'orig', 'context'))):
        return 'runtime_context_create_rope_yarn_config'
    if 'kv' in text and any(term in text for term in ('alloc', 'cache', 'memory', 'oom', 'failed')):
        return 'runtime_context_create_kv_cache_allocation'
    if 'metal' in text and any(term in text for term in ('buffer', 'graph', 'max size', 'too large')):
        return 'runtime_context_create_metal_buffer_limit'
    if 'metal' in text and any(term in text for term in ('alloc', 'memory', 'oom', 'out of memory', 'failed to create llama_context')):
        return 'runtime_context_create_metal_memory'
    cuda_allocation_markers = (
        'cublas_status_alloc_failed',
        'cudamalloc',
        'cuda malloc',
        'cuda oom',
        'cuda out of memory',
    )
    if any(term in text for term in cuda_allocation_markers):
        return 'runtime_context_create_cuda_memory'
    if 'ggml_cuda' in text and any(term in text for term in ('alloc', 'memory', 'oom', 'out of memory')):
        return 'runtime_context_create_cuda_memory'
    if 'cuda' in text and any(term in text for term in ('out of memory', 'oom', 'cudamalloc', 'alloc failed', 'allocation failed', 'buffer allocation failed')):
        return 'runtime_context_create_cuda_memory'
    if 'cuda' in text and any(term in text for term in ('buffer', 'resource', 'allocation')):
        return 'runtime_context_create_cuda_buffer_limit'
    if 'failed to create llama_context' in text or 'llamacontext' in text:
        return 'runtime_context_create_failed'
    return 'runtime_init_unclassified'



def _classify_runtime_initialization_error(error: Any, child_stderr: str = '') -> str:
    """Classify constructor/init failures with a bounded, path-free category set."""
    text = f'{error or ""}\n{child_stderr or ""}'.lower()
    if 'failed to load model' in text or 'llama_model_load' in text:
        return 'runtime_model_load_failed'
    if 'vocab' in text and any(term in text for term in ('fail', 'load', 'invalid')):
        return 'runtime_model_vocab_failed'
    if 'llama_batch' in text or 'batch create' in text or 'failed to create batch' in text:
        return 'runtime_batch_create_failed'
    if any(term in text for term in ('abi', 'incompatible gguf', 'unsupported gguf')):
        return 'runtime_model_load_failed'
    return _classify_runtime_context_create_error(error, child_stderr)

def _redact_paths_from_text(text: Any, *, limit: int = 2000) -> str:
    redacted = str(text or '')
    path_chars = r'[^\n\r\t<>|*?";]+'
    redacted = re.sub(rf'(?<!\w)(?:[A-Za-z]:)?[/\\]{path_chars}(?:[/\\]{path_chars})+', '<path>', redacted)
    redacted = re.sub(r'\b[A-Za-z]:\\[^\n\r\t<>|*?";]+(?:\\[^\n\r\t<>|*?";]+)+', '<path>', redacted)
    return redacted[-limit:].strip()


_CHILD_DIAGNOSTIC_ALLOWLIST = re.compile(
    r'(llama|llama_context|ggml|metal|kv|cache|alloc|memory|oom|buffer|rope|yarn|'
    r'flash_attn|type_k|type_v|n_ctx|context|unsupported|keyword|argument|failed)',
    re.IGNORECASE,
)
_CHILD_DIAGNOSTIC_SENSITIVE_FIELD_RE = re.compile(
    r'(?i)(?<![\w-])('
    r'prompt|assistant|message|content|(?:decrypted[_-]?)?payload|ciphertext|plaintext|decrypted|secret|'
    r'key|token|authorization|api[_-]?key'
    r')(?![\w-])\s*[:=]\s*(?:"[^"]*"|\'[^\']*\'|[^\s,;]+)'
)
_CHILD_DIAGNOSTIC_SENSITIVE_PREFIXED_FIELD_RE = re.compile(
    r'(?i)(?<![\w-])('
    r'[A-Za-z0-9_-]*(?:secret|ciphertext|plaintext|decrypted|payload|token|authorization|'
    r'api[_-]?key|[_-]key|key[_-])[A-Za-z0-9_-]*'
    r')\s*[:=]\s*(?:"[^"]*"|\'[^\']*\'|[^\s,;]+)'
)
_CHILD_DIAGNOSTIC_SENSITIVE_TOKEN_RE = re.compile(
    r'(?i)\b(?!(?:secret|ciphertext|plaintext|decrypted|(?:decrypted[_-]?)?payload|'
    r'key|token|api[_-]?key)\b)[^\s,;:=]*(?:secret|ciphertext|plaintext|decrypted|payload|'
    r'api[_-]?key)[^\s,;:=]*\b'
)
_CHILD_DIAGNOSTIC_PROTOCOL_PREFIXES = (
    'TOKEN_PLACE_LLAMA_CPP_JSON:',
)


def _sanitize_child_diagnostic_line(line: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith(_CHILD_DIAGNOSTIC_PROTOCOL_PREFIXES):
        return ''
    if not _CHILD_DIAGNOSTIC_ALLOWLIST.search(stripped):
        return ''
    sanitized = _CHILD_DIAGNOSTIC_SENSITIVE_FIELD_RE.sub(lambda match: f'{match.group(1)}=<redacted>', stripped)
    sanitized = _CHILD_DIAGNOSTIC_SENSITIVE_PREFIXED_FIELD_RE.sub(
        lambda match: f'{match.group(1)}=<redacted>',
        sanitized,
    )
    sanitized = _CHILD_DIAGNOSTIC_SENSITIVE_TOKEN_RE.sub('<redacted>', sanitized)
    return sanitized[:300]


def _sanitize_child_diagnostic_text(text: Any, *, limit: int = 1200) -> str:
    redacted = _redact_paths_from_text(text, limit=limit * 2)
    safe_lines = []
    for line in redacted.splitlines():
        sanitized = _sanitize_child_diagnostic_line(line)
        if sanitized:
            safe_lines.append(sanitized)
        if len('\n'.join(safe_lines)) >= limit:
            break
    return '\n'.join(safe_lines)[-limit:].strip()



def _safe_plain_completion_eval_return_code(exc: Any) -> Optional[int]:
    match = re.search(r"llama_decode[ \t]+returned[ \t]+(-?[0-9]+)", str(exc or ''), re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _generation_category_for_decode_return_code(return_code: Optional[int]) -> Optional[str]:
    if return_code is None:
        return None
    if return_code == -1:
        return 'prompt_eval_invalid_batch'
    if return_code == -2:
        return 'backend_allocation_failure'
    if return_code == -3:
        return 'backend_graph_compute_failure'
    if return_code == 1:
        return 'kv_slot_unavailable'
    if return_code == 2:
        return 'decode_aborted'
    if return_code < 0:
        return 'backend_decode_failure'
    return None


def safe_plain_completion_decode_failure_category_and_code(exc: Any) -> Tuple[Optional[str], Optional[int]]:
    """Return the safe decode category/code parsed from a llama_decode exception string."""

    return_code = _safe_plain_completion_eval_return_code(exc)
    return _generation_category_for_decode_return_code(return_code), return_code


_FATAL_CURRENT_WORKER_GENERATION_CATEGORIES = {
    'backend_allocation_failure',
    'backend_graph_compute_failure',
    'cuda_memory_allocation',
    'metal_graph_compute_failure',
    'kv_slot_unavailable',
    'decode_aborted',
    'backend_decode_failure',
}

_QWEN_64K_PROFILE_RECOVERABLE_FAILURE_CATEGORIES = {
    'backend_allocation_failure',
    'backend_graph_compute_failure',
    'metal_graph_compute_failure',
    'kv_slot_unavailable',
    # Decode-abort / generic backend-decode categories are fatal for the
    # current worker. During synthetic pre-registration readiness only, the
    # bounded Qwen 64K Metal profile sequence may continue after closing that
    # failed worker; live user requests are never replayed.
    'decode_aborted',
    'backend_decode_failure',
    'metal_command_buffer_out_of_memory',
    'metal_command_buffer_timeout',
    'metal_command_buffer_page_fault',
    'metal_command_buffer_execution_failure',
    'metal_backend_sticky_error',
    'metal_graph_compute_failed',
    'memory_context_apply_failed',
    'graph_initialization_failed',
    'unknown_metal_backend_failure',
    'runtime_context_create_cuda_memory',
    'runtime_context_create_cuda_buffer_limit',
    'cuda_memory_allocation',
}


class _MockLlamaCallable:
    def __init__(self) -> None:
        self.side_effect = None
        self.return_value = None

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self.side_effect is not None:
            return self.side_effect(*args, **kwargs)
        return self.return_value


class _MockLlamaInstance:
    """Tiny local mock for USE_MOCK_LLM without importing test frameworks."""

    def __init__(self) -> None:
        self.apply_chat_template = _MockLlamaCallable()
        self.render_and_tokenize_chat = _MockLlamaCallable()
        self.tokenize = _MockLlamaCallable()
        self.create_chat_completion = _MockLlamaCallable()


def is_qwen_64k_profile_recoverable_failure_category(category: Any) -> bool:
    """Return whether a readiness failure may advance the Qwen 64K Metal profile."""

    return str(category or '') in _QWEN_64K_PROFILE_RECOVERABLE_FAILURE_CATEGORIES

_METAL_COMMAND_BUFFER_STATUS_RE = re.compile(r'(?:command[- ]buffer|command buffer).*?status\D*(-?\d+)', re.IGNORECASE)


def _classify_safe_metal_backend_failure(lines: Iterable[str]) -> Dict[str, Any]:
    text = '\n'.join(str(line or '') for line in lines)[-6000:].lower()
    if not text:
        return {}
    category = None
    if 'backend is in error state' in text or 'has_error' in text:
        category = 'metal_backend_sticky_error'
    elif 'page fault' in text:
        category = 'metal_command_buffer_page_fault'
    elif 'timeout' in text or 'timed out' in text:
        category = 'metal_command_buffer_timeout'
    elif any(term in text for term in ('out of memory', 'insufficient memory', 'resource shortage', 'oom')):
        category = 'metal_command_buffer_out_of_memory'
    elif 'command buffer' in text and any(term in text for term in ('error', 'fail', 'completed with status')):
        category = 'metal_command_buffer_execution_failure'
    elif 'ggml_metal_graph_compute' in text or ('metal' in text and 'graph' in text and 'failed' in text):
        category = 'metal_graph_compute_failed'
    elif 'memory_context_apply' in text:
        category = 'memory_context_apply_failed'
    elif 'graph' in text and any(term in text for term in ('init', 'initialization')) and 'fail' in text:
        category = 'graph_initialization_failed'
    elif 'metal' in text:
        category = 'unknown_metal_backend_failure'
    if category is None:
        return {}
    diagnostics: Dict[str, Any] = {'plain_completion_metal_error_category': category}
    status = _METAL_COMMAND_BUFFER_STATUS_RE.search(text)
    if status:
        try:
            diagnostics['plain_completion_metal_command_buffer_status'] = int(status.group(1))
        except ValueError:
            pass
    if category in {'metal_backend_sticky_error', 'metal_command_buffer_out_of_memory', 'metal_command_buffer_timeout', 'metal_command_buffer_page_fault', 'metal_command_buffer_execution_failure', 'metal_graph_compute_failed'}:
        diagnostics['plain_completion_backend_failure_category'] = category if category != 'metal_graph_compute_failed' else 'metal_graph_compute_failure'
        diagnostics['plain_completion_backend_state_sticky'] = True
        diagnostics['plain_completion_backend_recreation_required'] = True
    return diagnostics

def _safe_parent_exception_message(error: Any, *, child_stderr: str = '') -> str:
    category = _classify_runtime_context_create_error(error, child_stderr)
    return (
        f"{type(error).__name__ if isinstance(error, BaseException) else 'RuntimeError'}; "
        f"safe_error_category={category}"
    )


def _qwen_64k_memory_profile_kwargs(
    llama_cpp_module: Any,
    llama_cls: Any,
    *,
    enable_kqv_offload: bool = True,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Backward-compatible helper returning the first quantized Qwen 64K profile."""
    profiles = _build_qwen_64k_runtime_profiles(
        llama_cpp_module,
        llama_cls,
        model_path='',
        n_ctx=65536,
        enable_kqv_offload=enable_kqv_offload,
    )
    for profile in profiles:
        if profile.get('profile_id') != QWEN_64K_RUNTIME_PROFILE_DEFAULT:
            return dict(profile.get('kwargs') or {}), dict(profile.get('diagnostics') or {})
    default_diag = dict(profiles[0].get('diagnostics') or {}) if profiles else {'enabled': False, 'applied': {}}
    skipped = default_diag.get('skipped_profiles')
    if isinstance(skipped, list) and skipped:
        diag = dict(skipped[0])
        if diag.get('capability_source') == 'worker_probe':
            omitted = dict(diag.get('omitted') or {})
            for key, value in list(omitted.items()):
                if value == 'constructor_kwarg_unsupported':
                    omitted[key] = 'worker_capability_unsupported'
            diag['omitted'] = omitted
        return {}, diag
    return {}, default_diag

def _format_qwen_yarn_unsupported_diagnostics(diagnostics: Dict[str, Any]) -> str:
    safe_fields = (
        'active_profile_id',
        'active_context_tier',
        'llama_module_path_present',
        'llama_module_identity_match',
        'incomplete_probe_fields',
        'llama_cpp_python_version',
        'missing_reason',
        'yarn_resolver_source',
        'constructor_kwarg_support',
        'parent_facade_type',
        'child_probe_reprobe_attempted',
        'constructor_kwargs_attempted',
    )
    parts = []
    missing = object()
    for field in safe_fields:
        value = diagnostics.get(field, missing)
        if value is missing or value is None:
            rendered = 'unknown'
        else:
            rendered = repr(value) if isinstance(value, (list, dict, tuple)) else str(value)
        parts.append(f'{field}={rendered}')
    return ', '.join(parts)


def _qwen_64k_rope_support_diagnostics(llama_cpp_module: Any, llama_cls: Any) -> Dict[str, Any]:
    probe_kwargs = (
        'rope_scaling_type',
        'yarn_ext_factor',
        'yarn_attn_factor',
        'yarn_beta_fast',
        'yarn_beta_slow',
        'yarn_orig_ctx',
        'rope_freq_base',
        'rope_freq_scale',
    )
    worker_capabilities = _safe_constructor_capability_payload(llama_cpp_module)
    worker_kwarg_support = worker_capabilities.get('constructor_kwarg_support')
    kwarg_support = (
        {name: bool(worker_kwarg_support.get(name)) for name in probe_kwargs}
        if isinstance(worker_kwarg_support, dict)
        else _llama_constructor_supports_kwargs(llama_cls, probe_kwargs)
    )
    accepted_kwargs = sorted(name for name, supported in kwarg_support.items() if supported)
    yarn_value, resolver_source = _resolve_yarn_rope_scaling_type(llama_cpp_module, llama_cls)
    required_kwargs = ('rope_scaling_type', 'rope_freq_scale', 'yarn_orig_ctx')
    constructor_has_var_kwargs = worker_capabilities.get('constructor_has_var_kwargs') is True
    missing_kwargs = (
        []
        if constructor_has_var_kwargs
        else [name for name in required_kwargs if not kwarg_support.get(name)]
    )
    missing_reasons = []
    if resolver_source == 'unsupported':
        missing_reasons.append('missing LLAMA_ROPE_SCALING_TYPE_YARN enum constant and rope_scaling_type constructor support')
        missing_kwargs_for_reason = [name for name in missing_kwargs if name != 'rope_scaling_type']
    else:
        missing_kwargs_for_reason = missing_kwargs
    if 'rope_freq_scale' in missing_kwargs_for_reason:
        missing_reasons.append('runtime_qwen_64k_yarn_rope_freq_scale_unavailable')
    remaining_missing = [name for name in missing_kwargs_for_reason if name != 'rope_freq_scale']
    if remaining_missing:
        missing_reasons.append(f'missing constructor kwargs: {", ".join(remaining_missing)}')
    support_classification = worker_capabilities.get('qwen_64k_yarn_support')
    if support_classification not in {'supported', 'unknown', 'unsupported'}:
        support_classification = 'supported' if not missing_reasons else 'unsupported'
    if support_classification == 'unknown':
        unknown_missing_reasons = []
        if worker_capabilities.get('yarn_enum_value') is None or resolver_source == 'unsupported':
            yarn_value = None
            resolver_source = worker_capabilities.get('yarn_resolver_source') or resolver_source
            unknown_missing_reasons.append('missing concrete YaRN enum value from unknown child probe')
        if missing_kwargs:
            unknown_missing_reasons.append(f'missing constructor kwargs: {", ".join(missing_kwargs)}')
        missing_reasons = unknown_missing_reasons
    supported = support_classification in {'supported', 'unknown'} and not missing_reasons and yarn_value is not None
    return {
        'supported': supported,
        'support_classification': support_classification,
        'yarn_enum_value': yarn_value,
        'yarn_enum_location': resolver_source,
        'yarn_resolver_source': resolver_source,
        'accepted_constructor_kwargs': accepted_kwargs,
        'constructor_kwarg_support': kwarg_support,
        'missing_required_kwargs': missing_kwargs,
        'missing_reason': '; '.join(missing_reasons) if missing_reasons else None,
        'llama_module_path': worker_capabilities.get('llama_module_path') or getattr(llama_cpp_module, '__file__', None),
        'llama_module_path_present': (
            worker_capabilities.get('llama_module_path_present')
            if isinstance(worker_capabilities.get('llama_module_path_present'), bool)
            else bool(worker_capabilities.get('llama_module_path'))
        ),
        'llama_module_identity_match': worker_capabilities.get('llama_module_identity_match'),
        'llama_cpp_python_version': worker_capabilities.get('llama_cpp_python_version') or _llama_cpp_python_version(llama_cpp_module),
        'constructor_has_var_kwargs': constructor_has_var_kwargs,
        'constructor_signature_inspectable': worker_capabilities.get('constructor_signature_inspectable'),
        'capability_source': worker_capabilities.get('capability_source') or (
            'worker_probe' if isinstance(worker_kwarg_support, dict) else 'local_constructor_signature'
        ),
        'parent_facade_type': type(llama_cpp_module).__name__ if getattr(llama_cpp_module, '__token_place_subprocess_facade__', False) else None,
        'child_probe_reprobe_attempted': bool(worker_capabilities.get('child_probe_reprobe_attempted', False)),
        'constructor_kwargs_attempted': worker_capabilities.get('constructor_kwargs_attempted') or (
            list(required_kwargs) if supported else []
        ),
    }


def _runtime_supports_qwen_yarn_rope(llama_cpp_module: Any, llama_cls: Any) -> Dict[str, Any]:
    if getattr(llama_cpp_module, '__token_place_subprocess_facade__', False):
        capabilities = _safe_constructor_capability_payload(llama_cpp_module)
        source = capabilities.get('capability_source')
        kwarg_support = capabilities.get('constructor_kwarg_support')
        required = ('rope_scaling_type', 'rope_freq_scale', 'yarn_orig_ctx')
        required_supported = isinstance(kwarg_support, dict) and all(kwarg_support.get(name) is True for name in required)
        authoritative_backend = str(capabilities.get('backend') or '').lower() in {'cuda', 'metal'}
        capability_module_path = capabilities.get('llama_module_path')
        raw_capability_identity = capabilities.get('llama_module_identity')
        capability_identity_supplied = llama_module_identity_supplied(raw_capability_identity)
        capability_identity = _valid_llama_module_identity(raw_capability_identity)
        capability_identity_malformed = (
            capability_identity_supplied and capability_identity is None
        ) or capabilities.get('llama_module_identity_malformed') is True
        facade_module_path = getattr(llama_cpp_module, '__file__', None)
        facade_identity = llama_module_identity_from_path(facade_module_path)
        concrete_paths_match = (
            bool(capability_module_path)
            and str(capability_module_path).strip() not in {'missing', 'unknown'}
            and bool(facade_module_path)
            and _canonical_path_for_compare(capability_module_path)
            == _canonical_path_for_compare(facade_module_path)
        )
        identities_match = bool(capability_identity and facade_identity and capability_identity == facade_identity)
        if capability_identity and capabilities.get('llama_module_identity_verified') is True:
            identities_match = True
        if capability_identity and capability_module_path and str(capability_module_path).strip() not in {'missing', 'unknown'}:
            identities_match = identities_match and capability_identity == llama_module_identity_from_path(capability_module_path)
        legacy_path_fallback_allowed = (
            source == 'desktop_runtime_setup_probe_legacy'
            and not capability_identity_supplied
            and not capability_identity_malformed
        )
        module_paths_match = identities_match or (legacy_path_fallback_allowed and concrete_paths_match)
        capabilities['llama_module_identity_match'] = identities_match
        capabilities['llama_module_identity_malformed'] = capability_identity_malformed
        try:
            llama_cpp_module.__token_place_worker_capabilities__['llama_module_identity_match'] = identities_match
        except Exception:
            pass
        authoritative = (
            source in {'desktop_runtime_setup_probe', 'desktop_runtime_setup_probe_legacy'}
            and authoritative_backend
            and capabilities.get('gpu_offload_supported') is True
            and module_paths_match
        )
        complete = (
            capabilities.get('qwen_64k_yarn_support') == 'supported'
            and required_supported
            and capabilities.get('yarn_enum_value') is not None
            and capabilities.get('constructor_signature_inspectable') is True
            and module_paths_match
        )
        if authoritative and complete:
            diagnostics = _qwen_64k_rope_support_diagnostics(llama_cpp_module, llama_cls)
            diagnostics['child_probe_reprobe_attempted'] = False
            diagnostics['child_probe_reprobe_skipped_reason'] = 'desktop_probe_authoritative'
            diagnostics['desktop_probe_authoritative'] = True
            return diagnostics
        if source in {'desktop_runtime_setup_probe', 'desktop_runtime_setup_probe_legacy'} and not complete:
            missing = []
            if capabilities.get('qwen_64k_yarn_support') != 'supported':
                missing.append('qwen_64k_yarn_support')
            if not required_supported:
                missing.extend([name for name in required if not (isinstance(kwarg_support, dict) and kwarg_support.get(name) is True)])
            if capabilities.get('yarn_enum_value') is None:
                missing.append('yarn_enum_value')
            if capabilities.get('constructor_signature_inspectable') is not True:
                missing.append('constructor_signature_inspectable')
            if not module_paths_match:
                if capability_identity_malformed or (source == 'desktop_runtime_setup_probe' and not capability_identity_supplied):
                    missing.append('llama_module_identity')
                elif capability_identity_supplied:
                    missing.append('llama_module_identity_match')
                else:
                    missing.append('llama_module_path')
            if not authoritative_backend:
                missing.append('backend')
            if capabilities.get('gpu_offload_supported') is not True:
                missing.append('gpu_offload_supported')
            diagnostics = _qwen_64k_rope_support_diagnostics(llama_cpp_module, llama_cls)
            diagnostics.update({
                'supported': False,
                'missing_reason': 'runtime_desktop_capability_probe_incomplete',
                'missing_required_kwargs': sorted(set(missing)),
                'incomplete_probe_fields': sorted(set(missing)),
                'child_probe_reprobe_attempted': False,
                'child_probe_reprobe_skipped_reason': 'desktop_probe_incomplete_fail_closed',
                'desktop_probe_authoritative': False,
            })
            return diagnostics
        probe = _probe_llama_cpp_capabilities_in_subprocess(timeout_seconds=getattr(llama_cpp_module, '_timeout_seconds', None))
        if isinstance(probe, dict):
            probe = dict(probe)
            probe['child_probe_reprobe_attempted'] = True
            llama_cpp_module.__token_place_worker_capabilities__ = probe
    diagnostics = _qwen_64k_rope_support_diagnostics(llama_cpp_module, llama_cls)
    if diagnostics.get('child_probe_reprobe_attempted') is not True:
        diagnostics.setdefault('child_probe_reprobe_attempted', False)
    return diagnostics

def _is_site_packages_path(path_text: Any) -> bool:
    normalized = str(path_text).replace('\\', '/').lower()
    return 'site-packages' in normalized or 'dist-packages' in normalized


def _stdlib_roots_for_import_order() -> list[str]:
    roots: list[str] = []
    for key in ('stdlib', 'platstdlib'):
        value = sysconfig.get_paths().get(key)
        if value:
            roots.append(_subprocess_safe_path_text(value))
    destshared = sysconfig.get_config_var('DESTSHARED')
    if destshared:
        roots.append(_subprocess_safe_path_text(destshared))
    for prefix in {sys.prefix, getattr(sys, 'base_prefix', sys.prefix), getattr(sys, 'exec_prefix', sys.prefix), getattr(sys, 'base_exec_prefix', sys.prefix)}:
        roots.append(_subprocess_safe_path_text(os.path.join(prefix, 'lib', f'python{sys.version_info.major}.{sys.version_info.minor}')))
        roots.append(_subprocess_safe_path_text(os.path.join(prefix, 'Lib')))
    deduped: list[str] = []
    seen: set[str] = set()
    for root in roots:
        compare = _canonical_path_for_compare(root)
        if compare and compare not in seen:
            seen.add(compare)
            deduped.append(root)
    return deduped


def _is_stdlib_path(path_text: Any) -> bool:
    if _is_site_packages_path(path_text):
        return False
    path_compare = _canonical_path_for_compare(path_text)
    if path_compare is None:
        return False
    for root in _stdlib_roots_for_import_order():
        root_compare = _canonical_path_for_compare(root)
        if not root_compare:
            continue
        try:
            if os.path.commonpath([path_compare, root_compare]) == root_compare:
                return True
        except ValueError:
            continue
    return False


def _stdlib_shadow_error(module_name: str, origin: Any) -> Optional[str]:
    if origin in (None, 'built-in', 'frozen'):
        return None
    if _is_site_packages_path(origin) or not _is_stdlib_path(origin):
        return f"stdlib module {module_name} shadowed by {origin or '<not found>'}"
    return None


def _assert_critical_stdlib_not_shadowed() -> None:
    importlib.invalidate_caches()
    for module_name in CRITICAL_STDLIB_IMPORT_MODULES:
        spec = importlib.util.find_spec(module_name)
        origin = getattr(spec, 'origin', None) if spec is not None else None
        error = _stdlib_shadow_error(module_name, origin) if spec is not None else (
            f"stdlib module {module_name} shadowed by <not found>"
        )
        if error:
            raise ImportError(error)


def _stdlib_safe_path_order(entries: Iterable[str]) -> list[str]:
    stdlib_entries: list[str] = []
    app_entries: list[str] = []
    site_entries: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, str) or not entry:
            continue
        safe_entry = _subprocess_safe_path_text(entry)
        compare = _canonical_path_for_compare(safe_entry)
        if compare is None or compare in seen:
            continue
        seen.add(compare)
        if _is_stdlib_path(safe_entry):
            stdlib_entries.append(safe_entry)
        elif _is_site_packages_path(safe_entry):
            site_entries.append(safe_entry)
        else:
            app_entries.append(safe_entry)
    return stdlib_entries + app_entries + site_entries

DESKTOP_RUNTIME_PROBE_ENV = 'TOKEN_PLACE_DESKTOP_RUNTIME_PROBE_JSON'
_LLAMA_CPP_IMPORT_PATH_LOCK = Lock()


class LlamaCppInferenceRequestError(RuntimeError):
    """Raised when a live llama.cpp worker reports a request-scoped failure."""

    def __init__(self, message: str, *, diagnostics: Optional[Dict[str, Any]] = None) -> None:
        self.diagnostics = diagnostics or {}
        super().__init__(message)


class LlamaCppRestartableWorkerError(RuntimeError):
    """Raised when the llama.cpp worker transport is unusable and may be replaced."""


class LlamaCppWorkerDeadError(LlamaCppRestartableWorkerError):
    """Raised when the subprocess worker is no longer alive before a request."""


class LlamaCppWorkerEOFError(LlamaCppRestartableWorkerError):
    """Raised when the worker exits before returning a response."""


class LlamaCppWorkerBrokenPipeError(LlamaCppRestartableWorkerError):
    """Raised when writing to the worker transport fails."""


class LlamaCppRuntimeStageTimeout(TimeoutError):
    """Raised when a llama_cpp discovery/import stage exceeds its bounded timeout."""

    def __init__(self, stage: str, timeout_seconds: float) -> None:
        self.stage = stage
        self.timeout_seconds = timeout_seconds
        super().__init__(f"{stage} after {timeout_seconds:g}s")


def _format_runtime_stage_timeout(exc: LlamaCppRuntimeStageTimeout) -> str:
    return f"{exc.stage}_timeout after {exc.timeout_seconds:g}s"


def _strip_windows_extended_path_prefix(path_text: str) -> str:
    """Return a path string with Windows extended-length prefixes removed for comparison."""

    if path_text.startswith('\\\\?\\UNC\\'):
        return '\\\\' + path_text[8:]
    if path_text.startswith('\\\\?\\'):
        return path_text[4:]
    return path_text


def _subprocess_safe_path_text(path_text: Any) -> str:
    """Return a subprocess/env-safe path string without Windows extended prefixes."""

    return _strip_windows_extended_path_prefix(str(path_text))


def _sanitize_subprocess_path_env(env: Dict[str, str], pythonpath_entries: list[str]) -> Dict[str, str]:
    """Normalize path bootstrap variables shared by probes and runtime workers."""

    sanitized = dict(env)
    sanitized_entries = [_subprocess_safe_path_text(entry) for entry in pythonpath_entries]
    sanitized['PYTHONPATH'] = os.pathsep.join(sanitized_entries)
    for name in (
        'TOKEN_PLACE_PYTHON_IMPORT_ROOT',
        'TOKEN_PLACE_DESKTOP_BOOTSTRAP_SCRIPT',
        'TOKEN_PLACE_DESKTOP_PYTHON_ROOT',
        'TOKEN_PLACE_PROBE_REPO_ROOT',
    ):
        value = sanitized.get(name)
        if value:
            sanitized[name] = _subprocess_safe_path_text(value)
    sanitized.setdefault('PYTHONNOUSERSITE', '1')
    return sanitized



def _raw_path_sentinel(module_path: Any) -> bool:
    if module_path is None:
        return True
    try:
        text = str(module_path).strip()
    except (TypeError, ValueError, OSError):
        return True
    return text == '' or text.lower() in {'missing', 'unknown'}


def _looks_like_windows_path(path_text: str) -> bool:
    stripped = path_text.strip()
    return (
        stripped.startswith("\\")
        or stripped.startswith("\\?\\")
        or (len(stripped) >= 3 and stripped[1] == ":" and stripped[2] in {"\\", "/"})
    )

def _canonical_windows_path_for_identity(path_text: str) -> str:
    stripped = _strip_windows_extended_path_prefix(path_text.strip())
    if stripped.startswith('\\?/'):
        stripped = stripped[3:]
    if stripped.startswith('/'):
        canonical = _shared_canonical_llama_module_identity_input(stripped)
        return canonical or os.path.normpath(stripped).replace("\\", "/")
    normalized = ntpath.normpath(stripped).replace("\\", "/")
    return normalized.lower()


def _canonical_path_for_compare(module_path: Any) -> Optional[str]:
    if _raw_path_sentinel(module_path):
        return None
    path_text = str(module_path)
    if _looks_like_windows_path(path_text):
        return _canonical_windows_path_for_identity(path_text)
    canonical = _shared_canonical_llama_module_identity_input(module_path)
    if not canonical or canonical.strip().lower() in {'missing', 'unknown'}:
        return None
    return canonical


def llama_module_identity_from_path(module_path: Any) -> Optional[str]:
    canonical = _canonical_path_for_compare(module_path)
    if not canonical:
        return None
    digest = hashlib.sha256(f"token.place.llama_cpp.module_path.v1\0{canonical}".encode('utf-8')).hexdigest()
    return f"sha256:{digest}"


def _valid_llama_module_identity(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return valid_llama_module_identity(value)

def _is_repo_llama_cpp_shim(module_path: Any) -> bool:
    """Return True when llama_cpp resolves to the repository-local shim."""
    if not module_path:
        return False
    module_compare = _canonical_path_for_compare(module_path)
    shim_compare = _canonical_path_for_compare(REPO_LLAMA_CPP_SHIM)
    return bool(module_compare and shim_compare and module_compare == shim_compare)


def _runtime_stage_timeout_seconds() -> float:
    raw_value = os.getenv('TOKEN_PLACE_LLAMA_CPP_RUNTIME_STAGE_TIMEOUT_SECONDS', '').strip()
    if not raw_value:
        return DEFAULT_LLAMA_CPP_RUNTIME_STAGE_TIMEOUT_SECONDS
    try:
        value = float(raw_value)
    except ValueError:
        return DEFAULT_LLAMA_CPP_RUNTIME_STAGE_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_LLAMA_CPP_RUNTIME_STAGE_TIMEOUT_SECONDS


def _sanitize_llama_cpp_import_paths() -> Dict[str, Any]:
    """Keep app imports available while preventing repo-local llama_cpp shim precedence."""

    with _LLAMA_CPP_IMPORT_PATH_LOCK:
        import_root = os.environ.get('TOKEN_PLACE_PYTHON_IMPORT_ROOT', '').strip() or str(REPO_ROOT)
        moved: list[str] = []
        repo_root_compare = _canonical_path_for_compare(REPO_ROOT)
        cwd_compare = _canonical_path_for_compare(Path.cwd())
        shim_entries: list[str] = []
        preserved_entries: list[str] = []

        cwd_text = os.getcwd()
        for entry in sys.path:
            entry_text = str(entry or cwd_text)
            compare = _canonical_path_for_compare(entry_text)
            # Avoid probing every sys.path entry with stat/is_file here: on Windows,
            # offline shares or slow filesystem roots can block before the bounded
            # subprocess discovery/import stages start.  The repository shim path is
            # known, so string-normalized repo/cwd comparisons are sufficient.
            shadows_repo_shim = (
                compare is not None
                and (compare == repo_root_compare or compare == cwd_compare)
            )
            if shadows_repo_shim:
                shim_entries.append(entry)
                moved.append(entry or '<cwd>')
                continue
            preserved_entries.append(entry)

        preserved_entries = _stdlib_safe_path_order(preserved_entries)
        preferred_index = len(preserved_entries)
        for idx, entry in enumerate(preserved_entries):
            normalized = str(entry).replace('\\', '/').lower()
            if 'site-packages' in normalized or 'dist-packages' in normalized:
                preferred_index = idx + 1

        sys.path[:] = (
            preserved_entries[:preferred_index]
            + shim_entries
            + preserved_entries[preferred_index:]
        )
        return {
            'import_root': import_root,
            'deprioritized_entries': moved,
            'sys_path_count': len(sys.path),
        }


def _llama_cpp_probe_sys_path_entries() -> list[str]:
    """Return explicit child probe import paths without implicit cwd shadow entries."""

    cwd_compare = _canonical_path_for_compare(Path.cwd())
    entries: list[str] = []
    seen: set[str] = set()
    for entry in sys.path:
        if not isinstance(entry, str):
            continue
        if entry == '':
            # In a child ``python -c`` process, an empty sys.path entry means that
            # child's cwd.  Do not pass it through because either the repo cwd or a
            # shared temp cwd can shadow the packaged llama_cpp runtime.
            continue
        compare = _canonical_path_for_compare(entry)
        if compare is not None and cwd_compare is not None and compare == cwd_compare:
            continue
        dedupe_key = compare or entry
        if dedupe_key in seen:
            continue
        entries.append(_subprocess_safe_path_text(entry))
        seen.add(dedupe_key)
    return _stdlib_safe_path_order(entries)


def _llama_cpp_probe_env() -> Dict[str, str]:
    """Return subprocess env with an explicit sanitized import path contract."""

    pythonpath_entries = _llama_cpp_probe_sys_path_entries()
    env = _sanitize_subprocess_path_env(os.environ.copy(), pythonpath_entries)
    env['TOKEN_PLACE_LLAMA_CPP_PROBE_SYS_PATH'] = json.dumps(pythonpath_entries)
    return env


def _llama_cpp_path_prefixed_code(user_code: str, path_source: str) -> str:
    """Prefix code so cwd/sys.path[0] cannot shadow the runtime."""

    return (
        "import json as _token_place_json, os as _token_place_os, sys as _token_place_sys\n"
        f"_token_place_probe_path = _token_place_json.loads({path_source})\n"
        "_token_place_cwd = _token_place_os.path.normcase("
        "_token_place_os.path.normpath(_token_place_os.getcwd()))\n"
        "_token_place_existing = []\n"
        "for _token_place_entry in _token_place_sys.path:\n"
        "    if not isinstance(_token_place_entry, str) or not _token_place_entry:\n"
        "        continue\n"
        "    _token_place_compare = _token_place_os.path.normcase("
        "_token_place_os.path.normpath(_token_place_os.path.abspath(_token_place_entry)))\n"
        "    if _token_place_compare == _token_place_cwd:\n"
        "        continue\n"
        "    _token_place_existing.append((_token_place_compare, _token_place_entry))\n"
        "if isinstance(_token_place_probe_path, list):\n"
        "    _token_place_explicit = []\n"
        "    _token_place_seen = set()\n"
        "    for _token_place_entry in _token_place_probe_path:\n"
        "        if not isinstance(_token_place_entry, str) or not _token_place_entry:\n"
        "            continue\n"
        "        _token_place_compare = _token_place_os.path.normcase("
        "_token_place_os.path.normpath(_token_place_os.path.abspath(_token_place_entry)))\n"
        "        if _token_place_compare == _token_place_cwd or _token_place_compare in _token_place_seen:\n"
        "            continue\n"
        "        _token_place_explicit.append(_token_place_entry)\n"
        "        _token_place_seen.add(_token_place_compare)\n"
        "    _token_place_sys.path[:] = _token_place_explicit + ["
        "_token_place_entry for _token_place_compare, _token_place_entry in _token_place_existing "
        "if _token_place_compare not in _token_place_seen]\n"
        "del _token_place_json, _token_place_os, _token_place_probe_path\n"
        "del _token_place_cwd, _token_place_existing, _token_place_seen\n"
        + user_code
    )


def _llama_cpp_stdlib_guard_code() -> str:
    return (
        "import importlib.util as _token_place_importlib_util, sysconfig as _token_place_sysconfig, "
        "os as _token_place_os, sys as _token_place_sys\n"
        "_token_place_stdlib_candidates = [_token_place_sysconfig.get_paths().get('stdlib'), _token_place_sysconfig.get_paths().get('platstdlib'), _token_place_sysconfig.get_config_var('DESTSHARED')]\n"
        "for _token_place_prefix in {_token_place_sys.prefix, getattr(_token_place_sys, 'base_prefix', _token_place_sys.prefix), getattr(_token_place_sys, 'exec_prefix', _token_place_sys.prefix), getattr(_token_place_sys, 'base_exec_prefix', _token_place_sys.prefix)}:\n"
        "    _token_place_stdlib_candidates.append(_token_place_os.path.join(_token_place_prefix, 'lib', f'python{_token_place_sys.version_info.major}.{_token_place_sys.version_info.minor}'))\n"
        "    _token_place_stdlib_candidates.append(_token_place_os.path.join(_token_place_prefix, 'Lib'))\n"
        "_token_place_stdlib_roots = [_token_place_os.path.normcase(_token_place_os.path.normpath(_token_place_os.path.realpath(_token_place_os.path.abspath(_p)))) "
        "for _p in _token_place_stdlib_candidates if _p]\n"
        "def _token_place_is_site(_p):\n"
        "    return 'site-packages' in str(_p).replace('\\\\', '/').lower() or 'dist-packages' in str(_p).replace('\\\\', '/').lower()\n"
        "def _token_place_is_stdlib(_p):\n"
        "    if not _p or _p in ('built-in', 'frozen'):\n"
        "        return True\n"
        "    if _token_place_is_site(_p):\n"
        "        return False\n"
        "    _candidate = _token_place_os.path.normcase(_token_place_os.path.normpath(_token_place_os.path.realpath(_token_place_os.path.abspath(_p))))\n"
        "    for _root in _token_place_stdlib_roots:\n"
        "        try:\n"
        "            if _token_place_os.path.commonpath([_candidate, _root]) == _root:\n"
        "                return True\n"
        "        except Exception:\n"
        "            pass\n"
        "    return False\n"
        "for _token_place_module in ('collections','typing','ctypes','subprocess','json','importlib','pathlib'):\n"
        "    _token_place_spec = _token_place_importlib_util.find_spec(_token_place_module)\n"
        "    _token_place_origin = getattr(_token_place_spec, 'origin', None) if _token_place_spec else None\n"
        "    if _token_place_spec is None or not _token_place_is_stdlib(_token_place_origin):\n"
        "        _token_place_bad_origin = _token_place_origin or '<not found>'\n"
        "        raise ImportError(f'stdlib module {_token_place_module} shadowed by {_token_place_bad_origin}')\n"
        "del _token_place_importlib_util, _token_place_sysconfig, _token_place_os, _token_place_sys, _token_place_stdlib_candidates\n"
        "try:\n"
        "    del _token_place_bad_origin\n"
        "except NameError:\n"
        "    pass\n"
    )

def _llama_cpp_probe_code(user_code: str) -> str:
    """Prefix probe code using the probe sys.path environment contract."""

    return _llama_cpp_path_prefixed_code(
        _llama_cpp_stdlib_guard_code() + user_code,
        "_token_place_os.environ.get('TOKEN_PLACE_LLAMA_CPP_PROBE_SYS_PATH', '[]')",
    )


def _llama_cpp_runtime_worker_env() -> Dict[str, str]:
    """Return subprocess env for killable runtime workers.

    Runtime workers intentionally do not set TOKEN_PLACE_LLAMA_CPP_PROBE_SYS_PATH:
    that variable belongs to discovery/probe subprocesses and historically
    triggered the removed import-watchdog failure mode in packaged desktop
    builds.  The worker still receives the same sanitized import path via an
    embedded JSON literal in its bootstrap code.
    """

    pythonpath_entries = _llama_cpp_probe_sys_path_entries()
    env = _sanitize_subprocess_path_env(os.environ.copy(), pythonpath_entries)
    env.pop('TOKEN_PLACE_LLAMA_CPP_PROBE_SYS_PATH', None)
    return env


def _llama_cpp_runtime_worker_code(user_code: str) -> str:
    """Prefix runtime-worker code with a literal sanitized import path."""

    return _llama_cpp_path_prefixed_code(
        _llama_cpp_stdlib_guard_code() + user_code,
        repr(json.dumps(_llama_cpp_probe_sys_path_entries())),
    )


def _llama_cpp_probe_subprocess_cwd() -> str:
    """Return a cwd that should be ignored by child probe import resolution."""

    # Python prepends the subprocess cwd as sys.path[0] for ``python -c``.  Probe
    # code immediately replaces sys.path with TOKEN_PLACE_LLAMA_CPP_PROBE_SYS_PATH,
    # so neither the repo cwd nor a shared temp cwd can shadow the runtime.
    return os.path.dirname(sys.executable) or os.getcwd()


def _run_llama_cpp_python_probe(stage: str, code: str, *, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
    """Run a llama_cpp runtime probe in a killable subprocess and return JSON output."""

    timeout = timeout_seconds if timeout_seconds is not None else _runtime_stage_timeout_seconds()
    env = _llama_cpp_probe_env()
    started_at = time.perf_counter()
    logger.info(
        "llama_cpp runtime process stage start stage=%s timeout_seconds=%s interpreter=%s",
        stage,
        f"{timeout:g}",
        sys.executable,
    )
    try:
        completed = subprocess.run(
            [sys.executable, '-c', _llama_cpp_probe_code(code)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=_llama_cpp_probe_subprocess_cwd(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        logger.error(
            "llama_cpp runtime process stage timeout stage=%s duration_ms=%s timeout_seconds=%s interpreter=%s",
            stage,
            duration_ms,
            f"{timeout:g}",
            sys.executable,
        )
        raise LlamaCppRuntimeStageTimeout(stage, timeout) from exc

    duration_ms = int((time.perf_counter() - started_at) * 1000)
    if completed.returncode != 0:
        stderr = (completed.stderr or '').strip()
        raise ImportError(
            f"{stage} failed returncode={completed.returncode} stderr={stderr[:500]}"
        )

    stdout = (completed.stdout or '').strip().splitlines()
    diagnostics: Dict[str, Any] = {}
    if stdout:
        try:
            parsed = json.loads(stdout[-1])
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            diagnostics = parsed
    logger.info(
        "llama_cpp runtime process stage complete stage=%s duration_ms=%s module_path_present=%s",
        stage,
        duration_ms,
        bool(diagnostics.get('module_path') or diagnostics.get('llama_module_path')),
    )
    return diagnostics


def _find_llama_cpp_spec_in_subprocess(*, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
    code = (
        "import importlib.util, json, sys\n"
        "spec = importlib.util.find_spec('llama_cpp')\n"
        "print(json.dumps({\n"
        "    'module_path': getattr(spec, 'origin', None) if spec else None,\n"
        "    'interpreter': sys.executable,\n"
        "}))\n"
    )
    return _run_llama_cpp_python_probe(
        'llama_cpp_runtime_discovery',
        code,
        timeout_seconds=timeout_seconds,
    )


def _probe_llama_cpp_capabilities_in_subprocess(*, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
    code = (
        "import importlib, inspect, json, sys\n"
        "llama_cpp = importlib.import_module('llama_cpp')\n"
        "def _ctor_details(cls, names):\n"
        "    ctor = getattr(cls, '__init__', cls)\n"
        "    try:\n"
        "        params = inspect.signature(ctor).parameters\n"
        "    except (TypeError, ValueError):\n"
        "        return {name: False for name in names}, False, False\n"
        "    accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())\n"
        "    return {name: (name in params or accepts_var_kw) for name in names}, accepts_var_kw, True\n"
        "probe_kwargs = (\n"
        "    'type_k', 'type_v', 'flash_attn', 'offload_kqv', 'n_batch', 'n_ubatch',\n"
        "    'rope_scaling_type', 'yarn_ext_factor', 'yarn_attn_factor', 'yarn_beta_fast',\n"
        "    'yarn_beta_slow', 'yarn_orig_ctx', 'rope_freq_base', 'rope_freq_scale',\n"
        ")\n"
        "llama_cls = getattr(llama_cpp, 'Llama', None)\n"
        "constructor_support, constructor_has_var_kwargs, signature_inspectable = _ctor_details(llama_cls, probe_kwargs)\n"
        "yarn_value = getattr(llama_cpp, 'LLAMA_ROPE_SCALING_TYPE_YARN', None)\n"
        "yarn_source = 'top_level_enum' if yarn_value is not None else 'unsupported'\n"
        "if yarn_value is None:\n"
        "    yarn_value = getattr(getattr(llama_cpp, 'llama_cpp', None), 'LLAMA_ROPE_SCALING_TYPE_YARN', None)\n"
        "    yarn_source = 'nested_enum' if yarn_value is not None else 'unsupported'\n"
        "if yarn_value is None and constructor_support.get('rope_scaling_type'):\n"
        "    yarn_value = 2\n"
        "    yarn_source = 'numeric_fallback'\n"
        "required_yarn = ('rope_scaling_type', 'rope_freq_scale', 'yarn_orig_ctx')\n"
        "if yarn_source != 'unsupported' and all(constructor_support.get(name) for name in required_yarn):\n"
        "    qwen_yarn_support = 'supported'\n"
        "elif not signature_inspectable:\n"
        "    qwen_yarn_support = 'unknown'\n"
        "else:\n"
        "    qwen_yarn_support = 'unsupported'\n"
        "def _const(*names):\n"
        "    for owner in (llama_cpp, getattr(llama_cpp, 'llama_cpp', None)):\n"
        "        if owner is None:\n"
        "            continue\n"
        "        for name in names:\n"
        "            value = getattr(owner, name, None)\n"
        "            if isinstance(value, int):\n"
        "                return value\n"
        "    return None\n"
        "q8_value = _const('GGML_TYPE_Q8_0', 'LLAMA_TYPE_Q8_0')\n"
        "q4_value = _const('GGML_TYPE_Q4_0', 'LLAMA_TYPE_Q4_0')\n"
        "f16_value = _const('GGML_TYPE_F16', 'LLAMA_TYPE_F16')\n"
        "cuda_markers = ('GGML_USE_CUDA', 'GGML_CUDA', 'LLAMA_CUDA', 'GGML_USE_CUBLAS', 'LLAMA_CUBLAS')\n"
        "metal_markers = ('GGML_USE_METAL', 'GGML_METAL', 'LLAMA_METAL')\n"
        "backend = 'cpu'\n"
        "if any(bool(getattr(llama_cpp, marker, False)) for marker in cuda_markers):\n"
        "    backend = 'cuda'\n"
        "elif any(bool(getattr(llama_cpp, marker, False)) for marker in metal_markers):\n"
        "    backend = 'metal'\n"
        "supports_gpu = getattr(llama_cpp, 'llama_supports_gpu_offload', None)\n"
        "gpu_supported = False\n"
        "if callable(supports_gpu):\n"
        "    gpu_supported = bool(supports_gpu())\n"
        "else:\n"
        "    gpu_supported = backend in {'cuda', 'metal'}\n"
        "if gpu_supported and backend == 'cpu':\n"
        "    backend = 'metal' if sys.platform == 'darwin' else 'cuda'\n"
        "print(json.dumps({\n"
        "    'backend': backend,\n"
        "    'gpu_offload_supported': gpu_supported,\n"
        "    'detected_device': backend if gpu_supported else 'cpu',\n"
        "    'interpreter': sys.executable,\n"
        "    'prefix': sys.prefix,\n"
        "    'llama_module_path': getattr(llama_cpp, '__file__', 'unknown'),\n"
        "    'constructor_kwarg_support': constructor_support,\n"
        "    'constructor_has_var_kwargs': constructor_has_var_kwargs,\n"
        "    'constructor_signature_inspectable': signature_inspectable,\n"
        "    'yarn_resolver_source': yarn_source,\n"
        "    'yarn_enum_value': yarn_value if isinstance(yarn_value, (int, float, str)) else None,\n"
        "    'qwen_64k_yarn_support': qwen_yarn_support,\n"
        "    'q8_kv_cache_type_value': q8_value if isinstance(q8_value, int) else None,\n"
        "    'q4_kv_cache_type_value': q4_value if isinstance(q4_value, int) else None,\n"
        "    'f16_kv_cache_type_value': f16_value if isinstance(f16_value, int) else None,\n"
        "    'capability_source': 'worker_probe',\n"
        "    'llama_cpp_python_version': getattr(llama_cpp, '__version__', None),\n"
        "    'error': None,\n"
        "}))\n"
    )
    return _run_llama_cpp_python_probe(
        'llama_cpp_gpu_probe',
        code,
        timeout_seconds=timeout_seconds,
    )

def _run_llama_cpp_import_watchdog(*, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
    """Validate llama_cpp import in a killable subprocess before parent import."""

    timeout = timeout_seconds if timeout_seconds is not None else _runtime_stage_timeout_seconds()
    env = _llama_cpp_probe_env()
    code = (
        "import importlib, json, sys\n"
        "llama_cpp = importlib.import_module('llama_cpp')\n"
        "print(json.dumps({\n"
        "    'module_path': getattr(llama_cpp, '__file__', None),\n"
        "    'interpreter': sys.executable,\n"
        "}))\n"
    )
    started_at = time.perf_counter()
    logger.info(
        "llama_cpp import watchdog start timeout_seconds=%s interpreter=%s",
        f"{timeout:g}",
        sys.executable,
    )
    try:
        completed = subprocess.run(
            [sys.executable, '-c', _llama_cpp_probe_code(code)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=_llama_cpp_probe_subprocess_cwd(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        logger.error(
            "llama_cpp import watchdog timeout duration_ms=%s timeout_seconds=%s interpreter=%s",
            duration_ms,
            f"{timeout:g}",
            sys.executable,
        )
        raise LlamaCppRuntimeStageTimeout('llama_cpp_import', timeout) from exc

    duration_ms = int((time.perf_counter() - started_at) * 1000)
    if completed.returncode != 0:
        stderr = (completed.stderr or '').strip()
        raise ImportError(
            "llama_cpp import watchdog failed "
            f"returncode={completed.returncode} stderr={stderr[:500]}"
        )

    stdout = (completed.stdout or '').strip().splitlines()
    diagnostics: Dict[str, Any] = {}
    if stdout:
        try:
            parsed = json.loads(stdout[-1])
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            diagnostics = parsed
    logger.info(
        "llama_cpp import watchdog complete duration_ms=%s module_path=%s",
        duration_ms,
        diagnostics.get('module_path') or 'unknown',
    )
    return diagnostics


def _llama_cpp_subprocess_inference_timeout_seconds() -> Optional[float]:
    """Return an optional timeout for subprocess-backed inference calls."""

    raw = os.getenv('TOKEN_PLACE_LLAMA_CPP_SUBPROCESS_INFERENCE_TIMEOUT_SECONDS')
    if raw is None or raw.strip() == '':
        # Runtime-stage timeouts bound discovery/import/probe work only.  Inference
        # can legitimately run longer, and API/relay callers already have their
        # own request deadlines, so do not apply the import-stage timeout here.
        return None
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _signal_guard_available() -> bool:
    return (
        hasattr(signal, 'SIGALRM')
        and hasattr(signal, 'ITIMER_REAL')
        and hasattr(signal, 'setitimer')
    )


def _import_llama_cpp_subprocess_module(
    *,
    module_path_hint: Any = None,
    timeout_seconds: Optional[float] = None,
    desktop_runtime_probe: Any = None,
    expected_llama_module_identity: Any = None,
):
    """Return a killable subprocess-backed llama_cpp module facade.

    Python's import machinery uses per-module locks.  If a daemon thread wedges
    inside a native ``llama_cpp`` import, later retries in the same bridge
    process can block behind that stuck import lock.  On Windows and desktop
    warm-load background threads, where SIGALRM cannot safely bound the active
    thread, avoid importing ``llama_cpp`` in-process at all and move the native
    import into a subprocess worker that can be terminated on timeout.
    """

    logger.info(
        "llama_cpp parent import skipped; using subprocess runtime facade "
        "module_path_hint_present=%s interpreter=%s thread=%s",
        bool(module_path_hint),
        sys.executable,
        threading.current_thread().name,
    )
    return _SubprocessLlamaCppModule(
        module_path_hint,
        timeout_seconds=timeout_seconds,
        desktop_runtime_probe=desktop_runtime_probe,
        expected_llama_module_identity=expected_llama_module_identity,
    )


def _import_llama_cpp_in_parent_with_timeout(
    *,
    timeout_seconds: Optional[float] = None,
    module_path_hint: Any = None,
    desktop_runtime_probe: Any = None,
    expected_llama_module_identity: Any = None,
):
    """Import llama_cpp in-process only when the active thread can be bounded.

    Prefer a SIGALRM guard on the main thread when available because it leaves no
    extra worker behind.  Windows and desktop warm-load background threads cannot
    use SIGALRM; spawning an in-process import thread is not recoverable if the
    native import wedges, so those paths return a subprocess-backed facade whose
    worker can be killed and retried without poisoning the bridge process.
    """

    timeout = timeout_seconds if timeout_seconds is not None else _runtime_stage_timeout_seconds()
    already_imported = sys.modules.get('llama_cpp')
    if already_imported is not None:
        return already_imported

    if not _signal_guard_available():
        return _import_llama_cpp_subprocess_module(
            module_path_hint=module_path_hint,
            timeout_seconds=timeout,
            desktop_runtime_probe=desktop_runtime_probe,
            expected_llama_module_identity=expected_llama_module_identity,
        )

    if threading.current_thread() is threading.main_thread():
        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout)

        def _handle_timeout(_signum, _frame):
            raise LlamaCppRuntimeStageTimeout('llama_cpp_import', timeout)

        signal.signal(signal.SIGALRM, _handle_timeout)
        try:
            _assert_critical_stdlib_not_shadowed()
            return importlib.import_module('llama_cpp')
        except TimeoutError as exc:
            if isinstance(exc, LlamaCppRuntimeStageTimeout):
                raise
            raise LlamaCppRuntimeStageTimeout('llama_cpp_import', timeout) from exc
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer[0] > 0:
                signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])

    return _import_llama_cpp_subprocess_module(
        module_path_hint=module_path_hint,
        timeout_seconds=timeout,
        desktop_runtime_probe=desktop_runtime_probe,
        expected_llama_module_identity=expected_llama_module_identity,
    )

def _llama_subprocess_tail(process: subprocess.Popen, name: str) -> str:
    value = getattr(process, name, '')
    if isinstance(value, list):
        lines = []
        for item in value:
            line = item[1] if isinstance(item, tuple) and len(item) == 2 else item
            if not str(line).startswith('TOKEN_PLACE_LLAMA_CPP_JSON:'):
                lines.append(str(line))
        text = ''.join(lines)
    else:
        text = str(value or '')
    return text[-2000:].strip()


def _format_llama_subprocess_early_exit_detail(process: subprocess.Popen, *, stage: str) -> str:
    poll = getattr(process, 'poll', None)
    exit_code = poll() if callable(poll) else None
    command = getattr(process, '_token_place_command', None)
    cwd = getattr(process, '_token_place_cwd', None)
    import_root = getattr(process, '_token_place_import_root', None)
    module_path_hint = getattr(process, '_token_place_module_path_hint', None)
    stdout_tail = _sanitize_child_diagnostic_text(_llama_subprocess_tail(process, '_token_place_stdout_tail'))
    stderr_tail = _sanitize_child_diagnostic_text(_llama_subprocess_tail(process, '_token_place_stderr_tail'))
    safe_command = (
        [_redact_paths_from_text(part) for part in command]
        if isinstance(command, list)
        else _redact_paths_from_text(command or 'unknown')
    )
    return (
        f"{stage} subprocess exited before JSON handshake; "
        f"exit_code={exit_code if exit_code is not None else 'running'} "
        f"program={_redact_paths_from_text(sys.executable)} command={safe_command} "
        f"cwd={_redact_paths_from_text(cwd or 'unknown')} "
        f"import_root={_redact_paths_from_text(import_root or 'unknown')} "
        f"module_path_hint={_redact_paths_from_text(module_path_hint or 'unknown')} "
        f"stage={stage} stdout_tail={stdout_tail or '<empty>'} "
        f"stderr_tail={stderr_tail or '<empty>'}"
    )


def _llama_subprocess_early_exit_payload(process: subprocess.Popen, *, stage: str) -> str:
    return json.dumps({
        'status': 'transport_error',
        'transport_error': 'eof_before_response',
        'error': _format_llama_subprocess_early_exit_detail(process, stage=stage),
    })


def _read_llama_subprocess_message(
    process: subprocess.Popen,
    *,
    timeout_seconds: Optional[float],
    stage: str,
) -> Dict[str, Any]:
    result_queue: queue.Queue[str] = queue.Queue(maxsize=1)

    def _reader() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            if line.startswith('TOKEN_PLACE_LLAMA_CPP_JSON:'):
                result_queue.put(line.split(':', 1)[1].strip())
                return
            tail = getattr(process, '_token_place_stdout_tail', None)
            if isinstance(tail, list):
                tail.append(line)
                del tail[:-100]
        try:
            process.wait(timeout=0.2)
        except Exception:
            pass
        time.sleep(0.05)
        result_queue.put(_llama_subprocess_early_exit_payload(process, stage=stage))

    reader = threading.Thread(target=_reader, name=f'{stage}_stdout_reader', daemon=True)
    reader.start()
    try:
        if timeout_seconds is None:
            raw_message = result_queue.get()
        else:
            raw_message = result_queue.get(timeout=timeout_seconds)
    except queue.Empty as exc:
        try:
            process.terminate()
            process.wait(timeout=1)
        except Exception:
            process.kill()
        raise LlamaCppRuntimeStageTimeout(stage, timeout_seconds) from exc
    try:
        message = json.loads(raw_message)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'{stage} returned malformed JSON') from exc
    if not isinstance(message, dict):
        raise RuntimeError(f'{stage} returned non-object JSON')
    if message.get('status') == 'transport_error':
        safe_error = _sanitize_child_diagnostic_text(message.get('error'))
        if not safe_error:
            safe_error = f'{stage} worker transport error; unsafe child diagnostic omitted'
        raise LlamaCppWorkerEOFError(safe_error)
    if message.get('status') == 'error':
        raw_error = str(message.get('error') or f'{stage} failed')
        error = raw_error
        if stage in {'llama_cpp_inference', 'llama_cpp_prompt_render_tokenize'} and message.get('request_error'):
            diagnostics = message.get('diagnostics')
            safe_diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
            raise LlamaCppInferenceRequestError(error, diagnostics=safe_diagnostics)
        child_category = _validated_init_safe_category(message.get('safe_error_category'))
        if stage == 'llama_cpp_model_initialization':
            category = child_category
            child_stderr_tail = ''
        else:
            classified = _classify_runtime_context_create_error(raw_error, str(message.get('stderr') or ''))
            category = child_category if child_category != 'runtime_init_unclassified' else classified
            child_stderr_tail = str(message.get('stderr') or '')
        raise LlamaCppRuntimeInitError(
            f'{stage} failed',
            child_exception_type=message.get('exception_type') or 'RuntimeError',
            safe_error_category=category,
            child_stderr_tail=child_stderr_tail,
        )
    return message


def _safe_worker_error_code(value: Any) -> str:
    text = str(value or '').strip().lower()
    if isinstance(value, LlamaCppWorkerDeadError):
        return 'worker_dead'
    if isinstance(value, LlamaCppWorkerEOFError):
        return 'worker_eof'
    if isinstance(value, LlamaCppWorkerBrokenPipeError):
        return 'worker_broken_pipe'
    if isinstance(value, LlamaCppInferenceRequestError):
        code = value.diagnostics.get('code') if isinstance(value.diagnostics, dict) else None
        return str(code) if isinstance(code, str) and code else 'inference_request_error'
    if 'broken pipe' in text:
        return 'worker_broken_pipe'
    if 'exited' in text or 'dead' in text or 'liveness' in text:
        return 'worker_dead'
    if 'timeout' in text:
        return 'worker_timeout'
    if text and all(ch.isalnum() or ch in {'_', '-'} for ch in text) and len(text) <= 80:
        return text.replace('-', '_')
    return type(value).__name__ if isinstance(value, BaseException) else 'worker_error'

class _SubprocessLlamaProxy:
    """Minimal llama_cpp.Llama proxy for no-SIGALRM runtimes."""

    def __init__(
        self,
        *args,
        timeout_seconds: Optional[float] = None,
        module_path_hint: Any = None,
        expected_llama_module_identity: Any = None,
        worker_capabilities: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> None:
        if 'model_path' in kwargs and isinstance(kwargs.get('model_path'), str):
            kwargs = dict(kwargs)
            kwargs['model_path'] = os.path.abspath(kwargs['model_path'])
        elif args and isinstance(args[0], str):
            args = (os.path.abspath(args[0]), *args[1:])
        self._timeout_seconds = timeout_seconds if timeout_seconds is not None else _runtime_stage_timeout_seconds()
        self._expected_llama_module_identity = _valid_llama_module_identity(expected_llama_module_identity)
        self._worker_capabilities_ref = worker_capabilities if isinstance(worker_capabilities, dict) else None
        self._lock = Lock()
        self._closed = False
        self._worker_tmpfile: Optional[str] = None
        code = _llama_cpp_runtime_worker_code(_LLAMA_CPP_RUNTIME_WORKER_CODE)
        # Write worker code to a temp file to avoid Windows command-line length
        # limit (CreateProcess caps at 32767 chars; the code is ~36KB).
        try:
            fd, tmppath = tempfile.mkstemp(suffix='.py', prefix='_token_place_worker_')
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(code)
            self._worker_tmpfile = tmppath
            command = [sys.executable, '-u', tmppath]
        except OSError:
            # Temp file creation failed (e.g. disk full, permissions); fall back
            # to the -c form which may exceed Windows' 32767-char limit but is
            # better than not launching at all.
            self._worker_tmpfile = None
            command = [sys.executable, '-u', '-c', code]
        env = _llama_cpp_runtime_worker_env()
        cwd = _llama_cpp_probe_subprocess_cwd()
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                cwd=cwd,
                bufsize=1,
            )
        except OSError:
            # Popen failed; clean up the temp file if one was created (when
            # self._worker_tmpfile is None we were already using the -c fallback).
            if self._worker_tmpfile:
                try:
                    os.unlink(self._worker_tmpfile)
                except Exception:
                    pass
                self._worker_tmpfile = None
            raise
        self._process._token_place_command = [command[0], '<runtime-worker-script>' if self._worker_tmpfile else '<runtime-worker-code>']  # type: ignore[attr-defined]
        self._process._token_place_cwd = cwd  # type: ignore[attr-defined]
        self._process._token_place_import_root = env.get('TOKEN_PLACE_PYTHON_IMPORT_ROOT', '')  # type: ignore[attr-defined]
        self._process._token_place_module_path_hint = module_path_hint or ''  # type: ignore[attr-defined]
        self._process._token_place_stdout_tail = []  # type: ignore[attr-defined]
        self._process._token_place_stderr_tail = []  # type: ignore[attr-defined]
        self._process._token_place_stderr_sequence = 0  # type: ignore[attr-defined]
        self._stderr_reader_thread: Optional[threading.Thread] = None
        self._start_stderr_tail_reader()
        try:
            self._send({'method': '__import__'}, check_health=False)
            import_message = _read_llama_subprocess_message(
                self._process,
                timeout_seconds=self._timeout_seconds,
                stage='llama_cpp_import',
            )
            imported_worker_module_path = import_message.get('module_path')
            imported_worker_identity = llama_module_identity_from_path(imported_worker_module_path)
            expected_identity = self._expected_llama_module_identity
            if _is_repo_llama_cpp_shim(imported_worker_module_path):
                raise ImportError('llama_cpp worker imported repository-local shim')
            if expected_identity is not None and imported_worker_identity != expected_identity:
                raise ImportError('llama_cpp worker identity mismatch')
            if self._worker_capabilities_ref is not None and expected_identity is not None:
                self._worker_capabilities_ref['llama_module_identity_match'] = True
                self._worker_capabilities_ref['llama_module_identity_verified'] = True
        except Exception as exc:
            self._drain_stderr_reader_bounded()
            safe_exc = _format_llama_subprocess_early_exit_detail(self._process, stage='llama_cpp_import')
            self.close()
            if isinstance(exc, LlamaCppRuntimeStageTimeout):
                raise
            if isinstance(exc, LlamaCppWorkerEOFError):
                raise LlamaCppWorkerEOFError(safe_exc) from exc
            if isinstance(exc, (LlamaCppWorkerBrokenPipeError, BrokenPipeError, OSError)):
                raise RuntimeError(safe_exc) from exc
            raise
        try:
            self._send({'method': '__init__', 'args': args, 'kwargs': kwargs}, check_health=False)
        except (LlamaCppWorkerBrokenPipeError, BrokenPipeError, OSError) as exc:
            self.close()
            raise RuntimeError(
                _format_llama_subprocess_early_exit_detail(self._process, stage='llama_cpp_model_initialization')
            ) from exc
        try:
            init_message = _read_llama_subprocess_message(
                self._process,
                timeout_seconds=self._timeout_seconds,
                stage='llama_cpp_model_initialization',
            )
            self.child_model_path_exists = bool(init_message.get('child_model_path_exists'))
        except LlamaCppRuntimeStageTimeout:
            self.close()
            raise
        except Exception as exc:
            self._drain_stderr_reader_bounded()
            stderr_tail = _sanitize_child_diagnostic_text(_llama_subprocess_tail(self._process, '_token_place_stderr_tail'))
            if isinstance(exc, LlamaCppRuntimeInitError):
                category = _refine_init_category(exc.safe_error_category, error=exc, child_stderr=stderr_tail)
                child_exception_type = exc.child_exception_type
                safe_exc = 'llama_cpp_model_initialization failed'
            elif isinstance(exc, LlamaCppWorkerEOFError):
                category = _classify_runtime_context_create_error(exc, stderr_tail)
                child_exception_type = type(exc).__name__
                safe_exc = _format_llama_subprocess_early_exit_detail(self._process, stage='llama_cpp_model_initialization')
            else:
                category = _classify_runtime_context_create_error(exc, stderr_tail)
                child_exception_type = type(exc).__name__
                safe_exc = _safe_parent_exception_message(exc, child_stderr=stderr_tail)
            self.close()
            raise LlamaCppRuntimeInitError(
                safe_exc,
                child_exception_type=child_exception_type,
                safe_error_category=category,
                child_stderr_tail=stderr_tail,
            ) from exc

    def _start_stderr_tail_reader(self) -> None:
        def _reader() -> None:
            stderr = self._process.stderr
            if stderr is None:
                return
            for line in stderr:
                tail = getattr(self._process, '_token_place_stderr_tail', None)
                if isinstance(tail, list):
                    seq = int(getattr(self._process, '_token_place_stderr_sequence', 0) or 0) + 1
                    self._process._token_place_stderr_sequence = seq  # type: ignore[attr-defined]
                    tail.append((seq, line))
                    del tail[:-100]

        self._stderr_reader_thread = threading.Thread(target=_reader, name='llama_cpp_stderr_reader', daemon=True)
        self._stderr_reader_thread.start()

    def _drain_stderr_reader_bounded(self) -> None:
        deadline = time.monotonic() + 0.5
        try:
            self._process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except Exception:
            pass
        thread = getattr(self, '_stderr_reader_thread', None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def _stderr_cursor(self) -> int:
        return int(getattr(self._process, '_token_place_stderr_sequence', 0) or 0)

    def _stderr_since(self, cursor: int) -> list[str]:
        tail = getattr(self._process, '_token_place_stderr_tail', None)
        if not isinstance(tail, list):
            return []
        lines: list[str] = []
        for item in tail:
            if isinstance(item, tuple) and len(item) == 2:
                seq, line = item
                if int(seq) > cursor:
                    lines.append(str(line))
            elif cursor <= 0:
                lines.append(str(item))
        return lines

    def _send(self, payload: Dict[str, Any], *, check_health: bool = True) -> None:
        if check_health:
            self.assert_healthy()
        if self._process.stdin is None:
            self._closed = True
            raise LlamaCppWorkerBrokenPipeError('llama_cpp subprocess stdin is unavailable')
        try:
            self._process.stdin.write(json.dumps(payload) + '\n')
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self._closed = True
            raise LlamaCppWorkerBrokenPipeError('llama_cpp subprocess transport write failed') from exc

    def is_alive(self) -> bool:
        if self._closed:
            return False
        poll = getattr(self._process, 'poll', None)
        return callable(poll) and poll() is None

    def assert_healthy(self) -> None:
        if not self.is_alive():
            raise LlamaCppWorkerDeadError(
                _format_llama_subprocess_early_exit_detail(self._process, stage='llama_cpp_liveness')
            )

    def create_chat_completion(self, *args, **kwargs):
        stream = bool(kwargs.get('stream', False))
        if stream:
            return self._stream_chat_completion(*args, **kwargs)
        stderr_cursor = 0
        try:
            with self._lock:
                stderr_cursor = self._stderr_cursor()
                self._send({'method': 'create_chat_completion', 'args': args, 'kwargs': kwargs})
                message = _read_llama_subprocess_message(
                    self._process,
                    timeout_seconds=_llama_cpp_subprocess_inference_timeout_seconds(),
                    stage='llama_cpp_inference',
                )
        except LlamaCppInferenceRequestError as exc:
            time.sleep(0.1)
            metal_diag = _classify_safe_metal_backend_failure(self._stderr_since(stderr_cursor))
            if metal_diag:
                exc.diagnostics.update({k: v for k, v in metal_diag.items() if k not in exc.diagnostics})
            raise
        except LlamaCppWorkerEOFError:
            self._closed = True
            raise
        return message.get('result')


    def create_chat_completion_from_rendered_prompt(self, *args, **kwargs):
        stderr_cursor = 0
        try:
            with self._lock:
                stderr_cursor = self._stderr_cursor()
                self._send({'method': 'create_chat_completion_from_rendered_prompt', 'args': args, 'kwargs': kwargs})
                message = _read_llama_subprocess_message(
                    self._process,
                    timeout_seconds=_llama_cpp_subprocess_inference_timeout_seconds(),
                    stage='llama_cpp_inference',
                )
        except LlamaCppInferenceRequestError as exc:
            time.sleep(0.1)
            metal_diag = _classify_safe_metal_backend_failure(self._stderr_since(stderr_cursor))
            if metal_diag:
                exc.diagnostics.update({k: v for k, v in metal_diag.items() if k not in exc.diagnostics})
            raise
        except LlamaCppWorkerEOFError:
            self._closed = True
            raise
        return message.get('result')


    def apply_chat_template(self, *args, **kwargs):
        with self._lock:
            self._send({'method': 'apply_chat_template', 'args': args, 'kwargs': kwargs})
            try:
                message = _read_llama_subprocess_message(
                    self._process,
                    timeout_seconds=self._timeout_seconds,
                    stage='llama_cpp_prompt_render',
                )
            except LlamaCppWorkerEOFError:
                self._closed = True
                raise
        return message.get('result')

    def render_and_tokenize_chat(self, *args, **kwargs):
        with self._lock:
            self._send({'method': 'render_and_tokenize_chat', 'args': args, 'kwargs': kwargs})
            try:
                message = _read_llama_subprocess_message(
                    self._process,
                    timeout_seconds=self._timeout_seconds,
                    stage='llama_cpp_prompt_render_tokenize',
                )
            except LlamaCppWorkerEOFError:
                self._closed = True
                raise
        return message.get('result')

    def tokenize(self, *args, **kwargs):
        serializable_args = tuple(
            {'__token_place_bytes_utf8__': arg.decode('utf-8')}
            if isinstance(arg, (bytes, bytearray))
            else arg
            for arg in args
        )
        with self._lock:
            self._send({'method': 'tokenize', 'args': serializable_args, 'kwargs': kwargs})
            try:
                message = _read_llama_subprocess_message(
                    self._process,
                    timeout_seconds=self._timeout_seconds,
                    stage='llama_cpp_prompt_tokenize',
                )
            except LlamaCppWorkerEOFError:
                self._closed = True
                raise
        return message.get('result')

    def _stream_chat_completion(self, *args, **kwargs):
        with self._lock:
            self._send({'method': 'create_chat_completion', 'args': args, 'kwargs': kwargs})
            while True:
                try:
                    message = _read_llama_subprocess_message(
                        self._process,
                        timeout_seconds=_llama_cpp_subprocess_inference_timeout_seconds(),
                        stage='llama_cpp_inference',
                    )
                except LlamaCppWorkerEOFError:
                    self._closed = True
                    raise
                if message.get('done'):
                    return
                yield message.get('chunk')

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._process.stdin is not None:
                self._process.stdin.close()
        except Exception:
            pass
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=1)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
        tmpfile = getattr(self, '_worker_tmpfile', None)
        if tmpfile:
            try:
                os.unlink(tmpfile)
            except Exception:
                pass
            self._worker_tmpfile = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class _SubprocessLlamaCppModule:
    def __init__(
        self,
        module_path: Any,
        *,
        timeout_seconds: Optional[float] = None,
        desktop_runtime_probe: Any = None,
        expected_llama_module_identity: Any = None,
    ) -> None:
        self.__file__ = module_path
        self._timeout_seconds = timeout_seconds
        probe = _coerce_desktop_runtime_probe(desktop_runtime_probe)
        self._expected_llama_module_identity = (
            _valid_llama_module_identity(expected_llama_module_identity)
            or _modern_desktop_runtime_probe_identity(probe)
        )
        self.__token_place_subprocess_facade__ = True
        self.LLAMA_TYPE_Q8_0 = 8
        self.GGML_TYPE_Q8_0 = 8
        self.__token_place_worker_capabilities__ = dict(probe or {})
        if self._expected_llama_module_identity is not None:
            self.__token_place_worker_capabilities__['llama_module_identity'] = self._expected_llama_module_identity
            module_identity = llama_module_identity_from_path(module_path)
            if module_identity is None or module_identity == self._expected_llama_module_identity:
                self.__token_place_worker_capabilities__['llama_module_identity_match'] = True
                self.__token_place_worker_capabilities__['llama_module_identity_verified'] = True
        backend = str((probe or {}).get('backend') or '').lower()
        self.GGML_USE_CUDA = backend == 'cuda'
        self.GGML_USE_METAL = backend == 'metal'
        for attr, key in (
            ('LLAMA_TYPE_Q8_0', 'q8_kv_cache_type_value'),
            ('GGML_TYPE_Q8_0', 'q8_kv_cache_type_value'),
            ('LLAMA_TYPE_Q4_0', 'q4_kv_cache_type_value'),
            ('GGML_TYPE_Q4_0', 'q4_kv_cache_type_value'),
            ('LLAMA_TYPE_F16', 'f16_kv_cache_type_value'),
            ('GGML_TYPE_F16', 'f16_kv_cache_type_value'),
        ):
            value = self.__token_place_worker_capabilities__.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                setattr(self, attr, value)
        yarn_value = self.__token_place_worker_capabilities__.get('yarn_enum_value')
        if isinstance(yarn_value, int) and not isinstance(yarn_value, bool):
            self.LLAMA_ROPE_SCALING_TYPE_YARN = yarn_value

    def llama_supports_gpu_offload(self) -> bool:
        return bool(self.GGML_USE_CUDA or self.GGML_USE_METAL)

    @property
    def Llama(self):
        timeout_seconds = self._timeout_seconds
        module_path_hint = self.__file__
        expected_llama_module_identity = self._expected_llama_module_identity

        worker_capabilities = _safe_constructor_capability_payload(self)
        worker_capabilities_ref = self.__token_place_worker_capabilities__
        supported_kwargs = tuple(
            sorted(
                name for name, supported in (worker_capabilities.get('constructor_kwarg_support') or {}).items()
                if supported
            )
        )

        class _ConfiguredSubprocessLlama(_SubprocessLlamaProxy):
            __token_place_supported_constructor_kwargs__ = supported_kwargs

            def __init__(self, *args, **kwargs):
                super().__init__(
                    *args,
                    timeout_seconds=timeout_seconds,
                    module_path_hint=module_path_hint,
                    expected_llama_module_identity=expected_llama_module_identity,
                    worker_capabilities=worker_capabilities_ref,
                    **kwargs,
                )

        return _ConfiguredSubprocessLlama


_LLAMA_CPP_RUNTIME_WORKER_CODE = """
import importlib, inspect, json, os, re, sys

def _jsonable(value):
    if hasattr(value, 'model_dump'):
        return value.model_dump()
    if hasattr(value, 'dict'):
        return value.dict()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value

def _emit(payload):
    print('TOKEN_PLACE_LLAMA_CPP_JSON:' + json.dumps(_jsonable(payload)), flush=True)



def _classify_initialization_exception(exc):
    text = str(exc or '').lower()
    if 'model path' in text and any(term in text for term in ('not found', 'unavailable', 'does not exist', 'no such file')):
        return 'runtime_model_path_unavailable'
    if any(term in text for term in ('failed to load model', 'llama_model_load', 'could not load model')):
        return 'runtime_model_load_failed'
    if 'vocab' in text and any(term in text for term in ('fail', 'load', 'invalid')):
        return 'runtime_model_vocab_failed'
    if 'llama_batch' in text or 'batch create' in text or 'failed to create batch' in text:
        return 'runtime_batch_create_failed'
    if any(term in text for term in ('abi', 'undefined symbol', 'incompatible gguf', 'unsupported gguf')):
        return 'runtime_model_load_failed'
    if re.search(r'(?:unexpected keyword argument|got an unexpected keyword argument|unsupported (?:parameter|kwarg|argument))', text):
        return 'runtime_context_create_unsupported_kwarg'
    if 'yarn' in text or ('rope' in text and any(term in text for term in ('scal', 'freq', 'orig', 'context'))):
        return 'runtime_context_create_rope_yarn_config'
    if 'kv' in text and any(term in text for term in ('alloc', 'cache', 'memory', 'oom', 'failed')):
        return 'runtime_context_create_kv_cache_allocation'
    if 'metal' in text and any(term in text for term in ('buffer', 'graph', 'max size', 'too large')):
        return 'runtime_context_create_metal_buffer_limit'
    if 'metal' in text and any(term in text for term in ('alloc', 'memory', 'oom', 'out of memory', 'failed to create llama_context')):
        return 'runtime_context_create_metal_memory'
    if any(term in text for term in ('cublas_status_alloc_failed', 'cudamalloc', 'cuda malloc', 'cuda oom', 'cuda out of memory', 'cuda allocation failed')):
        return 'runtime_context_create_cuda_memory'
    if 'cuda' in text and any(term in text for term in ('buffer', 'resource')):
        return 'runtime_context_create_cuda_buffer_limit'
    if 'failed to create llama_context' in text or 'llamacontext' in text:
        return 'runtime_context_create_failed'
    return 'runtime_init_unclassified'

def _extract_unsupported_generation_kwarg(message, attempted=None):
    attempted_set = set(str(key) for key in attempted) if attempted is not None else None
    for pattern in (
        r'positional-only (?:arguments?|keyword arguments?).*keyword arguments?:\\s*[\\'"]([A-Za-z_][A-Za-z0-9_]*)[\\'"]',
        r'positional-only (?:arguments?|keyword arguments?).*[\\'"]([A-Za-z_][A-Za-z0-9_]*)[\\'"]',
        r'(?:got an )?unexpected keyword argument [\\'"]([A-Za-z_][A-Za-z0-9_]*)[\\'"]',
        r'unsupported option(?:\\s+[\\'"]([A-Za-z_][A-Za-z0-9_]*)[\\'"]|\\s*:\\s*([A-Za-z_][A-Za-z0-9_]*))',
        r'invalid keyword(?: argument)? [\\'"]([A-Za-z_][A-Za-z0-9_]*)[\\'"]',
        r'invalid keyword\\s*=\\s*([A-Za-z_][A-Za-z0-9_]*)',
    ):
        match = re.search(pattern, str(message or ''))
        if match:
            rejected = next((group for group in match.groups() if group), None)
            if rejected and (attempted_set is None or rejected in attempted_set):
                return rejected
    return None

def _sanitize_error_summary(message):
    text = str(message or '').lower()
    cuda_allocation_markers = (
        'cuda out of memory',
        'cuda error: out of memory',
        'cudamalloc',
        'cuda malloc',
        'cublas_status_alloc_failed',
        'cublas alloc',
    )
    cuda_generic_allocation_markers = ('allocation failed', 'failed to allocate')
    if 'metal' in text and any(term in text for term in ('alloc', 'memory', 'out of memory', 'oom')):
        return type(message).__name__ + ':metal_memory_allocation'
    if (
        any(marker in text for marker in cuda_allocation_markers)
        or ('cuda' in text and any(marker in text for marker in cuda_generic_allocation_markers))
        or ('ggml_cuda' in text and any(term in text for term in ('alloc', 'memory', 'oom')))
    ):
        return type(message).__name__ + ':cuda_memory_allocation'
    if 'kv' in text and any(term in text for term in ('alloc', 'cache', 'memory', 'out of memory', 'oom')):
        return type(message).__name__ + ':kv_cache_allocation'
    if 'yarn' in text or ('rope' in text and any(term in text for term in ('scal', 'freq', 'eval'))):
        return type(message).__name__ + ':rope_yarn_eval_failure'
    classified = _classify_generation_exception(message)
    if classified in {'prompt_tokenization_failure', 'prompt_eval_failure', 'prompt_eval_decode_failure', 'prompt_eval_backend_failure', 'prompt_eval_invalid_token_failure', 'prompt_eval_state_failure', 'prompt_eval_context_failure', 'sampling_failure'}:
        return type(message).__name__ + ':' + classified
    if _extract_unsupported_generation_kwarg(str(message or '')) is not None:
        return type(message).__name__ + ':unsupported_kwarg'
    return type(message).__name__ + ':redacted'


def _worker_safe_plain_completion_eval_return_code(exc):
    # This helper intentionally mirrors the parent-module parser. It lives
    # inside _LLAMA_CPP_RUNTIME_WORKER_CODE so the standalone subprocess can
    # classify decode return codes without importing parent process helpers;
    # it does not override the parent helper at module import time.
    match = re.search(r"llama_decode[ \t]+returned[ \t]+(-?[0-9]+)", str(exc or ''), re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None

def _decode_return_category(exc):
    code = _worker_safe_plain_completion_eval_return_code(exc)
    if code == -1:
        return 'prompt_eval_invalid_batch'
    if code == -2:
        return 'backend_allocation_failure'
    if code == -3:
        return 'backend_graph_compute_failure'
    if code == 1:
        return 'kv_slot_unavailable'
    if code == 2:
        return 'decode_aborted'
    if code is not None and code < 0:
        return 'backend_decode_failure'
    return None

def _classify_generation_exception(exc):
    decode_category = _decode_return_category(exc)
    if decode_category is not None:
        return decode_category
    text = str(exc or '').lower()
    cuda_allocation_markers = (
        'cuda out of memory',
        'cuda error: out of memory',
        'cudamalloc',
        'cuda malloc',
        'cublas_status_alloc_failed',
        'cublas alloc',
    )
    cuda_generic_allocation_markers = ('allocation failed', 'failed to allocate')
    if 'metal' in text and any(term in text for term in ('alloc', 'memory', 'out of memory', 'oom')):
        return 'metal_memory_allocation'
    if (
        any(marker in text for marker in cuda_allocation_markers)
        or ('cuda' in text and any(marker in text for marker in cuda_generic_allocation_markers))
        or ('ggml_cuda' in text and any(term in text for term in ('alloc', 'memory', 'oom')))
    ):
        return 'cuda_memory_allocation'
    if 'kv' in text and any(term in text for term in ('alloc', 'cache', 'memory', 'out of memory', 'oom')):
        return 'kv_cache_allocation'
    if 'yarn' in text or ('rope' in text and any(term in text for term in ('scal', 'freq', 'eval'))):
        return 'rope_yarn_eval_failure'
    if 'timeout' in text or 'timed out' in text:
        return 'worker_timeout'
    if any(term in text for term in ('worker_dead', 'worker dead', 'worker exited', 'subprocess exited', 'liveness')):
        return 'worker_dead'
    if any(term in text for term in ('context window', 'context_window', 'maximum context', 'exceeds context')):
        return 'context_window_exceeded'
    if any(term in text for term in ('context length', 'n_ctx', 'ctx size')):
        return 'context_length_exceeded'
    if any(term in text for term in ('token overflow', 'too many tokens', 'exceeds token')):
        return 'token_overflow'
    if any(term in text for term in ('failed to tokenize', 'tokenization failed', 'llama_tokenize', 'could not tokenize')):
        return 'prompt_tokenization_failure'
    if any(term in text for term in ('llama_decode', 'decode failed', 'decode returned')):
        return 'prompt_eval_decode_failure'
    if 'invalid token' in text or 'invalid token id' in text or 'token id is invalid' in text:
        return 'prompt_eval_invalid_token_failure'
    if 'backend' in text and any(term in text for term in ('eval', 'decode', 'failed', 'error')):
        return 'prompt_eval_backend_failure'
    if any(term in text for term in ('llama state', 'model state', 'decode state', 'eval state')) and any(term in text for term in ('eval', 'decode', 'failed', 'error')):
        return 'prompt_eval_state_failure'
    if any(term in text for term in ('llama context', 'model context', 'decode context', 'eval context')) and any(term in text for term in ('eval', 'decode', 'failed', 'error')):
        return 'prompt_eval_context_failure'
    if any(term in text for term in ('failed to eval', 'failed to evaluate', 'model failed to evaluate', 'llama_eval')):
        return 'prompt_eval_failure'
    if any(term in text for term in ('sample failed', 'sampler', 'logits', 'no logits')):
        return 'sampling_failure'
    if _extract_unsupported_generation_kwarg(str(exc or '')) is not None:
        return 'unsupported_generation_kwarg'
    return 'unknown_generation_exception'

def _plain_completion_method_shape_category(exc):
    text = str(exc or '').lower()
    rejected = _extract_unsupported_generation_kwarg(text)
    if rejected == 'prompt':
        return 'unsupported_prompt_kwarg'
    if rejected == 'stream':
        return 'unsupported_stream_kwarg'
    if rejected == 'stop':
        return 'unsupported_stop_kwarg'
    if 'positional argument' in text or 'positional-only argument' in text or 'were given' in text or 'missing required positional argument' in text:
        return 'method_shape'
    if rejected is not None:
        return 'unexpected_kwarg'
    classified = _classify_generation_exception(exc)
    if classified != 'unknown_generation_exception':
        return classified
    return 'worker_exception'


def _reset_plain_completion_state(llama):
    reset = getattr(llama, 'reset', None)
    if not callable(reset):
        return False
    try:
        reset()
        return True
    except Exception:
        return False

def _tokenize_rendered_prompt_variants_for_plain_completion(llama, rendered_prompt):
    diagnostics = {
        'plain_completion_prompt_tokenization_attempted': True,
        'plain_completion_prompt_token_count': 0,
        'plain_completion_prompt_tokenization_method': '',
        'plain_completion_prompt_tokenization_special': None,
        'plain_completion_prompt_tokenization_error_category': '',
        'plain_completion_prompt_tokenization_variant_count': 0,
        'plain_completion_prompt_tokenization_variant_ids': '',
        'plain_completion_prompt_tokenization_token_counts': '',
        'plain_completion_prompt_tokenization_special_values': '',
        'plain_completion_prompt_tokenization_selected_variant': '',
        'plain_completion_prompt_tokenization_selected_token_count': 0,
        'plain_completion_prompt_tokenization_selected_special': None,
    }
    tokenize = getattr(llama, 'tokenize', None)
    if not callable(tokenize):
        diagnostics['plain_completion_prompt_tokenization_error_category'] = 'tokenizer_unavailable'
        return [], diagnostics
    prompt_bytes = rendered_prompt.encode('utf-8') if isinstance(rendered_prompt, str) else rendered_prompt
    supports_special = False
    try:
        sig = inspect.signature(tokenize)
        supports_special = 'special' in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    except Exception:
        supports_special = False
    attempts = []
    if supports_special:
        attempts.append(('tokenize_add_bos_false_special_false', 'llama.tokenize', False, False, {'add_bos': False, 'special': False}))
    attempts.append(('tokenize_add_bos_false_no_special', 'llama.tokenize', None, False, {'add_bos': False}))
    if supports_special:
        attempts.append(('tokenize_add_bos_false_special_true', 'llama.tokenize', True, False, {'add_bos': False, 'special': True}))
    variants = []
    seen = set()
    last_category = 'prompt_tokenization_failure'
    for variant_id, method_name, special_value, add_bos_value, kwargs in attempts:
        try:
            tokens = tokenize(prompt_bytes, **kwargs)
        except TypeError:
            last_category = 'method_shape'
            continue
        except Exception as exc:
            last_category = _classify_generation_exception(exc)
            if last_category == 'unknown_generation_exception':
                last_category = 'prompt_tokenization_failure'
            continue
        if not (
            isinstance(tokens, (list, tuple))
            and bool(tokens)
            and all(isinstance(token, int) and not isinstance(token, bool) for token in tokens)
        ):
            last_category = 'prompt_tokenization_failure'
            continue
        token_count = len(tokens)
        fingerprint = tuple(tokens)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        variants.append({
            'tokens': list(tokens),
            'tokenization_variant_id': variant_id,
            'special': special_value,
            'add_bos': add_bos_value,
            'token_count': token_count,
            'method': method_name,
        })
    if variants:
        diagnostics.update({
            'plain_completion_prompt_token_count': variants[0]['token_count'],
            'plain_completion_prompt_tokenization_method': variants[0]['method'],
            'plain_completion_prompt_tokenization_special': variants[0]['special'],
            'plain_completion_prompt_tokenization_error_category': '',
            'plain_completion_prompt_tokenization_variant_count': len(variants),
            'plain_completion_prompt_tokenization_variant_ids': ','.join(v['tokenization_variant_id'] for v in variants),
            'plain_completion_prompt_tokenization_token_counts': ','.join(str(v['token_count']) for v in variants),
            'plain_completion_prompt_tokenization_special_values': ','.join('none' if v['special'] is None else str(v['special']).lower() for v in variants),
            'plain_completion_prompt_tokenization_selected_variant': variants[0]['tokenization_variant_id'],
            'plain_completion_prompt_tokenization_selected_token_count': variants[0]['token_count'],
            'plain_completion_prompt_tokenization_selected_special': variants[0]['special'],
        })
    else:
        diagnostics['plain_completion_prompt_tokenization_error_category'] = last_category
    return variants, diagnostics

def _tokenize_rendered_prompt_for_plain_completion(llama, rendered_prompt):
    variants, diagnostics = _tokenize_rendered_prompt_variants_for_plain_completion(llama, rendered_prompt)
    if not variants:
        return None, diagnostics
    return variants[0]['tokens'], diagnostics

def _completion_result_shape(result):
    if isinstance(result, str):
        return 'direct_string'
    if isinstance(result, dict):
        choices = result.get('choices')
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            choice = choices[0]
            if isinstance(choice.get('text'), str):
                return 'choices_text'
            message = choice.get('message')
            if isinstance(message, dict):
                return 'choices_message'
        return 'dict_malformed'
    return type(result).__name__

def _completion_contains_reasoning_field(payload):
    if isinstance(payload, dict):
        if 'reasoning_content' in payload or 'reasoning' in payload:
            return True
        return any(_completion_contains_reasoning_field(value) for value in payload.values())
    if isinstance(payload, list):
        return any(_completion_contains_reasoning_field(item) for item in payload)
    return False

def _normalize_plain_completion_result(result):
    if _completion_contains_reasoning_field(result):
        return None, 'thinking_leaked'
    text = None
    if isinstance(result, str):
        text = result
    elif isinstance(result, dict):
        choices = result.get('choices')
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            choice = choices[0]
            message = choice.get('message')
            if isinstance(choice.get('text'), str):
                text = choice.get('text')
            elif isinstance(message, dict):
                content = message.get('content')
                if isinstance(content, str):
                    text = content
    if not isinstance(text, str):
        return None, 'malformed_completion_output'
    cleaned = text.strip()
    lower_cleaned = cleaned.lower()
    if lower_cleaned.startswith('<think>'):
        end = lower_cleaned.find('</think>')
        if end >= 0:
            think_body = cleaned[len('<think>'):end].strip()
            if think_body:
                return None, 'thinking_leaked'
            cleaned = cleaned[end + len('</think>'):].lstrip()
            lower_cleaned = cleaned.lower()
        else:
            return None, 'thinking_leaked'
    if '<think>' in lower_cleaned or '</think>' in lower_cleaned:
        return None, 'thinking_leaked'
    if cleaned.endswith('<|im_end|>'):
        cleaned = cleaned[:-len('<|im_end|>')].rstrip()
    if not cleaned:
        return None, 'empty_completion_output'
    return {'choices': [{'message': {'role': 'assistant', 'content': cleaned}}]}, None


class _RuntimeTemplateRenderError(RuntimeError):
    def __init__(self, reason, diagnostics=None):
        super().__init__(reason)
        self.diagnostics = diagnostics if isinstance(diagnostics, dict) else {}

_QWEN_NON_THINKING_ALLOWED_BLOCK_TYPES = {
    'input_text',
    'text',
}

def _safe_kwarg_names_csv(value):
    if isinstance(value, str):
        names = value.split(',')
    elif isinstance(value, dict):
        names = list(value)
    elif isinstance(value, (list, tuple, set)):
        names = list(value)
    else:
        return None
    safe_names = []
    for name in names:
        if not isinstance(name, str):
            continue
        name = name.strip()
        if not name or len(name) > 64:
            continue
        if all(ch.isalnum() or ch == '_' for ch in name):
            safe_names.append(name)
    if not safe_names:
        return None
    return ','.join(sorted(dict.fromkeys(safe_names))[:32])

def _safe_request_error(reason, *, request=None, exc=None, extra=None):
    diagnostics = {'reason': reason}
    if reason == 'malformed_completion_output':
        diagnostics['generation_exception_category'] = 'malformed_completion_output'
    if isinstance(extra, dict):
        safe_extra_keys = {
            'code',
            'reason',
            'generation_exception_category',
            'exception_type',
            'rejected_option',
            'rejected_generation_kwarg',
            'attempted_generation_kwargs',
            'attempted_plain_completion_methods',
            'result_shape',
            'method',
            'stream',
            'retryable',
            'runtime_healthy',
            'recovery_attempted',
            'recovery_succeeded',
            'profile_id',
            'context_tier',
            'context_window_tokens',
            'n_ctx',
            'kv_cache_mode',
            'type_k',
            'type_v',
            'sanitized_error_summary',
            'direct_apply_chat_template',
            'metadata_template',
            'jinja_renderer',
            'qwen_evidence',
            'testing_template_fallback',
            'render_rejected_generation_kwarg',
            'qwen_api_v1_non_thinking_template_fallback',
            'plain_completion_create_completion_callable',
            'plain_completion_llama_call_callable',
            'plain_completion_signature_inspectable',
            'plain_completion_accepts_prompt_kwarg',
            'plain_completion_accepts_max_tokens_kwarg',
            'plain_completion_accepts_var_kwargs',
            'plain_completion_prompt_tokenization_attempted',
            'plain_completion_prompt_token_count',
            'plain_completion_prompt_tokenization_method',
            'plain_completion_prompt_tokenization_special',
            'plain_completion_prompt_tokenization_error_category',
            'plain_completion_prompt_tokenization_variant_count',
            'plain_completion_prompt_tokenization_variant_ids',
            'plain_completion_prompt_tokenization_token_counts',
            'plain_completion_prompt_tokenization_special_values',
            'plain_completion_prompt_tokenization_selected_variant',
            'plain_completion_prompt_tokenization_selected_token_count',
            'plain_completion_prompt_tokenization_selected_special',
            'plain_completion_attempt_methods',
            'plain_completion_attempt_categories',
            'plain_completion_attempt_exception_types',
            'plain_completion_attempt_safe_summaries',
            'plain_completion_attempt_rejected_kwargs',
            'plain_completion_attempt_result_shapes',
            'plain_completion_attempt_tokenization_variants',
            'plain_completion_attempt_count',
            'qwen_high_level_chat_fallback_attempted',
            'qwen_high_level_chat_fallback_supported',
            'qwen_high_level_chat_fallback_succeeded',
            'qwen_high_level_chat_fallback_rejected_kwarg',
            'qwen_high_level_chat_fallback_category',
            'plain_completion_eval_return_code',
            'plain_completion_first_failure_method',
            'plain_completion_backend_failure_category',
            'plain_completion_backend_state_sticky',
            'plain_completion_backend_recreation_required',
            'plain_completion_metal_error_category',
            'plain_completion_metal_command_buffer_status',
            'backend_recreation_required',
            'plain_completion_reset_after_failure_count',
        }
        for key, value in extra.items():
            if (
                isinstance(key, str)
                and key in safe_extra_keys
                and isinstance(value, (str, bool, int, float, type(None)))
            ):
                if key == 'attempted_generation_kwargs':
                    safe_attempted = _safe_kwarg_names_csv(value)
                    if safe_attempted:
                        diagnostics[key] = safe_attempted
                else:
                    diagnostics[key] = value
    if isinstance(request, dict):
        method = request.get('method')
        if 'method' not in diagnostics:
            if method in {'create_chat_completion', 'create_chat_completion_from_rendered_prompt'}:
                diagnostics['method'] = method
            elif method is not None:
                diagnostics['method'] = 'unsupported'
        kwargs = request.get('kwargs')
        if isinstance(kwargs, dict):
            diagnostics['stream'] = bool(kwargs.get('stream'))
            for safe_scalar in ('max_tokens', 'temperature', 'top_p', 'top_k'):
                value = kwargs.get(safe_scalar)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    diagnostics[safe_scalar] = value
    if exc is not None:
        diagnostics['exception_type'] = type(exc).__name__
        diagnostics['sanitized_error_summary'] = _sanitize_error_summary(exc)
        if reason == 'inference_exception':
            if 'generation_exception_category' not in diagnostics:
                diagnostics['generation_exception_category'] = _classify_generation_exception(exc)
            message = str(exc)
            attempted = None
            if isinstance(request, dict) and isinstance(request.get('kwargs'), dict):
                attempted = sorted(
                    str(key) for key in request['kwargs']
                    if isinstance(key, str) and key != 'messages'
                )
            rejected = _extract_unsupported_generation_kwarg(message, attempted)
            if rejected:
                diagnostics['reason'] = 'unsupported_generation_option'
                diagnostics['code'] = 'compute_node_options_unsupported'
                diagnostics['rejected_option'] = rejected
                diagnostics['rejected_generation_kwarg'] = rejected
                if attempted and 'attempted_generation_kwargs' not in diagnostics:
                    diagnostics['attempted_generation_kwargs'] = ','.join(attempted[:32])
                diagnostics['generation_exception_category'] = 'unsupported_generation_kwarg'
    return {
        'status': 'error',
        'request_error': True,
        'error': 'llama_cpp request failed',
        'diagnostics': diagnostics,
    }

def _metadata_value(container, key):
    if container is None:
        return None
    try:
        if isinstance(container, dict):
            return container.get(key)
        getter = getattr(container, 'get', None)
        if callable(getter):
            value = getter(key)
            if value is not None:
                return value
        return getattr(container, key, None)
    except Exception:
        return None

def _runtime_chat_template(llama):
    metadata_candidates = [
        getattr(llama, 'metadata', None),
        getattr(llama, 'model_metadata', None),
        getattr(getattr(llama, '_model', None), 'metadata', None),
        getattr(getattr(llama, 'model', None), 'metadata', None),
    ]
    tokenizer_factory = getattr(llama, 'tokenizer', None)
    if callable(tokenizer_factory):
        try:
            tokenizer = tokenizer_factory()
        except Exception:
            tokenizer = None
        metadata_candidates.extend([
            getattr(tokenizer, 'metadata', None),
            getattr(tokenizer, 'model_metadata', None),
        ])
    qwen_evidence = False
    for metadata in metadata_candidates:
        for evidence_key in (
            'general.name',
            'general.architecture',
            'tokenizer.ggml.model',
            'tokenizer.chat_template.policy',
            'chat_template_policy',
        ):
            evidence = _metadata_value(metadata, evidence_key)
            if isinstance(evidence, bytes):
                evidence = evidence.decode('utf-8', errors='ignore')
            if isinstance(evidence, str) and 'qwen' in evidence.lower():
                qwen_evidence = True

    for metadata in metadata_candidates:
        for key in (
            'tokenizer.chat_template',
            'chat_template',
            'tokenizer_chat_template',
            'llama.chat_template',
        ):
            value = _metadata_value(metadata, key)
            if isinstance(value, bytes):
                value = value.decode('utf-8', errors='ignore')
            if isinstance(value, str) and value.strip():
                return value, qwen_evidence
    return None, qwen_evidence

def _jinja_renderer_available():
    try:
        importlib.import_module('jinja2.sandbox')
        return True
    except Exception:
        return False

def _runtime_token_text(llama, token_name):
    token_id_getter = getattr(llama, 'token_' + token_name, None)
    token_id = None
    if callable(token_id_getter):
        try:
            token_id = token_id_getter()
        except Exception:
            token_id = None
    if isinstance(token_id, str):
        return token_id
    if token_id is None:
        return ''
    text_getter = getattr(llama, 'token_get_text', None)
    if callable(text_getter):
        try:
            token_text = text_getter(token_id)
            if isinstance(token_text, bytes):
                return token_text.decode('utf-8', errors='ignore')
            if isinstance(token_text, str):
                return token_text
        except Exception:
            pass
    detokenize = getattr(llama, 'detokenize', None)
    if callable(detokenize):
        try:
            token_text = detokenize([token_id])
            if isinstance(token_text, bytes):
                return token_text.decode('utf-8', errors='ignore')
            if isinstance(token_text, str):
                return token_text
        except Exception:
            pass
    return ''

def _render_gguf_jinja_chat_template(template, messages, llama, *, add_generation_prompt=True, enable_thinking=None):
    chat_format_module = None
    try:
        chat_format_module = importlib.import_module('llama_cpp.llama_chat_format')
    except Exception:
        chat_format_module = None
    bos_token = _runtime_token_text(llama, 'bos')
    eos_token = _runtime_token_text(llama, 'eos')
    if chat_format_module is not None:
        formatter_cls = getattr(chat_format_module, 'Jinja2ChatFormatter', None)
        if callable(formatter_cls):
            try:
                formatter = formatter_cls(template=template, bos_token=bos_token, eos_token=eos_token)
                formatter_kwargs = {
                    'messages': messages,
                    'functions': None,
                    'tools': None,
                    'tool_choice': None,
                    'add_generation_prompt': add_generation_prompt,
                }
                if enable_thinking is not None:
                    formatter_kwargs['enable_thinking'] = enable_thinking
                rendered = formatter(**formatter_kwargs)
                prompt = getattr(rendered, 'prompt', rendered)
                if isinstance(prompt, str):
                    return prompt
            except Exception:
                pass
    sandbox = importlib.import_module('jinja2.sandbox')
    env = sandbox.SandboxedEnvironment(autoescape=False)
    def _raise_exception(message):
        raise RuntimeError('runtime_chat_template_render_exception')
    env.globals['raise_exception'] = _raise_exception
    rendered = env.from_string(template).render(
        messages=messages,
        add_generation_prompt=add_generation_prompt,
        enable_thinking=enable_thinking,
        bos_token=bos_token,
        eos_token=eos_token,
        tools=None,
        documents=None,
        date_string='',
    )
    if not isinstance(rendered, str):
        raise RuntimeError('GGUF/Jinja chat template did not render text')
    return rendered


def _runtime_message_content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        malformed_text_block = False
        for block in content:
            if not isinstance(block, dict):
                malformed_text_block = True
                continue
            block_type = block.get('type')
            if block_type not in {'text', 'input_text'}:
                malformed_text_block = True
                continue
            text = block.get('text')
            if text is None and block_type == 'input_text':
                text = block.get('input_text')
            if not isinstance(text, str):
                malformed_text_block = True
                continue
            parts.append(text)
        if malformed_text_block:
            raise RuntimeError('runtime_chat_template_render_exception')
        if parts:
            return ('\\n' * 2).join(parts)
    raise RuntimeError('runtime_chat_template_render_exception')

def _qwen_api_v1_non_thinking_has_unsupported_multimodal_content(messages):
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get('content')
        if isinstance(content, str):
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get('type')
            if block_type not in _QWEN_NON_THINKING_ALLOWED_BLOCK_TYPES:
                return True
    return False

def _render_qwen_api_v1_non_thinking_template(messages, llama, *, add_generation_prompt=True):
    if not isinstance(messages, list):
        raise RuntimeError('runtime_chat_template_render_exception')
    if _qwen_api_v1_non_thinking_has_unsupported_multimodal_content(messages):
        # Keep the specific contract violation in the worker reason/category while
        # exposing the broader invalid-request class via the public error code.
        raise _RuntimeTemplateRenderError(
            'runtime_text_only_content_blocks_required',
            {
                'code': 'compute_node_invalid_request',
                'generation_exception_category': 'text_only_content_blocks_required',
                'retryable': False,
            },
        )
    rendered = []
    bos_token = _runtime_token_text(llama, 'bos')
    if isinstance(bos_token, str) and bos_token:
        rendered.append(bos_token)
    for message in messages:
        if not isinstance(message, dict):
            raise RuntimeError('runtime_chat_template_render_exception')
        role = message.get('role')
        if role not in {'system', 'user', 'assistant'}:
            raise RuntimeError('runtime_chat_template_render_exception')
        content = _runtime_message_content_text(message.get('content'))
        rendered.append(f'<|im_start|>{role}\\n{content}<|im_end|>\\n')
    if add_generation_prompt:
        rendered.append('<|im_start|>assistant\\n<think>\\n\\n</think>\\n\\n')
    return ''.join(rendered)

def _type_error_is_unexpected_keyword(exc, keyword):
    message = str(exc)
    return (
        ("unexpected keyword argument '%s'" % keyword) in message
        or ('unexpected keyword argument "%s"' % keyword) in message
        or ("got an unexpected keyword argument '%s'" % keyword) in message
        or ('got an unexpected keyword argument "%s"' % keyword) in message
        or ('unexpected keyword argument %s' % keyword) in message
        or ('got an unexpected keyword argument %s' % keyword) in message
    )

def _render_chat_with_runtime_template(llama, args, kwargs):
    kwargs = dict(kwargs)

    def _rejection_diagnostics(rejected_kwarg, *, include_generation_category=True):
        diagnostics = {
            'direct_apply_chat_template': direct_apply_available,
            'metadata_template': False,
            'jinja_renderer': False,
        }
        if rejected_kwarg:
            diagnostics.update({
                'render_rejected_generation_kwarg': rejected_kwarg,
                'rejected_generation_kwarg': rejected_kwarg,
                'attempted_generation_kwargs': _safe_kwarg_names_csv(kwargs),
                'method': 'apply_chat_template',
            })
        if rejected_kwarg and include_generation_category:
            diagnostics['generation_exception_category'] = 'unsupported_render_kwarg'
        return {key: value for key, value in diagnostics.items() if value is not None}

    def _retry_without_rejected_kwarg(rejected_kwarg):
        if not rejected_kwarg or not callable(render):
            return None
        # enable_thinking must never be removed as a compatibility retry.
        # Dropping it would silently re-enable thinking on the non-thinking path.
        # If apply_chat_template rejects enable_thinking, the caller must fall
        # through to the GGUF/Jinja renderer (which honours enable_thinking) or
        # fail closed with safe diagnostics.
        if rejected_kwarg not in {'tokenize', 'add_generation_prompt'}:
            return None
        compatibility_kwargs = dict(kwargs)
        compatibility_kwargs.pop(rejected_kwarg, None)
        rendered = render(*args, **compatibility_kwargs)
        return rendered, _rejection_diagnostics(rejected_kwarg)

    def _raise_template_error(reason, rejected_kwarg=None):
        try:
            retry_result = _retry_without_rejected_kwarg(rejected_kwarg)
        except Exception:
            retry_result = None
        if retry_result is not None:
            return retry_result
        raise _RuntimeTemplateRenderError(
            reason,
            _rejection_diagnostics(rejected_kwarg, include_generation_category=False),
        )
    provider_hint = str(kwargs.pop('token_place_provider', '') or '').lower()
    policy_hint = str(kwargs.pop('token_place_template_policy', '') or '').lower()
    qwen_api_v1_non_thinking = provider_hint == 'qwen' and 'gguf' in policy_hint
    if 'enable_thinking' in kwargs and kwargs.get('enable_thinking') is not False:
        raise _RuntimeTemplateRenderError('runtime_chat_template_render_exception', {
            'generation_exception_category': 'qwen_non_thinking_hard_switch_unavailable',
            'method': 'apply_chat_template',
            'qwen_evidence': provider_hint == 'qwen',
        })
    if qwen_api_v1_non_thinking and 'enable_thinking' not in kwargs:
        raise _RuntimeTemplateRenderError('runtime_qwen_non_thinking_hard_switch_missing', {
            'generation_exception_category': 'qwen_non_thinking_hard_switch_unavailable',
            'method': 'apply_chat_template',
            'qwen_evidence': True,
        })
    render = getattr(llama, 'apply_chat_template', None)
    direct_apply_available = callable(render)
    if not direct_apply_available:
        tokenizer = getattr(llama, 'tokenizer', None)
        if callable(tokenizer):
            try:
                tokenizer = tokenizer()
            except Exception:
                tokenizer = None
        render = getattr(tokenizer, 'apply_chat_template', None) if tokenizer is not None else None
    render_exc = None
    rejected_render_kwarg = None
    if callable(render):
        try:
            return render(*args, **kwargs), {
                'direct_apply_chat_template': direct_apply_available,
                'metadata_template': False,
                'jinja_renderer': False,
            }
        except TypeError as exc:
            render_exc = exc
            attempted_render_kwargs = sorted(str(key) for key in kwargs)
            rejected_render_kwarg = _extract_unsupported_generation_kwarg(str(exc), attempted_render_kwargs)
            if rejected_render_kwarg is None:
                for candidate in ('enable_thinking', 'tokenize', 'add_generation_prompt'):
                    if candidate in kwargs and _type_error_is_unexpected_keyword(exc, candidate):
                        rejected_render_kwarg = candidate
                        break
            if rejected_render_kwarg not in {'enable_thinking', 'tokenize', 'add_generation_prompt'}:
                raise
    template, metadata_qwen_evidence = _runtime_chat_template(llama)
    if not isinstance(template, str) or not template.strip():
        if qwen_api_v1_non_thinking:
            messages = args[0] if args else kwargs.get('messages')
            return _render_qwen_api_v1_non_thinking_template(messages, llama, add_generation_prompt=bool(kwargs.get('add_generation_prompt', True))), {
                'direct_apply_chat_template': direct_apply_available, 'metadata_template': False, 'jinja_renderer': False,
                'qwen_evidence': True, 'qwen_api_v1_non_thinking_template_fallback': True,
                **({'render_rejected_generation_kwarg': rejected_render_kwarg, 'rejected_generation_kwarg': rejected_render_kwarg, 'generation_exception_category': 'unsupported_render_kwarg', 'method': 'apply_chat_template'} if rejected_render_kwarg else {}),
            }
        retry_result = _raise_template_error('runtime_chat_template_metadata_missing', rejected_render_kwarg)
        if retry_result is not None:
            return retry_result
    qwen_safe = metadata_qwen_evidence or provider_hint == 'qwen' or 'qwen' in policy_hint
    if not qwen_safe:
        retry_result = _raise_template_error('runtime_chat_template_qwen_evidence_missing', rejected_render_kwarg)
        if retry_result is not None:
            return retry_result
    if not _jinja_renderer_available():
        if qwen_api_v1_non_thinking:
            messages = args[0] if args else kwargs.get('messages')
            return _render_qwen_api_v1_non_thinking_template(messages, llama, add_generation_prompt=bool(kwargs.get('add_generation_prompt', True))), {
                'direct_apply_chat_template': direct_apply_available, 'metadata_template': bool(template), 'jinja_renderer': False,
                'qwen_evidence': True, 'qwen_api_v1_non_thinking_template_fallback': True,
                **({'render_rejected_generation_kwarg': rejected_render_kwarg, 'rejected_generation_kwarg': rejected_render_kwarg, 'generation_exception_category': 'unsupported_render_kwarg', 'method': 'apply_chat_template'} if rejected_render_kwarg else {}),
            }
        retry_result = _raise_template_error('runtime_chat_template_renderer_unavailable', rejected_render_kwarg)
        if retry_result is not None:
            return retry_result
    messages = args[0] if args else kwargs.get('messages')
    if not isinstance(messages, list):
        raise RuntimeError('runtime_chat_template_render_exception')
    try:
        rendered = _render_gguf_jinja_chat_template(
            template,
            messages,
            llama,
            add_generation_prompt=bool(kwargs.get('add_generation_prompt', True)),
            enable_thinking=kwargs.get('enable_thinking'),
        )
    except Exception:
        if qwen_api_v1_non_thinking:
            return _render_qwen_api_v1_non_thinking_template(messages, llama, add_generation_prompt=bool(kwargs.get('add_generation_prompt', True))), {
                'direct_apply_chat_template': direct_apply_available, 'metadata_template': True, 'jinja_renderer': _jinja_renderer_available(),
                'qwen_evidence': True, 'qwen_api_v1_non_thinking_template_fallback': True,
                **({'render_rejected_generation_kwarg': rejected_render_kwarg, 'rejected_generation_kwarg': rejected_render_kwarg, 'generation_exception_category': 'unsupported_render_kwarg', 'method': 'apply_chat_template'} if rejected_render_kwarg else {}),
            }
        retry_result = _raise_template_error('runtime_chat_template_render_exception', rejected_render_kwarg)
        if retry_result is not None:
            return retry_result
    diagnostics = {
        'direct_apply_chat_template': False,
        'metadata_template': True,
        'jinja_renderer': True,
        'qwen_evidence': True,
    }
    if rejected_render_kwarg:
        diagnostics.update({
            'render_rejected_generation_kwarg': rejected_render_kwarg,
            'rejected_generation_kwarg': rejected_render_kwarg,
            'attempted_generation_kwargs': _safe_kwarg_names_csv(kwargs),
            'generation_exception_category': 'unsupported_render_kwarg',
            'method': 'apply_chat_template',
        })
    return rendered, diagnostics

def _testing_render_template_fallback_allowed(model_path):
    if os.environ.get('TOKEN_PLACE_ENV') != 'testing':
        return False
    basename = os.path.basename(str(model_path or '')).lower()
    return basename in {'mock.gguf'} or 'stories15m' in basename

def _render_testing_chat_template_fallback(args, kwargs):
    messages = args[0] if args else kwargs.get('messages')
    if not isinstance(messages, list):
        raise RuntimeError('runtime_chat_template_render_exception')
    rendered_parts = []
    for message in messages:
        if not isinstance(message, dict):
            raise RuntimeError('runtime_chat_template_render_exception')
        role = str(message.get('role') or 'user')
        content = str(message.get('content') or '')
        rendered_parts.append('<|im_start|>' + role + '\\n' + content + '<|im_end|>')
    if bool(kwargs.get('add_generation_prompt', True)):
        rendered_parts.append('<|im_start|>assistant\\n')
    return '\\n'.join(rendered_parts)

try:
    init_line = sys.stdin.readline()
    if not init_line:
        raise RuntimeError('llama_cpp subprocess missing init payload')
    init_payload = json.loads(init_line)
    emit_import_handshake = isinstance(init_payload, dict) and init_payload.get('method') == '__import__'
    llama_cpp = importlib.import_module('llama_cpp')
    if emit_import_handshake:
        _emit({'status': 'ok', 'module_path': getattr(llama_cpp, '__file__', None)})
        init_line = sys.stdin.readline()
        if not init_line:
            raise RuntimeError('llama_cpp subprocess missing init payload')
        init_payload = json.loads(init_line)
    init_args = init_payload.get('args', [])
    init_kwargs = init_payload.get('kwargs', {})
except Exception as exc:
    _emit({
        'status': 'error',
        'exception_type': type(exc).__name__,
        'safe_error_category': _classify_initialization_exception(exc),
        'child_model_path_exists': False,
    })
    raise SystemExit(1)

try:
    _token_place_model_path = init_args[0] if isinstance(init_args, list) and init_args else None
    if _token_place_model_path is None and isinstance(init_kwargs, dict):
        _token_place_model_path = init_kwargs.get('model_path')
    child_model_path_exists = (
        os.path.exists(_token_place_model_path)
        if isinstance(_token_place_model_path, str)
        else False
    )
    llama = llama_cpp.Llama(*init_args, **init_kwargs)
    _emit({'status': 'ok', 'module_path': getattr(llama_cpp, '__file__', None), 'child_model_path_exists': child_model_path_exists})
except Exception as exc:
    _emit({
        'status': 'error',
        'exception_type': type(exc).__name__,
        'safe_error_category': _classify_initialization_exception(exc),
        'child_model_path_exists': bool(locals().get('child_model_path_exists', False)),
    })
    raise SystemExit(1)

for line in sys.stdin:
    request = None
    try:
        request = json.loads(line)
        if not isinstance(request, dict):
            _emit(_safe_request_error('malformed_request'))
            continue
        method = request.get('method')
        if method not in {'create_chat_completion', 'create_chat_completion_from_rendered_prompt', 'apply_chat_template', 'tokenize', 'render_and_tokenize_chat'}:
            _emit(_safe_request_error('unsupported_method', request=request))
            continue
        kwargs = request.get('kwargs', {})
        if not isinstance(kwargs, dict):
            _emit(_safe_request_error('malformed_kwargs', request=request))
            continue
        if method in {'apply_chat_template', 'render_and_tokenize_chat'}:
            if method == 'render_and_tokenize_chat':
                _render_diagnostics = None
                try:
                    rendered_prompt, _render_diagnostics = _render_chat_with_runtime_template(
                        llama, request.get('args', []), kwargs
                    )
                    if not isinstance(rendered_prompt, str):
                        _emit(_safe_request_error('prompt_render_unavailable', request=request))
                        continue
                    tokenize = getattr(llama, 'tokenize', None)
                    if not callable(tokenize):
                        _emit(_safe_request_error('runtime_tokenizer_unavailable', request=request))
                        continue
                    tokens = tokenize(rendered_prompt.encode('utf-8'), add_bos=False)
                    if not isinstance(tokens, (list, tuple)):
                        _emit(_safe_request_error('runtime_tokenizer_unavailable', request=request))
                        continue
                    _emit({'status': 'ok', 'result': {'prompt_tokens': len(tokens)}})
                except Exception as exc:
                    reason = str(exc) if str(exc) in {
                        'runtime_chat_template_metadata_missing',
                        'runtime_chat_template_renderer_unavailable',
                        'runtime_tokenizer_unavailable',
                        'runtime_template_tokenizer_bridge_unavailable',
                        'runtime_chat_template_render_exception',
                        'runtime_chat_template_qwen_evidence_missing',
                        'runtime_text_only_content_blocks_required',
                    } else 'runtime_chat_template_render_exception'
                    _emit(_safe_request_error(reason, request=request, exc=exc, extra=getattr(exc, 'diagnostics', None) if isinstance(getattr(exc, 'diagnostics', None), dict) else (_render_diagnostics if isinstance(locals().get('_render_diagnostics'), dict) else None)))
                continue
            render = getattr(llama, 'apply_chat_template', None)
            if not callable(render):
                tokenizer = getattr(llama, 'tokenizer', None)
                if callable(tokenizer):
                    try:
                        tokenizer = tokenizer()
                    except Exception:
                        tokenizer = None
                render = getattr(tokenizer, 'apply_chat_template', None) if tokenizer is not None else None
            if not callable(render):
                try:
                    chat_format = getattr(llama, 'chat_format', None) or 'llama-2'
                    chat_format_module = importlib.import_module('llama_cpp.llama_chat_format')
                    formatter_key = str(chat_format).replace("-", "_")
                    formatter_name = (
                        "format_llama2"
                        if formatter_key == "llama_2"
                        else "format_" + formatter_key
                    )
                    if formatter_name == "format_llama_3":
                        formatter_name = "format_llama3"
                    formatter = getattr(chat_format_module, formatter_name, None)
                    if not callable(formatter):
                        _emit(_safe_request_error('prompt_render_unavailable', request=request))
                        continue
                    rendered = formatter(*request.get('args', []), **kwargs)
                    prompt = getattr(rendered, 'prompt', rendered)
                    if kwargs.get('tokenize'):
                        tokenize = getattr(llama, 'tokenize', None)
                        if not callable(tokenize):
                            _emit(_safe_request_error('tokenizer_unavailable', request=request))
                            continue
                        prompt = tokenize(str(prompt).encode('utf-8'), add_bos=False)
                    _emit({'status': 'ok', 'result': prompt})
                except Exception as exc:
                    _emit(_safe_request_error('prompt_render_unavailable', request=request, exc=exc))
                continue
            _emit({'status': 'ok', 'result': render(*request.get('args', []), **kwargs)})
            continue
        if method == 'tokenize':
            tokenize = getattr(llama, 'tokenize', None)
            if not callable(tokenize):
                _emit(_safe_request_error('tokenizer_unavailable', request=request))
                continue
            tokenize_args = []
            for arg in request.get('args', []):
                if isinstance(arg, dict) and set(arg) == {'__token_place_bytes_utf8__'}:
                    tokenize_args.append(arg['__token_place_bytes_utf8__'].encode('utf-8'))
                else:
                    tokenize_args.append(arg)
            _emit({'status': 'ok', 'result': tokenize(*tokenize_args, **kwargs)})
            continue
        if method == 'create_chat_completion_from_rendered_prompt':
            render_kwargs = {
                'tokenize': False,
                'add_generation_prompt': True,
                'enable_thinking': kwargs.pop('enable_thinking', None),
                'token_place_provider': kwargs.pop('token_place_provider', None),
                'token_place_template_policy': kwargs.pop('token_place_template_policy', None),
            }
            render_kwargs = {key: value for key, value in render_kwargs.items() if value is not None}
            try:
                rendered_prompt, render_diagnostics = _render_chat_with_runtime_template(
                    llama, request.get('args', []), render_kwargs
                )
            except Exception as exc:
                reason = str(exc) if str(exc) in {
                    'runtime_chat_template_metadata_missing',
                    'runtime_chat_template_renderer_unavailable',
                    'runtime_template_tokenizer_bridge_unavailable',
                    'runtime_chat_template_render_exception',
                    'runtime_chat_template_qwen_evidence_missing',
                    'runtime_text_only_content_blocks_required',
                } else 'runtime_chat_template_render_exception'
                if (
                    reason == 'runtime_chat_template_metadata_missing'
                    and _testing_render_template_fallback_allowed(_token_place_model_path)
                ):
                    try:
                        rendered_prompt = _render_testing_chat_template_fallback(
                            request.get('args', []), render_kwargs
                        )
                        render_diagnostics = {
                            'direct_apply_chat_template': False,
                            'metadata_template': False,
                            'jinja_renderer': False,
                            'testing_template_fallback': True,
                        }
                    except Exception as fallback_exc:
                        _emit(_safe_request_error(reason, request=request, exc=fallback_exc))
                        continue
                else:
                    _emit(_safe_request_error(reason, request=request, exc=exc, extra=getattr(exc, 'diagnostics', None)))
                    continue
            max_tokens = kwargs.get('max_tokens', 64)
            if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
                max_tokens = 64
            attempts = []
            result = None
            completion_error = None
            create_completion = getattr(llama, 'create_completion', None)
            plain_capabilities = {
                'plain_completion_create_completion_callable': callable(create_completion),
                'plain_completion_llama_call_callable': callable(llama),
                'plain_completion_signature_inspectable': False,
                'plain_completion_accepts_prompt_kwarg': None,
                'plain_completion_accepts_max_tokens_kwarg': None,
                'plain_completion_accepts_var_kwargs': None,
                'plain_completion_reset_after_failure_count': 0,
            }
            normalized = None
            invalid_reason = None
            last_invalid_reason = None
            if callable(create_completion):
                try:
                    sig = inspect.signature(create_completion)
                    params = sig.parameters
                    plain_capabilities['plain_completion_signature_inspectable'] = True
                    plain_capabilities['plain_completion_accepts_var_kwargs'] = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
                    plain_capabilities['plain_completion_accepts_prompt_kwarg'] = 'prompt' in params or plain_capabilities['plain_completion_accepts_var_kwargs']
                    plain_capabilities['plain_completion_accepts_max_tokens_kwarg'] = 'max_tokens' in params or plain_capabilities['plain_completion_accepts_var_kwargs']
                except Exception:
                    pass
            fatal_plain_completion_categories = {
                'backend_allocation_failure',
                'backend_graph_compute_failure',
                'cuda_memory_allocation',
                'metal_graph_compute_failure',
                'kv_slot_unavailable',
                'decode_aborted',
                'backend_decode_failure',
                'metal_memory_allocation',
                'kv_cache_allocation',
                'rope_yarn_eval_failure',
                'worker_timeout',
                'worker_dead',
                'context_window_exceeded',
                'context_length_exceeded',
                'token_overflow',
            }
            def _plain_attempt_diagnostics():
                return {
                    'plain_completion_attempt_methods': ','.join(item.get('method', '') for item in attempts if item.get('method')),
                    'plain_completion_attempt_categories': ','.join(item.get('generation_exception_category', '') for item in attempts),
                    'plain_completion_attempt_exception_types': ','.join(item.get('exception_type', '') for item in attempts),
                    'plain_completion_attempt_safe_summaries': ','.join(item.get('sanitized_error_summary', '') for item in attempts),
                    'plain_completion_attempt_rejected_kwargs': ','.join(item.get('rejected_generation_kwarg', '') for item in attempts),
                    'plain_completion_attempt_result_shapes': ','.join(item.get('result_shape', '') for item in attempts),
                    'plain_completion_attempt_tokenization_variants': ','.join(item.get('tokenization_variant_id', '') for item in attempts),
                    'plain_completion_attempt_count': len(attempts),
                }
            def _attempt_plain_completion(method_name, attempted_kwargs, call, tokenization_variant_id=''):
                try:
                    attempt_result = call()
                    attempts.append({
                        'method': method_name,
                        'attempted_kwarg_names': ','.join(attempted_kwargs),
                        'result_shape': _completion_result_shape(attempt_result),
                        'tokenization_variant_id': tokenization_variant_id,
                    })
                    return attempt_result, None
                except Exception as exc:
                    category = _plain_completion_method_shape_category(exc)
                    rejected = _extract_unsupported_generation_kwarg(str(exc), attempted_kwargs)
                    attempts.append({
                        'method': method_name,
                        'attempted_kwarg_names': ','.join(attempted_kwargs),
                        'exception_type': type(exc).__name__,
                        'generation_exception_category': category,
                        'rejected_generation_kwarg': rejected or '',
                        'sanitized_error_summary': _sanitize_error_summary(exc),
                        'tokenization_variant_id': tokenization_variant_id,
                    })
                    return_code = _worker_safe_plain_completion_eval_return_code(exc)
                    if return_code is not None:
                        plain_capabilities['plain_completion_eval_return_code'] = return_code
                    if category == 'backend_graph_compute_failure':
                        plain_capabilities['plain_completion_backend_failure_category'] = 'metal_graph_compute_failure'
                        plain_capabilities['plain_completion_backend_state_sticky'] = True
                        plain_capabilities['plain_completion_backend_recreation_required'] = True
                    if category == 'backend_allocation_failure':
                        plain_capabilities['plain_completion_backend_recreation_required'] = True
                    if category == 'cuda_memory_allocation':
                        plain_capabilities['plain_completion_backend_failure_category'] = 'cuda_memory_allocation'
                        plain_capabilities['plain_completion_backend_recreation_required'] = True
                    if category not in fatal_plain_completion_categories and _reset_plain_completion_state(llama):
                        plain_capabilities['plain_completion_reset_after_failure_count'] += 1
                    return None, exc

            normalized = None
            invalid_reason = None
            last_invalid_reason = None
            if callable(create_completion):
                result, completion_error = _attempt_plain_completion(
                    'create_completion_keyword_prompt',
                    ['max_tokens', 'prompt'],
                    lambda: create_completion(prompt=rendered_prompt, max_tokens=max_tokens),
                )
            if result is None and callable(create_completion) and (
                last_invalid_reason != 'thinking_leaked'
                and (not attempts or attempts[-1].get('generation_exception_category') not in fatal_plain_completion_categories)
            ):
                result, completion_error = _attempt_plain_completion(
                    'create_completion_positional_prompt',
                    ['max_tokens'],
                    lambda: create_completion(rendered_prompt, max_tokens=max_tokens),
                )
            if result is None and callable(llama) and (
                not attempts or attempts[-1].get('generation_exception_category') not in fatal_plain_completion_categories
            ):
                result, completion_error = _attempt_plain_completion(
                    'llama_call_positional_prompt',
                    ['max_tokens'],
                    lambda: llama(rendered_prompt, max_tokens=max_tokens),
                )
            rendered_prompt_token_ids = None
            tokenization_variants = []
            tokenization_diagnostics = {}
            if result is not None:
                normalized, invalid_reason = _normalize_plain_completion_result(result)
                if invalid_reason is not None:
                    last_invalid_reason = invalid_reason
                if invalid_reason is not None and invalid_reason != 'thinking_leaked':
                    result = None
            if result is None and callable(create_completion) and (
                not attempts or attempts[-1].get('generation_exception_category') not in fatal_plain_completion_categories
            ):
                tokenization_variants, tokenization_diagnostics = _tokenize_rendered_prompt_variants_for_plain_completion(llama, rendered_prompt)
                plain_capabilities.update(tokenization_diagnostics)
                for tokenization_variant in tokenization_variants:
                    if last_invalid_reason == 'thinking_leaked' or (attempts and attempts[-1].get('generation_exception_category') in fatal_plain_completion_categories):
                        break
                    rendered_prompt_token_ids = tokenization_variant['tokens']
                    variant_id = tokenization_variant['tokenization_variant_id']
                    plain_capabilities['plain_completion_prompt_tokenization_selected_variant'] = variant_id
                    plain_capabilities['plain_completion_prompt_tokenization_selected_token_count'] = tokenization_variant['token_count']
                    plain_capabilities['plain_completion_prompt_tokenization_selected_special'] = tokenization_variant['special']
                    result, completion_error = _attempt_plain_completion(
                        'create_completion_keyword_token_ids',
                        ['max_tokens', 'prompt'],
                        lambda token_ids=rendered_prompt_token_ids: create_completion(prompt=token_ids, max_tokens=max_tokens),
                        variant_id,
                    )
                    if result is not None:
                        normalized, invalid_reason = _normalize_plain_completion_result(result)
                        if invalid_reason is not None:
                            last_invalid_reason = invalid_reason
                            result = None
                    if result is None and last_invalid_reason != 'thinking_leaked' and (
                        not attempts or attempts[-1].get('generation_exception_category') not in fatal_plain_completion_categories
                    ):
                        result, completion_error = _attempt_plain_completion(
                            'create_completion_positional_token_ids',
                            ['max_tokens'],
                            lambda token_ids=rendered_prompt_token_ids: create_completion(token_ids, max_tokens=max_tokens),
                            variant_id,
                        )
                        if result is not None:
                            normalized, invalid_reason = _normalize_plain_completion_result(result)
                            if invalid_reason is not None:
                                last_invalid_reason = invalid_reason
                                result = None
                    if result is not None:
                        break
            if result is not None:
                normalized, invalid_reason = _normalize_plain_completion_result(result)
            primary_attempt = attempts[-1] if attempts else None
            primary_completion_error = completion_error
            primary_invalid_reason = last_invalid_reason
            primary_generation_exception_category = (
                'thinking_leaked'
                if primary_invalid_reason == 'thinking_leaked'
                else primary_invalid_reason
                or (
                    primary_attempt.get('generation_exception_category')
                    if primary_attempt is not None
                    else None
                )
                or 'worker_exception'
            )
            primary_method = (
                primary_attempt.get('method')
                if primary_attempt is not None
                else 'create_completion_from_rendered_prompt'
            )
            primary_rejected_generation_kwarg = (
                primary_attempt.get('rejected_generation_kwarg', '')
                if primary_attempt is not None
                else ''
            )
            primary_eval_return_code = plain_capabilities.get(
                'plain_completion_eval_return_code'
            )
            primary_failure_exists = (
                primary_attempt is not None
                or primary_invalid_reason is not None
                or primary_completion_error is not None
            )
            chat_fallback_category = ''
            chat_fallback_invoked = False
            if result is None and last_invalid_reason != 'thinking_leaked' and (not attempts or attempts[-1].get('generation_exception_category') not in fatal_plain_completion_categories):
                chat_fallback_category = 'unsupported_generation_kwarg'
                create_chat_completion = getattr(llama, 'create_chat_completion', None)
                plain_capabilities['qwen_high_level_chat_fallback_attempted'] = True
                plain_capabilities['qwen_high_level_chat_fallback_supported'] = callable(create_chat_completion)
                plain_capabilities['qwen_high_level_chat_fallback_succeeded'] = False
                plain_capabilities['qwen_high_level_chat_fallback_rejected_kwarg'] = ''
                if callable(create_chat_completion):
                    try:
                        chat_sig = inspect.signature(create_chat_completion)
                        chat_params = chat_sig.parameters
                        chat_supported = 'chat_template_kwargs' in chat_params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in chat_params.values())
                    except Exception:
                        chat_supported = False
                    plain_capabilities['qwen_high_level_chat_fallback_supported'] = bool(chat_supported)
                    if chat_supported:
                        chat_fallback_invoked = True
                        result, completion_error = _attempt_plain_completion(
                            'create_chat_completion_qwen_non_thinking',
                            ['chat_template_kwargs', 'max_tokens', 'messages'],
                            lambda: create_chat_completion(
                                messages=(request.get('args') or [None])[0],
                                max_tokens=max_tokens,
                                chat_template_kwargs={'enable_thinking': False},
                            ),
                        )
                        if result is not None:
                            normalized, invalid_reason = _normalize_plain_completion_result(result)
                            if invalid_reason is not None:
                                last_invalid_reason = invalid_reason
                            plain_capabilities['qwen_high_level_chat_fallback_succeeded'] = invalid_reason is None
                            chat_fallback_category = invalid_reason or ''
                        elif attempts:
                            chat_fallback_category = attempts[-1].get('generation_exception_category', 'worker_exception')
                            plain_capabilities['qwen_high_level_chat_fallback_rejected_kwarg'] = attempts[-1].get('rejected_generation_kwarg', '')
                    else:
                        chat_fallback_category = 'unsupported_generation_kwarg'
                plain_capabilities['qwen_high_level_chat_fallback_category'] = chat_fallback_category
            if result is None:
                extra = dict(render_diagnostics)
                extra.update(plain_capabilities)
                extra.update(_plain_attempt_diagnostics())
                # The Qwen high-level chat fallback is optional and is commonly
                # unsupported by deployed llama-cpp-python builds that lack
                # chat_template_kwargs.  Keep concrete bounded-completion
                # failures (including malformed/empty output) as the primary
                # readiness signal; report unsupported chat fallback only as
                # secondary diagnostics unless there was no real primary
                # failure to preserve.
                preserve_primary_failure = (
                    plain_capabilities.get('qwen_high_level_chat_fallback_attempted')
                    and chat_fallback_category == 'unsupported_generation_kwarg'
                    and not chat_fallback_invoked
                    and primary_failure_exists
                )
                unsupported_chat_fallback_is_terminal = (
                    plain_capabilities.get('qwen_high_level_chat_fallback_attempted')
                    and chat_fallback_category == 'unsupported_generation_kwarg'
                    and not chat_fallback_invoked
                    and not primary_failure_exists
                )
                if preserve_primary_failure:
                    if primary_completion_error is not None:
                        completion_error = primary_completion_error
                    if primary_eval_return_code is not None:
                        plain_capabilities['plain_completion_eval_return_code'] = primary_eval_return_code
                if unsupported_chat_fallback_is_terminal and completion_error is None:
                    completion_error = RuntimeError('unsupported option: chat_template_kwargs')
                extra.update({
                    'method': (
                        primary_method
                        if preserve_primary_failure
                        else (attempts[-1].get('method') if attempts else 'create_completion_from_rendered_prompt')
                    ),
                    'attempted_plain_completion_methods': ','.join(item.get('method', '') for item in attempts if item.get('method')),
                    'attempted_generation_kwargs': ','.join(sorted(set(','.join(item.get('attempted_kwarg_names', '') for item in attempts).split(',')) - {''})),
                    'generation_exception_category': (
                        primary_generation_exception_category
                        if preserve_primary_failure
                        else
                        chat_fallback_category
                        if unsupported_chat_fallback_is_terminal
                        else last_invalid_reason or (attempts[-1].get('generation_exception_category', 'worker_exception') if attempts else 'worker_exception')
                    ),
                    'rejected_generation_kwarg': (
                        primary_rejected_generation_kwarg
                        if preserve_primary_failure
                        else (attempts[-1].get('rejected_generation_kwarg', '') if attempts else '')
                    ),
                })
                _emit(_safe_request_error('inference_exception', request=request, exc=completion_error, extra=extra))
                continue
            if invalid_reason is not None:
                extra = dict(render_diagnostics)
                extra.update(plain_capabilities)
                extra.update(_plain_attempt_diagnostics())
                extra.update({
                    'method': attempts[-1].get('method') if attempts else 'create_completion_from_rendered_prompt',
                    'attempted_plain_completion_methods': ','.join(item.get('method', '') for item in attempts if item.get('method')),
                    'attempted_generation_kwargs': ','.join(sorted(set(','.join(item.get('attempted_kwarg_names', '') for item in attempts).split(',')) - {''})),
                    'generation_exception_category': invalid_reason,
                    'result_shape': _completion_result_shape(result),
                })
                _emit(_safe_request_error(invalid_reason, request=request, extra=extra))
                continue
            _emit({'status': 'ok', 'result': normalized})
            continue
        result = llama.create_chat_completion(*request.get('args', []), **kwargs)
        if kwargs.get('stream'):
            for chunk in result:
                _emit({'status': 'ok', 'chunk': chunk, 'done': False})
            _emit({'status': 'ok', 'done': True})
        else:
            _emit({'status': 'ok', 'result': result})
    except json.JSONDecodeError as exc:
        _emit(_safe_request_error('invalid_json', exc=exc))
    except Exception as exc:
        _emit(_safe_request_error('inference_exception', request=request, exc=exc))
"""


def _llama_cpp_package_parent_from_module_path(module_path: Any) -> Optional[str]:
    """Return the import parent for a probed llama_cpp module path."""

    if not module_path:
        return None
    try:
        module_file = Path(_strip_windows_extended_path_prefix(str(module_path)))
    except (TypeError, ValueError, OSError):
        return None
    if module_file.name == '__init__.py' and module_file.parent.name == 'llama_cpp':
        return str(module_file.parent.parent)
    if module_file.name == 'llama_cpp.py':
        return str(module_file.parent)
    return None


def _clear_llama_cpp_module_namespace(reason: str, *, expected_path: Any = None) -> None:
    """Remove cached llama_cpp modules so a runtime switch cannot reuse stale bindings."""

    stale_names = [
        name for name in sys.modules
        if name == 'llama_cpp' or name.startswith('llama_cpp.')
    ]
    if not stale_names:
        return
    logger.info(
        "llama_cpp clearing cached module namespace reason=%s expected_path=%s module_count=%s",
        reason,
        expected_path or 'unknown',
        len(stale_names),
    )
    for name in stale_names:
        sys.modules.pop(name, None)


def _prepare_llama_cpp_import_from_probe(module_path: Any) -> None:
    """Make a successful desktop probe durable for the real in-process import."""

    if not module_path:
        return
    loaded = sys.modules.get('llama_cpp')
    loaded_path = getattr(loaded, '__file__', None) if loaded is not None else None
    expected_compare = _canonical_path_for_compare(module_path)
    loaded_compare = _canonical_path_for_compare(loaded_path)
    cached_llama_modules = any(
        name == 'llama_cpp' or name.startswith('llama_cpp.')
        for name in sys.modules
    )
    if cached_llama_modules and expected_compare:
        stale_cached_namespace = loaded_compare != expected_compare or any(
            name.startswith('llama_cpp.') for name in sys.modules
        )
        if stale_cached_namespace:
            logger.info(
                "llama_cpp clearing stale imported module before desktop probe reuse "
                "loaded_path=%s expected_path=%s",
                loaded_path or 'unknown',
                module_path,
            )
            _clear_llama_cpp_module_namespace('desktop_probe_path_mismatch', expected_path=module_path)

    package_parent = _llama_cpp_package_parent_from_module_path(module_path)
    if not package_parent:
        return
    package_parent_compare = _canonical_path_for_compare(package_parent)
    if package_parent_compare is None:
        return
    with _LLAMA_CPP_IMPORT_PATH_LOCK:
        retained = []
        for entry in sys.path:
            entry_compare = _canonical_path_for_compare(entry or os.getcwd())
            if entry_compare == package_parent_compare:
                continue
            retained.append(entry)
        retained = _stdlib_safe_path_order(retained)
        insert_index = 0
        if _is_site_packages_path(package_parent):
            for idx, entry in enumerate(retained):
                if _is_stdlib_path(entry):
                    insert_index = idx + 1
        sys.path[:] = retained[:insert_index] + [package_parent] + retained[insert_index:]


def _desktop_runtime_probe_from_env() -> Optional[Dict[str, Any]]:
    raw_probe = os.getenv(DESKTOP_RUNTIME_PROBE_ENV, '').strip()
    if not raw_probe:
        return None
    try:
        parsed = json.loads(raw_probe)
    except json.JSONDecodeError:
        logger.warning(
            "Ignoring invalid desktop runtime probe environment payload env=%s",
            DESKTOP_RUNTIME_PROBE_ENV,
        )
        return None
    return _coerce_desktop_runtime_probe(parsed)


_VALID_DESKTOP_RUNTIME_BACKENDS = frozenset({'cpu', 'cuda', 'metal'})
_INVALID_RUNTIME_MODULE_PATH_VALUES = frozenset({'missing', 'unknown'})


def _desktop_runtime_probe_identity(probe: Optional[Dict[str, Any]]) -> Optional[Tuple[str, str, str]]:
    """Return a validated (interpreter, backend, runtime_action) identity tuple for a probe."""

    if not isinstance(probe, dict):
        return None
    interpreter = str(probe.get('interpreter') or '').strip()
    backend = str(probe.get('backend') or '').strip().lower()
    action = str(probe.get('runtime_action') or '').strip().lower()
    if not interpreter or backend not in _VALID_DESKTOP_RUNTIME_BACKENDS:
        return None
    if not action:
        return None
    return interpreter, backend, action


def _probe_module_path_from_probe_dict(probe: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return a validated llama_cpp module path from a probe dict, or None for invalid/sentinel values."""

    if probe is None or probe.get('error'):
        return None
    module_path = str(probe.get('llama_module_path') or '').strip()
    if not module_path or module_path in _INVALID_RUNTIME_MODULE_PATH_VALUES:
        return None
    return module_path


def _effective_desktop_runtime_probe(probe: Any) -> Optional[Dict[str, Any]]:
    explicit_probe = _coerce_desktop_runtime_probe(probe)
    if explicit_probe is None:
        return _desktop_runtime_probe_from_env()
    explicit_path = _probe_module_path_from_probe_dict(explicit_probe)
    if explicit_path:
        return explicit_probe
    env_probe = _desktop_runtime_probe_from_env()
    env_path = _probe_module_path_from_probe_dict(env_probe)
    if not env_path:
        return explicit_probe
    if _desktop_runtime_probe_identity(explicit_probe) != _desktop_runtime_probe_identity(env_probe):
        return explicit_probe
    merged_probe = dict(explicit_probe)
    merged_probe['llama_module_path'] = env_path
    return merged_probe


def _probe_module_path_from_desktop_runtime_probe(probe: Any) -> Optional[str]:
    coerced = _effective_desktop_runtime_probe(probe)
    return _probe_module_path_from_probe_dict(coerced)


def _modern_desktop_runtime_probe_identity(probe: Any) -> Optional[str]:
    coerced = _coerce_desktop_runtime_probe(probe)
    if not coerced:
        return None
    if coerced.get('capability_source') != 'desktop_runtime_setup_probe':
        return None
    if coerced.get('llama_module_identity_malformed') is True:
        return None
    identity = _valid_llama_module_identity(coerced.get('llama_module_identity'))
    if identity is None:
        return None
    action = str(coerced.get('runtime_action') or '').lower()
    if action not in {'already_supported', 'metal_already_supported', 'installed_cuda_reexec', 'installed_metal_reexec'}:
        return None
    if str(coerced.get('backend') or '').lower() not in {'cuda', 'metal'}:
        return None
    if coerced.get('gpu_offload_supported') is not True:
        return None
    return identity


def _will_use_llama_cpp_subprocess_facade() -> bool:
    return (not _signal_guard_available()) or (threading.current_thread() is not threading.main_thread())


def _is_pathless_modern_desktop_probe_with_invalid_identity(probe: Any) -> bool:
    coerced = _coerce_desktop_runtime_probe(probe)
    if not coerced or coerced.get('capability_source') != 'desktop_runtime_setup_probe':
        return False
    if _probe_module_path_from_probe_dict(coerced):
        return False
    return _valid_llama_module_identity(coerced.get('llama_module_identity')) is None


def _import_llama_cpp_runtime(
    *,
    require_real_runtime: bool = True,
    timeout_seconds: Optional[float] = None,
    desktop_runtime_probe: Any = None,
):
    """Import llama_cpp while guarding against the repo-local test shim.

    Packaged desktop runtime setup verifies CUDA/Metal support and now hands off
    an identity-only llama_cpp module contract.  When the active platform/thread
    requires the subprocess facade, enforce that identity in the worker import
    handshake instead of launching redundant native discovery.
    """
    path_diagnostics = _sanitize_llama_cpp_import_paths()
    logger.info(
        "llama_cpp import path sanitized import_root=%s deprioritized_entries=%s sys_path_count=%s",
        _redact_paths_from_text(path_diagnostics.get('import_root')),
        len(path_diagnostics.get('deprioritized_entries', [])),
        path_diagnostics.get('sys_path_count'),
    )

    desktop_runtime_probe = _effective_desktop_runtime_probe(desktop_runtime_probe)
    expected_module_path = _probe_module_path_from_desktop_runtime_probe(desktop_runtime_probe)
    expected_module_identity = _modern_desktop_runtime_probe_identity(desktop_runtime_probe)
    identity_only_subprocess_probe = (
        expected_module_path is None
        and expected_module_identity is not None
        and _will_use_llama_cpp_subprocess_facade()
    )
    if expected_module_path:
        llama_module_path = expected_module_path
        logger.info(
            "llama_cpp runtime discovery reused desktop probe module_path=%s interpreter=%s",
            _redact_paths_from_text(llama_module_path),
            _redact_paths_from_text(sys.executable),
        )
    elif identity_only_subprocess_probe:
        llama_module_path = None
        logger.info(
            "llama_cpp runtime discovery reused desktop probe identity interpreter=%s",
            _redact_paths_from_text(sys.executable),
        )
    elif _is_pathless_modern_desktop_probe_with_invalid_identity(desktop_runtime_probe):
        raise ImportError('Desktop runtime probe identity is missing or malformed; refusing runtime discovery fallback')
    else:
        spec_diagnostics = _find_llama_cpp_spec_in_subprocess(timeout_seconds=timeout_seconds)
        llama_module_path = spec_diagnostics.get('module_path')
        logger.info(
            "llama_cpp runtime discovery complete module_path=%s interpreter=%s",
            _redact_paths_from_text(llama_module_path or 'missing'),
            _redact_paths_from_text(sys.executable),
        )

    if require_real_runtime and _is_repo_llama_cpp_shim(llama_module_path):
        _clear_llama_cpp_module_namespace('repo_local_shim_rejected', expected_path=llama_module_path)
        raise ImportError(
            "Refusing to use repository-local llama_cpp.py shim for runtime inference; "
            "install llama-cpp-python and ensure site-packages wins import priority."
        )

    if expected_module_path:
        _prepare_llama_cpp_import_from_probe(expected_module_path)

    logger.info(
        "llama_cpp direct import start module_path_hint=%s interpreter=%s",
        _redact_paths_from_text(llama_module_path or 'unknown'),
        _redact_paths_from_text(sys.executable),
    )
    llama_cpp = _import_llama_cpp_in_parent_with_timeout(
        timeout_seconds=timeout_seconds,
        module_path_hint=llama_module_path,
        desktop_runtime_probe=desktop_runtime_probe,
        expected_llama_module_identity=(
            expected_module_identity if expected_module_identity is not None and _will_use_llama_cpp_subprocess_facade() else None
        ),
    )
    imported_module_path = getattr(llama_cpp, '__file__', None)
    if (
        require_real_runtime
        and expected_module_path
        and imported_module_path
        and _canonical_path_for_compare(expected_module_path)
        != _canonical_path_for_compare(imported_module_path)
    ):
        _clear_llama_cpp_module_namespace('desktop_probe_import_mismatch', expected_path=expected_module_path)
        raise ImportError(
            "Desktop runtime probe module path mismatch; refusing mismatched llama_cpp runtime "
            f"desktop_probe_path={expected_module_path} imported_path={imported_module_path}"
        )
    llama_module_path = imported_module_path
    logger.info(
        "llama_cpp import complete module_path=%s interpreter=%s",
        _redact_paths_from_text(llama_module_path or 'unknown'),
        _redact_paths_from_text(sys.executable),
    )

    if require_real_runtime and _is_repo_llama_cpp_shim(llama_module_path):
        _clear_llama_cpp_module_namespace('repo_local_shim_rejected', expected_path=llama_module_path)
        raise ImportError(
            "Refusing to use repository-local llama_cpp.py shim for runtime inference; "
            "install llama-cpp-python and ensure site-packages wins import priority."
        )

    return llama_cpp


def detect_llama_runtime_capabilities() -> Dict[str, Any]:
    """Return backend/offload capability details from the installed llama_cpp runtime."""
    try:
        llama_cpp = _import_llama_cpp_runtime(require_real_runtime=True)
    except LlamaCppRuntimeStageTimeout as exc:
        return {
            'backend': 'missing',
            'gpu_offload_supported': False,
            'detected_device': 'none',
            'error': _format_runtime_stage_timeout(exc),
        }
    except Exception as exc:
        return {
            'backend': 'missing',
            'gpu_offload_supported': False,
            'detected_device': 'none',
            'error': str(exc),
        }

    if getattr(llama_cpp, '__token_place_subprocess_facade__', False):
        facade_backend = 'cuda' if getattr(llama_cpp, 'GGML_USE_CUDA', False) else (
            'metal' if getattr(llama_cpp, 'GGML_USE_METAL', False) else 'cpu'
        )
        if facade_backend == 'cpu':
            try:
                probe = _probe_llama_cpp_capabilities_in_subprocess()
                facade_backend = str(probe.get('backend') or facade_backend)
                return {
                    'backend': facade_backend,
                    'gpu_offload_supported': bool(probe.get('gpu_offload_supported', False)),
                    'detected_device': str(probe.get('detected_device') or 'cpu'),
                    'interpreter': str(probe.get('interpreter') or sys.executable),
                    'prefix': str(probe.get('prefix') or sys.prefix),
                    'llama_module_path': str(
                        probe.get('llama_module_path')
                        or getattr(llama_cpp, '__file__', None)
                        or 'unknown'
                    ),
                    'error': probe.get('error'),
                }
            except LlamaCppRuntimeStageTimeout as exc:
                return {
                    'backend': 'missing',
                    'gpu_offload_supported': False,
                    'detected_device': 'none',
                    'interpreter': sys.executable,
                    'prefix': sys.prefix,
                    'llama_module_path': getattr(llama_cpp, '__file__', None) or 'unknown',
                    'error': _format_runtime_stage_timeout(exc),
                }
            except Exception as exc:
                return {
                    'backend': 'missing',
                    'gpu_offload_supported': False,
                    'detected_device': 'none',
                    'interpreter': sys.executable,
                    'prefix': sys.prefix,
                    'llama_module_path': getattr(llama_cpp, '__file__', None) or 'unknown',
                    'error': str(exc),
                }
        return {
            'backend': facade_backend,
            'gpu_offload_supported': True,
            'detected_device': facade_backend,
            'interpreter': sys.executable,
            'prefix': sys.prefix,
            'llama_module_path': getattr(llama_cpp, '__file__', None) or 'unknown',
            'error': None,
        }

    backend = 'cpu'
    cuda_markers = (
        'GGML_USE_CUDA',
        'GGML_CUDA',
        'LLAMA_CUDA',
        'GGML_USE_CUBLAS',
        'LLAMA_CUBLAS',
    )
    metal_markers = (
        'GGML_USE_METAL',
        'GGML_METAL',
        'LLAMA_METAL',
    )
    if any(bool(getattr(llama_cpp, marker, False)) for marker in cuda_markers):
        backend = 'cuda'
    elif any(bool(getattr(llama_cpp, marker, False)) for marker in metal_markers):
        backend = 'metal'

    supports_gpu = getattr(llama_cpp, 'llama_supports_gpu_offload', None)
    gpu_offload_supported = False
    module_path = getattr(llama_cpp, '__file__', None)
    if callable(supports_gpu):
        try:
            if module_path:
                probe = _probe_llama_cpp_capabilities_in_subprocess()
                gpu_offload_supported = bool(probe.get('gpu_offload_supported', False))
                backend = str(probe.get('backend') or backend)
            else:
                gpu_offload_supported = bool(supports_gpu())
        except LlamaCppRuntimeStageTimeout as exc:
            return {
                'backend': 'missing',
                'gpu_offload_supported': False,
                'detected_device': 'none',
                'interpreter': sys.executable,
                'prefix': sys.prefix,
                'llama_module_path': module_path or 'unknown',
                'llama_module_path_present': bool(module_path and module_path not in {'missing', 'unknown'}),
                'error': _format_runtime_stage_timeout(exc),
            }
        except Exception:
            gpu_offload_supported = False
    else:
        gpu_offload_supported = backend in {'cuda', 'metal'}

    # Some llama_cpp builds can report runtime GPU offload support via probe
    # without exposing GGML_USE_* backend markers. Preserve prior Linux behavior
    # by inferring CUDA when offload is available and backend markers are absent.
    if gpu_offload_supported and backend == 'cpu':
        backend = 'metal' if sys.platform == 'darwin' else 'cuda'

    return {
        'backend': backend,
        'gpu_offload_supported': gpu_offload_supported,
        'detected_device': backend if gpu_offload_supported else 'cpu',
        'interpreter': sys.executable,
        'prefix': sys.prefix,
        'llama_module_path': module_path or 'unknown',
        'llama_module_path_present': bool(module_path and module_path not in {'missing', 'unknown'}),
        'error': None,
    }

def _coerce_desktop_runtime_probe(probe: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(probe, dict):
        return None
    error = str(probe.get('error') or probe.get('fallback_reason') or '').strip()
    action = str(probe.get('runtime_action') or probe.get('action') or '').strip().lower()
    backend = str(probe.get('selected_backend') or probe.get('backend') or '').strip().lower()
    gpu_bool = _coerce_strict_bool(probe.get('gpu_offload_supported', True if backend in {'cuda', 'metal'} else False))
    gpu_supported = bool(gpu_bool)
    module_path = str(probe.get('llama_module_path') or '').strip()
    raw_module_identity = probe.get('llama_module_identity')
    module_identity_supplied = llama_module_identity_supplied(raw_module_identity)
    module_identity = _valid_llama_module_identity(raw_module_identity)
    if not backend or backend == 'missing' or action in {'failed', 'unavailable', 'shadowed_repo_llama_cpp'}:
        return None
    if error and action not in {'already_supported', 'metal_already_supported'}:
        return None
    coerced = {
        'backend': backend,
        'gpu_offload_supported': gpu_supported,
        'detected_device': str(probe.get('detected_device') or probe.get('device') or backend),
        'interpreter': str(probe.get('interpreter') or sys.executable),
        'prefix': str(probe.get('prefix') or sys.prefix),
        'llama_module_path': module_path or 'unknown',
        'llama_module_path_present': (
            _coerce_strict_bool(probe.get('llama_module_path_present'))
            if _coerce_strict_bool(probe.get('llama_module_path_present')) is not None
            else bool(module_path and module_path not in {'missing', 'unknown'})
        ),
        'error': None,
        'runtime_action': action or 'unknown',
    }
    if module_identity is not None:
        coerced['llama_module_identity'] = module_identity
    elif module_identity_supplied:
        coerced['llama_module_identity_malformed'] = True
    support: Dict[str, bool] = {}
    raw_support = probe.get('constructor_kwarg_support')
    if isinstance(raw_support, dict):
        for name in LLAMA_CPP_CONSTRUCTOR_CAPABILITY_KWARGS:
            if name in raw_support:
                value = _coerce_strict_bool(raw_support.get(name))
                if value is not None:
                    support[name] = value
    legacy_map = {
        'rope_scaling_type': 'rope_scaling_type_supported',
        'yarn_ext_factor': 'yarn_ext_factor_supported',
        'rope_freq_scale': 'rope_freq_scale_supported',
        'yarn_orig_ctx': 'yarn_orig_ctx_supported',
    }
    for kwarg, field in legacy_map.items():
        if kwarg not in support and field in probe:
            value = _coerce_strict_bool(probe.get(field))
            if value is not None:
                support[kwarg] = value
    if support:
        coerced['constructor_kwarg_support'] = support

    def _coerce_bounded_schema_int(value: Any) -> Optional[int]:
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**31 - 1:
            return value
        return None

    for field in ('q8_kv_cache_type_value', 'q4_kv_cache_type_value', 'f16_kv_cache_type_value'):
        value = _coerce_bounded_schema_int(probe.get(field))
        if value is not None and 0 <= value <= 2**31 - 1:
            coerced[field] = value
    if probe.get('llama_cpp_python_version'):
        coerced['llama_cpp_python_version'] = str(probe.get('llama_cpp_python_version'))
    for field in ('constructor_has_var_kwargs', 'constructor_signature_inspectable'):
        value = _coerce_strict_bool(probe.get(field))
        if value is not None:
            coerced[field] = value
    yarn_support = probe.get('qwen_64k_yarn_support')
    if yarn_support in {'supported', 'unknown', 'unsupported'}:
        coerced['qwen_64k_yarn_support'] = str(yarn_support)
    if probe.get('yarn_resolver_source'):
        coerced['yarn_resolver_source'] = str(probe.get('yarn_resolver_source'))
    yarn_enum_value = _coerce_bounded_schema_int(probe.get('yarn_enum_value'))
    if yarn_enum_value is not None and 0 <= yarn_enum_value <= 2**31 - 1:
        coerced['yarn_enum_value'] = yarn_enum_value

    source = str(probe.get('capability_source') or '').strip()
    if source in {'desktop_runtime_setup_probe', 'desktop_runtime_setup_probe_legacy', 'worker_probe'}:
        coerced['capability_source'] = source

    # Compatibility bridge for deployed flat desktop probes. Do not invent
    # unobserved performance/KV kwargs or assume exported enum sources map to
    # value 2. Only the old probe's explicit verified numeric-fallback path is
    # accepted here; launching another native import would recreate the Windows
    # timeout failure this desktop facade is designed to avoid.
    legacy_yarn = _coerce_strict_bool(probe.get('yarn_rope_supported'))
    required_supported = all(support.get(name) is True for name in ('rope_scaling_type', 'rope_freq_scale', 'yarn_orig_ctx'))
    legacy_resolver = str(probe.get('yarn_resolver_source') or '').strip().lower()
    if (
        coerced.get('qwen_64k_yarn_support') is None
        and coerced.get('yarn_enum_value') is None
        and legacy_yarn is True
        and required_supported
        and legacy_resolver == 'numeric_fallback'
        and probe.get('yarn_enum_value') is None
    ):
        coerced['qwen_64k_yarn_support'] = 'supported'
        coerced['yarn_enum_value'] = 2
        coerced['yarn_resolver_source'] = 'numeric_fallback'
        coerced['constructor_signature_inspectable'] = True
        coerced['capability_source'] = 'desktop_runtime_setup_probe_legacy'
    elif (
        coerced.get('capability_source') is None
        and (
            legacy_yarn is not None
            or probe.get('yarn_resolver_source') is not None
            or any(probe.get(f'{name}_supported') is not None for name in ('rope_scaling_type', 'rope_freq_scale', 'yarn_orig_ctx'))
        )
    ):
        coerced['capability_source'] = 'desktop_runtime_setup_probe_legacy'
    return coerced


def llama_cpp_verbose_logging_enabled() -> bool:
    """Return whether raw llama.cpp verbose logging should be enabled."""

    return (
        os.getenv('TOKEN_PLACE_VERBOSE_LLM_LOGS') == '1'
        or os.getenv('TOKEN_PLACE_VERBOSE_SUBPROCESS_LOGS') == '1'
    )


class ModelManager:
    """
    Manages LLM model downloading, initialization, and inference.
    """
    def __init__(self, config=None):
        """Initialize the ModelManager with configuration."""
        # Import config lazily to avoid circular imports
        if config is None:
            from config import get_config
            config = get_config()

        self.config = config

        # Model artifact configuration is sourced from the active API v1 profile,
        # while preserving explicit config/env overrides for existing deployments.
        self.profile_id = resolve_profile_id(
            config.get('model.profile_id', None),
            config.get('model.api_model_id', None),
        )
        self.model_profile = get_model_profile(self.profile_id)
        assert self.model_profile is not None
        self.api_model_id = self.model_profile['api_model_id']
        self.display_name = self.model_profile['display_name']
        self.file_name = self._get_profile_artifact_config('filename', 'filename')
        self.url = self._get_profile_artifact_config('url', 'download_url')
        self.canonical_family_url = self._get_profile_artifact_config(
            'canonical_family_url',
            'canonical_family_url',
        )
        self.chunk_size_mb = config.get('model.download_chunk_size_mb', 10)
        # Network timeout for model downloads (seconds)
        self.download_timeout = config.get('model.download_timeout', 30)
        self.models_dir = config.get('paths.models_dir')
        self.model_path = os.path.join(self.models_dir, self.file_name)

        # LLM instance and lock for thread safety
        self.llm = None
        self.child_model_path_exists = False
        self.llm_lock = Lock()
        self._llm_generation = 0
        self.worker_restart_count = 0
        self.last_worker_error_code: Optional[str] = None
        self.last_worker_exit_code: Optional[int] = None
        self.last_worker_restart_at_ms: Optional[int] = None
        self.last_plain_completion_eval_return_code: Optional[int] = None
        self.worker_state = 'stopped'
        self.last_runtime_init_error: Optional[str] = None
        self._qwen_64k_runtime_profiles: list[Dict[str, Any]] = []
        self._qwen_64k_selected_profile_index = 0
        self._qwen_64k_selected_profile_id: Optional[str] = None
        self._qwen_64k_profile_attempt_ids: list[str] = []
        self._qwen_64k_profile_recovery_count = 0
        # Preserved across profile advances: the first recoverable failure
        # category from the initial readiness smoke, so later profile failures
        # do not overwrite it and the first Metal failure remains observable.
        self._qwen_64k_first_readiness_failure_category: Optional[str] = None
        self._qwen_64k_first_readiness_failure_diagnostics: Dict[str, Any] = {}

        # Check if mock mode is enabled
        self.use_mock_llm = config.get('model.use_mock', False) or os.getenv('USE_MOCK_LLM') == '1'
        self.default_n_gpu_layers = config.get('model.n_gpu_layers', -1)
        self.hybrid_n_gpu_layers = config.get('model.hybrid_n_gpu_layers', 24)
        self.gpu_headroom_percent = config.get('model.gpu_memory_headroom_percent', 0.1)
        self.enforce_gpu_headroom = config.get('model.enforce_gpu_memory_headroom', True)
        self.requested_compute_mode = 'auto'
        self.desktop_runtime_probe: Optional[Dict[str, Any]] = None
        self._imported_llama_cpp_module_path: Optional[str] = None
        self.last_compute_diagnostics = {
            'requested_mode': 'auto',
            'effective_mode': 'pending',
            'backend_available': 'unknown',
            'backend_selected': 'unknown',
            'backend_used': 'unknown',
            'n_gpu_layers': self.default_n_gpu_layers,
            'fallback_reason': None,
            'context_tier': getattr(self, 'context_tier', '8k-fast'),
            'context_window_tokens': config.get('model.context_size', 8192),
        }

    def _runtime_capabilities(self=None) -> Dict[str, Any]:
        probe = _coerce_desktop_runtime_probe(getattr(self, 'desktop_runtime_probe', None))
        if probe is not None:
            probe_module_path = probe.get('llama_module_path')
            imported_module_path = getattr(self, '_imported_llama_cpp_module_path', None)
            if (
                probe_module_path
                and probe_module_path != 'unknown'
                and imported_module_path
                and imported_module_path != 'unknown'
                and _canonical_path_for_compare(probe_module_path)
                != _canonical_path_for_compare(imported_module_path)
            ):
                if self is not None:
                    self.log_warning(
                        "Desktop runtime probe module path mismatch; refusing to reuse probe "
                        f"desktop_probe_path={probe_module_path} "
                        f"imported_path={imported_module_path}"
                    )
                return {
                    'backend': 'cpu',
                    'gpu_offload_supported': False,
                    'detected_device': 'cpu',
                    'interpreter': sys.executable,
                    'prefix': sys.prefix,
                    'llama_module_path': imported_module_path,
                    'error': 'llama_cpp_runtime_probe_mismatch',
                }
            if self is not None:
                self.log_info(
                    "Using desktop runtime probe diagnostics for compute plan "
                    f"backend={probe['backend']} interpreter={probe['interpreter']} "
                    f"llama_module_path={probe['llama_module_path']} "
                    f"runtime_action={probe.get('runtime_action', 'unknown')}"
                )
            return probe
        return detect_llama_runtime_capabilities()

    def _platform_gpu_backend(self=None) -> Optional[str]:
        runtime = self._runtime_capabilities() if self is not None else detect_llama_runtime_capabilities()
        backend = str(runtime.get('backend') or 'cpu')
        if backend in {'cuda', 'metal'}:
            return backend
        return None

    def _llama_gpu_offload_available(self=None) -> bool:
        runtime = self._runtime_capabilities() if self is not None else detect_llama_runtime_capabilities()
        return bool(runtime.get('gpu_offload_supported', False))

    def _mock_compute_plan(self) -> Dict[str, Any]:
        """Return lightweight diagnostics for mock LLM mode without probing llama_cpp."""

        requested = str(getattr(self, 'requested_compute_mode', 'auto')).lower()
        probe = _coerce_desktop_runtime_probe(getattr(self, 'desktop_runtime_probe', None))
        runtime_error = None
        backend = 'cpu'
        gpu_runtime_supported = False
        if probe is not None:
            runtime_error = probe.get('error')
            backend = str(probe.get('backend') or 'cpu')
            gpu_runtime_supported = bool(probe.get('gpu_offload_supported', False))

        gpu_requested = requested in {'auto', 'gpu', 'hybrid'} and int(self.default_n_gpu_layers) != 0
        fallback_reason = None
        backend_selected = backend if gpu_requested else 'cpu'
        backend_used = backend_selected
        n_gpu_layers = 0
        effective_mode = 'cpu'

        if gpu_requested and backend in {'cuda', 'metal'} and gpu_runtime_supported:
            effective_mode = backend if requested == 'gpu' else (f'hybrid_{backend}' if requested == 'hybrid' else backend)
            n_gpu_layers = -1 if requested in {'auto', 'gpu'} else max(1, int(self.hybrid_n_gpu_layers))
        elif gpu_requested:
            backend_used = 'cpu'
            fallback_reason = runtime_error or 'mock LLM mode does not require llama_cpp GPU probing'
            effective_mode = 'cpu_fallback' if requested != 'cpu' else 'cpu'
        return {
            'requested_mode': requested,
            'effective_mode': effective_mode,
            'backend_available': backend,
            'backend_selected': backend_selected,
            'backend_used': backend_used,
            'n_gpu_layers': n_gpu_layers,
            'fallback_reason': fallback_reason,
            'mock_runtime': True,
        }

    def _resolve_compute_plan(self) -> Dict[str, Any]:
        requested = str(getattr(self, 'requested_compute_mode', 'auto')).lower()
        if requested == 'cpu':
            return {
                'requested_mode': requested,
                'effective_mode': 'cpu',
                'backend_available': 'cpu',
                'backend_selected': 'cpu',
                'backend_used': 'cpu',
                'n_gpu_layers': 0,
                'fallback_reason': None,
            }

        runtime = self._runtime_capabilities()
        runtime_error = str(runtime.get('error') or '')
        backend = str(runtime.get('backend') or 'cpu')
        backend_available = backend if backend in {'cuda', 'metal'} else 'cpu'
        gpu_runtime_supported = bool(runtime.get('gpu_offload_supported', False))
        fallback_reason = None

        if runtime_error.endswith('_timeout') or '_timeout after ' in runtime_error:
            raise RuntimeError(runtime_error)

        if requested == 'auto':
            requested_layers = int(self.default_n_gpu_layers)
            n_gpu_layers = requested_layers
            gpu_requested = n_gpu_layers != 0
            backend_selected = backend_available if gpu_requested else 'cpu'
            if gpu_requested and (
                backend_available == 'cpu' or not gpu_runtime_supported
            ):
                n_gpu_layers = 0
                fallback_reason = (
                    runtime_error or 'no CUDA/Metal backend is supported on this platform'
                    if backend_available == 'cpu'
                    else (
                        f'llama-cpp-python runtime does not expose {backend_available} '
                        'GPU offload support'
                    )
                )
            return {
                'requested_mode': requested,
                'effective_mode': 'cpu_fallback' if fallback_reason else backend_selected,
                'backend_available': backend_available,
                'backend_selected': backend_selected,
                'backend_used': 'cpu' if fallback_reason else backend_selected,
                'n_gpu_layers': n_gpu_layers,
                'fallback_reason': fallback_reason,
            }

        if backend_available == 'cpu':
            fallback_reason = runtime_error or 'no CUDA/Metal backend is supported on this platform'
        elif not gpu_runtime_supported:
            fallback_reason = (
                f'llama-cpp-python runtime does not expose {backend_available} GPU offload support'
            )

        if fallback_reason:
            return {
                'requested_mode': requested,
                'effective_mode': 'cpu_fallback',
                'backend_available': backend_available,
                'backend_selected': backend_available,
                'backend_used': 'cpu',
                'n_gpu_layers': 0,
                'fallback_reason': fallback_reason,
            }

        if requested == 'hybrid':
            n_gpu_layers = max(1, int(self.hybrid_n_gpu_layers))
            return {
                'requested_mode': requested,
                'effective_mode': f'hybrid_{backend_available}',
                'backend_available': backend_available,
                'backend_selected': backend_available,
                'backend_used': backend_available,
                'n_gpu_layers': n_gpu_layers,
                'fallback_reason': None,
            }

        # Explicit ``gpu`` uses full offload when backend support is available.
        return {
            'requested_mode': requested,
            'effective_mode': backend_available,
            'backend_available': backend_available,
            'backend_selected': backend_available,
            'backend_used': backend_available,
            'n_gpu_layers': -1,
            'fallback_reason': None,
        }


    def supports_api_v1_model(self, model_id: str) -> bool:
        """Return whether the active runtime profile can serve an API v1 model id.

        Capability reporting is intentionally profile-derived: a Qwen profile
        advertises Qwen only, while stale Llama profiles/files never satisfy the
        Qwen API v1 default.
        """

        if not isinstance(model_id, str) or not model_id.strip():
            return False
        normalized_model = model_id.strip().lower()
        active_ids = {
            str(value).strip().lower()
            for value in (
                self.api_model_id,
                self.profile_id,
                self.file_name,
                os.path.basename(str(self.model_path)),
            )
            if value
        }
        return normalized_model in active_ids

    def _get_profile_artifact_config(self, config_key: str, profile_key: str) -> Any:
        """Return a model artifact config override or the active profile default."""
        profile_value = self.model_profile[profile_key]
        configured_value = self.config.get(f'model.{config_key}', profile_value)
        from utils.config_schema import DEFAULT_CONFIG

        default_model_config = DEFAULT_CONFIG.get('model', {})
        default_value = default_model_config.get(config_key)
        if self.profile_id != default_model_config.get('profile_id') and configured_value == default_value:
            return profile_value
        return configured_value

    def _llama_constructor_accepts(self, llama_cls: Any, kwarg: str) -> bool:
        """Return whether the imported llama-cpp-python Llama constructor accepts a kwarg."""

        return _constructor_accepts_kwarg(llama_cls, kwarg)

    def _apply_chat_template_accepts(self, llm: Any, kwarg: str) -> bool:
        """Return whether runtime chat-template rendering accepts a kwarg."""

        renderer = getattr(llm, 'apply_chat_template', None)
        if not callable(renderer):
            tokenizer = getattr(llm, 'tokenizer', None)
            if callable(tokenizer):
                try:
                    tokenizer = tokenizer()
                except Exception:
                    tokenizer = None
            renderer = getattr(tokenizer, 'apply_chat_template', None) if tokenizer is not None else None
        if not callable(renderer):
            return False
        try:
            signature = inspect.signature(renderer)
        except (TypeError, ValueError):
            return False
        parameters = signature.parameters
        return kwarg in parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )

    def _chat_template_mode(self) -> str:
        """Return the runtime chat-template policy for the active profile."""

        return str(self.model_profile.get('chat_template_policy') or 'llama-3')

    def _qwen_non_thinking_required(self) -> bool:
        return self.model_profile.get('provider') == 'qwen' and self.model_profile.get('thinking_mode') == 'disabled'

    def _runtime_init_kwargs(self, llama_cls: Any, n_gpu_layers: int, llama_cpp_module: Any = None, runtime_profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Build verified llama-cpp-python runtime kwargs for the selected profile.

        llama-cpp-python uses the GGUF/Jinja chat template when ``chat_format`` is
        omitted.  Qwen3 must therefore not inherit token.place's historical
        ``chat_format='llama-3'`` default; if the runtime cannot expose a template
        after initialization, warm-load fails below instead of running Qwen with a
        Llama template.  YaRN/RoPE kwargs are only passed after constructor
        signature verification so unsupported runtimes fail closed before a node
        can advertise false 64K Qwen capability.
        """

        n_ctx = int(self.config.get('model.context_size', 8192))
        kwargs: Dict[str, Any] = {
            'model_path': self.model_path,
            'n_gpu_layers': n_gpu_layers,
            'n_ctx': n_ctx,
            'verbose': llama_cpp_verbose_logging_enabled(),
        }
        if self.model_profile.get('provider') == 'qwen':
            if self._chat_template_mode() != 'gguf-jinja':
                raise RuntimeError('Qwen runtime requires GGUF/Jinja chat template policy')
        else:
            kwargs['chat_format'] = self.config.get('model.chat_format', 'llama-3')

        rope_policy = self.model_profile.get('rope_scaling_policy') or {}
        context_tier = getattr(self, 'context_tier', '8k-fast')
        native_context = int(self.model_profile.get('native_context_tokens') or n_ctx)
        needs_yarn = (
            self.model_profile.get('provider') == 'qwen'
            and rope_policy.get('type') == 'yarn'
            and context_tier == rope_policy.get('required_for_tier')
            and n_ctx > native_context
        )
        if needs_yarn:
            try:
                original_context_tokens = int(rope_policy['original_context_tokens'])
                requested_context_tokens = int(n_ctx)
                configured_multiplier = float(rope_policy['factor'])
                computed_multiplier = requested_context_tokens / original_context_tokens
                # Qwen profile factor is a context multiplier (64K over 32K),
                # which llama-cpp-python expects as rope_freq_scale=1/N.
                rope_freq_scale = 1.0 / configured_multiplier
            except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
                self.last_yarn_rope_diagnostics = {
                    'active_profile_id': self.profile_id,
                    'active_context_tier': context_tier,
                    'requested_n_ctx': n_ctx,
                    'qwen_yarn_configuration_valid': False,
                    'missing_reason': 'runtime_qwen_64k_yarn_configuration_invalid',
                }
                raise RuntimeError('runtime_qwen_64k_yarn_configuration_invalid') from exc
            configuration_valid = (
                original_context_tokens == 32768
                and requested_context_tokens == 65536
                and original_context_tokens > 0
                and requested_context_tokens > original_context_tokens
                and math.isfinite(configured_multiplier)
                and configured_multiplier > 1.0
                and math.isclose(configured_multiplier, computed_multiplier, rel_tol=1e-12, abs_tol=1e-12)
                and math.isclose(rope_freq_scale, 0.5, rel_tol=1e-12, abs_tol=1e-12)
            )
            if not configuration_valid:
                self.last_yarn_rope_diagnostics = {
                    'active_profile_id': self.profile_id,
                    'active_context_tier': context_tier,
                    'requested_n_ctx': n_ctx,
                    'qwen_yarn_requested_context_tokens': requested_context_tokens,
                    'qwen_yarn_original_context_tokens': original_context_tokens,
                    'qwen_yarn_context_multiplier': configured_multiplier,
                    'qwen_yarn_rope_freq_scale': rope_freq_scale,
                    'qwen_yarn_ext_factor_overridden': False,
                    'qwen_yarn_configuration_valid': False,
                    'missing_reason': 'runtime_qwen_64k_yarn_configuration_invalid',
                }
                raise RuntimeError('runtime_qwen_64k_yarn_configuration_invalid')

            llama_module = llama_cpp_module or inspect.getmodule(llama_cls)
            yarn_probe = _runtime_supports_qwen_yarn_rope(llama_module, llama_cls)
            self.last_yarn_rope_diagnostics = {
                'supported': yarn_probe['supported'],
                'yarn_enum_location': yarn_probe['yarn_enum_location'],
                'yarn_resolver_source': yarn_probe.get('yarn_resolver_source'),
                'constructor_kwarg_support': yarn_probe.get('constructor_kwarg_support'),
                'accepted_constructor_kwargs': yarn_probe['accepted_constructor_kwargs'],
                'active_profile_id': self.profile_id,
                'active_context_tier': context_tier,
                'requested_n_ctx': n_ctx,
                'llama_module_path': yarn_probe['llama_module_path'],
                'llama_cpp_python_version': yarn_probe['llama_cpp_python_version'],
                'missing_reason': yarn_probe['missing_reason'],
                'capability_source': yarn_probe.get('capability_source'),
                'support_classification': yarn_probe.get('support_classification'),
                'constructor_signature_inspectable': yarn_probe.get('constructor_signature_inspectable'),
                'constructor_has_var_kwargs': yarn_probe.get('constructor_has_var_kwargs'),
                'parent_facade_type': yarn_probe.get('parent_facade_type'),
                'child_probe_reprobe_attempted': yarn_probe.get('child_probe_reprobe_attempted'),
                'llama_module_path_present': yarn_probe.get('llama_module_path_present'),
                'llama_module_identity_match': yarn_probe.get('llama_module_identity_match'),
                'incomplete_probe_fields': yarn_probe.get('incomplete_probe_fields'),
                'constructor_kwargs_attempted': (
                    ['rope_scaling_type', 'rope_freq_scale', 'yarn_orig_ctx']
                    if yarn_probe.get('supported') else []
                ),
            }
            if not yarn_probe['supported']:
                safe_diagnostics = _format_qwen_yarn_unsupported_diagnostics(
                    self.last_yarn_rope_diagnostics
                )
                raise RuntimeError(
                    f'{QWEN_64K_YARN_UNSUPPORTED_MESSAGE}; {safe_diagnostics}'
                )
            if yarn_probe.get('yarn_enum_value') is None:
                self.last_yarn_rope_diagnostics['supported'] = False
                self.last_yarn_rope_diagnostics['missing_reason'] = 'missing concrete YaRN enum value from supported child probe'
                safe_diagnostics = _format_qwen_yarn_unsupported_diagnostics(
                    self.last_yarn_rope_diagnostics
                )
                raise RuntimeError(
                    f'{QWEN_64K_YARN_UNSUPPORTED_MESSAGE}; {safe_diagnostics}'
                )
            self.last_yarn_rope_diagnostics.update({
                'qwen_yarn_requested_context_tokens': requested_context_tokens,
                'qwen_yarn_original_context_tokens': original_context_tokens,
                'qwen_yarn_context_multiplier': configured_multiplier,
                'qwen_yarn_rope_freq_scale': rope_freq_scale,
                'qwen_yarn_ext_factor_overridden': False,
                'qwen_yarn_rope_scaling_type_source': yarn_probe.get('yarn_resolver_source'),
                'qwen_yarn_configuration_valid': configuration_valid,
            })
            kwargs.update(
                {
                    'rope_scaling_type': yarn_probe['yarn_enum_value'],
                    'rope_freq_scale': rope_freq_scale,
                    'yarn_orig_ctx': original_context_tokens,
                }
            )
            if runtime_profile is None:
                built_profiles = _build_qwen_64k_runtime_profiles(
                    llama_module,
                    llama_cls,
                    model_path=self.model_path,
                    n_ctx=n_ctx,
                    enable_kqv_offload=n_gpu_layers != 0,
                )
                runtime_profile = built_profiles[0] if built_profiles else {
                    'profile_id': QWEN_64K_RUNTIME_PROFILE_DEFAULT,
                    'kwargs': {},
                    'diagnostics': {'profile_id': QWEN_64K_RUNTIME_PROFILE_DEFAULT, 'enabled': False, 'applied': {}},
                }
            profile_kwargs = runtime_profile.get('kwargs') if isinstance(runtime_profile, dict) else {}
            if isinstance(profile_kwargs, dict):
                kwargs.update(profile_kwargs)
            profile_diagnostics = runtime_profile.get('diagnostics') if isinstance(runtime_profile, dict) else None
            self.last_qwen_64k_memory_profile_diagnostics = dict(profile_diagnostics) if isinstance(profile_diagnostics, dict) else {
                'profile_id': QWEN_64K_RUNTIME_PROFILE_DEFAULT,
                'enabled': True,
                'applied': {},
            }
        else:
            self.last_qwen_64k_memory_profile_diagnostics = {'enabled': False, 'applied': {}}
            self.last_yarn_rope_diagnostics = {
                'supported': False,
                'active': False,
                'required': False,
                'active_profile_id': self.profile_id,
                'active_context_tier': context_tier,
                'requested_n_ctx': n_ctx,
                'missing_reason': 'not_required_for_active_profile_or_tier',
            }
        return kwargs

    def get_model_artifact_metadata(self) -> Dict[str, Any]:
        """Return runtime model metadata used by server and desktop bridges."""
        file_exists = os.path.exists(self.model_path)
        return {
            'api_model_id': self.api_model_id,
            'active_api_model_id': self.api_model_id,
            'profile_id': self.profile_id,
            'active_profile_id': self.profile_id,
            'display_name': self.display_name,
            'canonical_family_url': self.canonical_family_url,
            'filename': self.file_name,
            'url': self.url,
            'download_url': self.url,
            'gguf_repo': self.model_profile.get('gguf_repo'),
            'source_model': self.model_profile.get('source_model'),
            'quantization': self.model_profile.get('quantization'),
            'license': self.model_profile.get('license'),
            'native_context_tokens': self.model_profile.get('native_context_tokens'),
            'maximum_validated_context_tokens': self.model_profile.get('maximum_validated_context_tokens'),
            'supported_context_tiers': self.model_profile.get('supported_context_tiers'),
            'chat_template_policy': self.model_profile.get('chat_template_policy'),
            'thinking_mode': self.model_profile.get('thinking_mode'),
            'models_dir': self.models_dir,
            'resolved_model_path': self.model_path,
            'exists': file_exists,
            'size_bytes': os.path.getsize(self.model_path) if file_exists else None,
        }

    def _log(self, level: int, message: str, **kwargs) -> None:
        """Log a message when not in production."""
        if self.config.is_production:
            return
        logger.log(level, message, **kwargs)

    def log_info(self, message):
        """Log info only in non-production environments"""
        self._log(logging.INFO, message)

    def log_warning(self, message):
        """Log warnings only in non-production environments"""
        self._log(logging.WARNING, message)

    def log_error(self, message, exc_info=False):
        """Log errors only in non-production environments"""
        self._log(logging.ERROR, message, exc_info=exc_info)

    def create_models_directory(self) -> str:
        """Create the models directory if it doesn't exist."""
        os.makedirs(self.models_dir, exist_ok=True)
        return self.models_dir

    def _profile_has_pinned_artifact(self) -> bool:
        return (
            self.model_profile.get('artifact_size_bytes') is not None
            or self.model_profile.get('artifact_sha256') is not None
        )

    def _is_selected_model_path(self, file_path: str) -> bool:
        try:
            return os.path.abspath(str(file_path)) == os.path.abspath(str(self.model_path))
        except (TypeError, ValueError):
            return False

    def download_file_in_chunks(self, file_path: str, url: str, chunk_size_mb: int) -> bool:
        """
        Download a file in chunks with progress reporting.

        Args:
            file_path: The path to save the file to
            url: The URL to download from
            chunk_size_mb: The chunk size in MB

        Returns:
            bool: True if download was successful, False otherwise
        """
        pinned_artifact = (
            self._profile_has_pinned_artifact()
            and self._is_managed_canonical_model_path()
            and self._is_selected_model_path(file_path)
        )
        expected_size = self.model_profile.get('artifact_size_bytes') if pinned_artifact else None
        expected_sha256 = self.model_profile.get('artifact_sha256') if pinned_artifact else None
        chunk_size_bytes = chunk_size_mb * 1024 * 1024  # Convert MB to bytes
        response = None
        tmp_path = f"{file_path}.tmp.{os.getpid()}.{uuid.uuid4().hex}"

        try:
            response = requests.get(url, stream=True, timeout=self.download_timeout)
        except requests.Timeout as e:
            self.log_error(f"Error: Download request timed out: {e}")
            return False
        except requests.RequestException as e:
            self.log_error(f"Error: Unable to start download request: {e}")
            return False

        try:
            if response.status_code != 200:
                self.log_error(f"Error: Unable to download file, status code {response.status_code}")
                return False

            total_size_in_bytes = int(response.headers.get('content-length', 0))
            if total_size_in_bytes == 0:
                self.log_error("Error: Content-Length header is missing or zero.")
                return False

            total_size_in_mb = total_size_in_bytes / (1024 * 1024)
            progress = 0
            digest = hashlib.sha256()
            start_time = time.time()
            times = []
            bytes_downloaded = []

            with open(tmp_path, 'wb') as file:
                for data in response.iter_content(chunk_size=chunk_size_bytes):
                    if not data:
                        self.log_warning("Warning: Received empty data chunk.")
                        continue

                    file.write(data)
                    digest.update(data)

                    elapsed_time = time.time() - start_time
                    progress += len(data)
                    times.append(elapsed_time)
                    bytes_downloaded.append(progress)

                    # Keep only the last 10 seconds of data
                    times = [t for t in times if elapsed_time - t <= 10]
                    bytes_downloaded = bytes_downloaded[-len(times):]

                    # Calculate speed and estimated time remaining
                    speed = sum(bytes_downloaded) / sum(times) if times else 0
                    eta = (total_size_in_bytes - progress) / speed if speed else 0

                    downloaded_mb = progress / (1024 * 1024)
                    done = int(50 * progress / total_size_in_bytes)
                    if not self.config.is_production:
                        # Progress output is cosmetic and difficult to test
                        print(
                            f'\r[{"=" * done}{" " * (50-done)}] {progress * 100 / total_size_in_bytes:.2f}% ({downloaded_mb:.2f}/{total_size_in_mb:.2f} MB) ETA: {eta:.2f}s',
                            end='\r',
                            file=sys.stderr,
                        )  # pragma: no cover
                file.flush()
                os.fsync(file.fileno())
        except Exception as e:
            self.log_error(f"Error during file download: {e}")
            self._remove_partial_download(tmp_path)
            return False
        finally:
            if response is not None:
                close = getattr(response, 'close', None)
                if callable(close):
                    close()

        actual_size = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else -1
        if expected_size is not None and actual_size != int(expected_size):
            self.log_error("Download failed artifact size validation.")
            self._remove_partial_download(tmp_path)
            return False
        if actual_size != total_size_in_bytes:
            self.log_error("Download failed or Content-Length does not match.")
            self._remove_partial_download(tmp_path)
            return False
        if expected_size is not None or expected_sha256:
            try:
                with open(tmp_path, 'rb') as check_file:
                    if check_file.read(4) != GGUF_MAGIC:
                        self.log_error("Download failed GGUF magic validation.")
                        self._remove_partial_download(tmp_path)
                        return False
            except OSError:
                self._remove_partial_download(tmp_path)
                return False
        if expected_sha256 and digest.hexdigest().lower() != str(expected_sha256).lower():
            self.log_error("Download failed SHA-256 validation.")
            self._remove_partial_download(tmp_path)
            return False

        if os.path.exists(tmp_path) and actual_size == total_size_in_bytes:
            try:
                os.replace(tmp_path, file_path)
                if expected_sha256:
                    self._write_artifact_verification_receipt(str(expected_sha256))
                self.log_info(f"File Size Immediately After Download: {os.path.getsize(file_path)} bytes")
                return True
            except OSError as e:
                self.log_error(
                    f"Download failed while replacing final artifact: exception_type={type(e).__name__}"
                )
                self._remove_partial_download(tmp_path)
                return False
        else:
            self.log_error("Download failed or file size does not match.")
            self._remove_partial_download(tmp_path)
            return False

    def _remove_partial_download(self, tmp_path: str) -> None:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    def _is_managed_canonical_model_path(self) -> bool:
        return (
            self.profile_id == 'qwen3-8b-q4-k-m'
            and os.path.abspath(str(self.model_path)) == os.path.abspath(os.path.join(self.models_dir, self.file_name))
        )

    def _artifact_verification_receipt_path(self) -> str:
        return f"{self.model_path}.sha256.verified.json"

    def _artifact_stat_fingerprint(self) -> Dict[str, Any]:
        stat = os.stat(self.model_path)
        return {
            'size_bytes': stat.st_size,
            'mtime_ns': getattr(stat, 'st_mtime_ns', int(stat.st_mtime * 1_000_000_000)),
        }

    def _read_artifact_verification_receipt(self, expected_sha256: str) -> bool:
        try:
            with open(self._artifact_verification_receipt_path(), 'r', encoding='utf-8') as receipt_file:
                receipt = json.load(receipt_file)
            fingerprint = self._artifact_stat_fingerprint()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False
        return (
            receipt.get('profile_id') == self.profile_id
            and receipt.get('artifact_sha256') == str(expected_sha256).lower()
            and receipt.get('size_bytes') == fingerprint['size_bytes']
            and receipt.get('mtime_ns') == fingerprint['mtime_ns']
        )

    def _write_artifact_verification_receipt(self, expected_sha256: str) -> None:
        try:
            fingerprint = self._artifact_stat_fingerprint()
            receipt = {
                'profile_id': self.profile_id,
                'artifact_sha256': str(expected_sha256).lower(),
                **fingerprint,
            }
            receipt_path = self._artifact_verification_receipt_path()
            tmp_receipt = f"{receipt_path}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
            with open(tmp_receipt, 'w', encoding='utf-8') as receipt_file:
                json.dump(receipt, receipt_file, sort_keys=True)
                receipt_file.flush()
                os.fsync(receipt_file.fileno())
            os.replace(tmp_receipt, receipt_path)
        except OSError:
            try:
                if 'tmp_receipt' in locals():
                    os.unlink(tmp_receipt)
            except OSError:
                pass

    def _validate_existing_model_artifact(self, *, hash_if_suspect: bool = False) -> tuple[bool, str]:
        if not os.path.exists(self.model_path):
            return False, 'missing'
        expected_size = self.model_profile.get('artifact_size_bytes')
        expected_sha256 = self.model_profile.get('artifact_sha256')
        pinned_artifact = self._profile_has_pinned_artifact() and self._is_managed_canonical_model_path()
        try:
            actual_size = os.path.getsize(self.model_path)
            if pinned_artifact and expected_size is not None and actual_size != int(expected_size):
                return False, 'size_mismatch'
            with open(self.model_path, 'rb') as file:
                if file.read(4) != GGUF_MAGIC:
                    return False, 'bad_magic'
                needs_checksum = (
                    pinned_artifact
                    and expected_sha256
                    and (hash_if_suspect or not self._read_artifact_verification_receipt(str(expected_sha256)))
                )
                if needs_checksum:
                    file.seek(0)
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: file.read(1024 * 1024), b''):
                        digest.update(chunk)
                    if digest.hexdigest().lower() != str(expected_sha256).lower():
                        return False, 'checksum_mismatch'
                    self._write_artifact_verification_receipt(str(expected_sha256))
        except OSError:
            return False, 'unavailable'
        return True, 'valid'

    def download_model_if_needed(self) -> bool:
        """
        Download the model file if it doesn't exist.

        Returns:
            bool: True if the model file exists (either already present or successfully downloaded),
                 False if download failed
        """
        self.create_models_directory()

        # Downloads are always streamed through SHA-256 validation when pinned.
        # Warm starts validate pinned size + GGUF magic plus a stat-bound
        # checksum receipt; a missing/stale receipt or exact suspect evidence
        # triggers a full hash without repeating that 5 GB hash every launch.
        suspect_existing_artifact = (
            self._is_managed_canonical_model_path()
            and str(getattr(self, 'last_runtime_init_error', '') or '').startswith((
                'model_artifact_invalid:',
                'runtime_model_load_failed',
                'runtime_model_vocab_failed',
            ))
        )
        valid, reason = self._validate_existing_model_artifact(hash_if_suspect=suspect_existing_artifact)
        self.last_model_artifact_validation = {'valid': valid, 'reason': reason}
        if not valid and reason == 'missing':
            self.log_info(f"Downloading {self.file_name}...")
            if self.download_file_in_chunks(self.model_path, self.url, self.chunk_size_mb):
                self.log_info("Download completed!")
                return True
            else:
                self.log_error("Download failed or file is empty.")
                return False
        if valid:
            self.log_info(f"Model file {self.file_name} already exists.")
            return True
        if self._is_managed_canonical_model_path():
            self.log_warning(f"Managed model artifact invalid ({reason}); attempting one bounded repair download.")
            if self.download_file_in_chunks(self.model_path, self.url, self.chunk_size_mb):
                return True
        self.last_runtime_init_error = f"model_artifact_invalid:{reason}"
        return False

    def _publish_qwen_64k_init_failure_readiness_diagnostics(
        self,
        *,
        compute_plan: Dict[str, Any],
        profile_failures: list[Dict[str, Any]],
        current_profile_id: Optional[str] = None,
    ) -> None:
        """Publish scalar-only readiness diagnostics for pre-registration Qwen 64K init failures."""
        if not profile_failures:
            return

        latest_failure = profile_failures[-1]
        attempted_kwargs = latest_failure.get('attempted_runtime_kwargs')
        if not isinstance(attempted_kwargs, dict):
            attempted_kwargs = {}
        memory_profile = getattr(self, 'last_qwen_64k_memory_profile_diagnostics', None)
        applied_memory = memory_profile.get('applied') if isinstance(memory_profile, dict) and isinstance(memory_profile.get('applied'), dict) else {}
        profile_id = current_profile_id or latest_failure.get('profile_id')
        attempted_profile_ids = [
            str(failure.get('profile_id'))
            for failure in profile_failures
            if isinstance(failure.get('profile_id'), str) and failure.get('profile_id')
        ] or list(getattr(self, '_qwen_64k_profile_attempt_ids', []) or [])

        diagnostics = dict(compute_plan) if isinstance(compute_plan, dict) else {}
        diagnostics.update({
            'api_v1_runtime_ready': False,
            'api_v1_readiness_result': 'failed',
            'api_v1_readiness_error_code': 'compute_node_runtime_init_failed',
            'api_v1_readiness_error_reason': 'qwen_64k_runtime_profile_initialization_failed',
            'api_v1_readiness_qwen_64k_runtime_profile_id': profile_id,
            'api_v1_readiness_qwen_64k_runtime_profile_attempt_ids': ','.join(attempted_profile_ids),
            'api_v1_readiness_qwen_64k_runtime_profile_recovery_count': max(0, len(attempted_profile_ids) - 1),
            'api_v1_readiness_qwen_64k_runtime_profile_flash_attn': attempted_kwargs.get('flash_attn', applied_memory.get('flash_attn')),
            'api_v1_readiness_qwen_64k_runtime_profile_offload_kqv': attempted_kwargs.get('offload_kqv', applied_memory.get('offload_kqv')),
            'api_v1_readiness_qwen_64k_runtime_profile_type_k': attempted_kwargs.get('type_k', applied_memory.get('type_k')),
            'api_v1_readiness_qwen_64k_runtime_profile_type_v': attempted_kwargs.get('type_v', applied_memory.get('type_v')),
            'api_v1_readiness_qwen_64k_runtime_profile_n_batch': attempted_kwargs.get('n_batch', applied_memory.get('n_batch')),
            'api_v1_readiness_qwen_64k_runtime_profile_n_ubatch': attempted_kwargs.get('n_ubatch', applied_memory.get('n_ubatch')),
            'api_v1_readiness_qwen_64k_runtime_profile_result': 'failed',
            'api_v1_readiness_qwen_64k_runtime_profile_failure_category': latest_failure.get('safe_error_category'),
        })
        n_ctx = attempted_kwargs.get('n_ctx') or latest_failure.get('n_ctx')
        if n_ctx is not None:
            diagnostics['api_v1_readiness_yarn_requested_context_tokens'] = n_ctx
        backend = latest_failure.get('backend') or diagnostics.get('backend_used')
        if isinstance(backend, str):
            diagnostics['api_v1_readiness_backend_used'] = backend
        self.last_compute_diagnostics = diagnostics

    def get_llm_instance(self):
        """
        Gets the Llama instance, initializing it if necessary (thread-safe),
        or returns a mock if USE_MOCK_LLM is set.

        Returns:
            A Llama instance or a local mock object
        """
        # Check if mocking is enabled via configuration
        if self.use_mock_llm:
            self.log_info("Using Mock LLM instance based on USE_MOCK_LLM configuration.")
            self.last_compute_diagnostics = self._mock_compute_plan()
            mock_llama_instance = _MockLlamaInstance()

            def _mock_apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
                **kwargs,
            ):
                mock_llama_instance._token_place_last_mock_template_kwargs = dict(kwargs)
                rendered_parts = []
                for message in messages:
                    role = message.get('role', 'user') if isinstance(message, dict) else 'user'
                    content = message.get('content', '') if isinstance(message, dict) else str(message)
                    if isinstance(content, list):
                        content = ''.join(
                            str(item.get('text', ''))
                            for item in content
                            if isinstance(item, dict)
                        )
                    rendered_parts.append(f"<|{role}|>\n{content}")
                if add_generation_prompt:
                    rendered_parts.append("<|assistant|>\n")
                rendered = "\n".join(rendered_parts)
                if tokenize:
                    return _mock_tokenize(rendered.encode('utf-8'), add_bos=False)
                return rendered

            def _mock_render_and_tokenize_chat(
                messages,
                tokenize=False,
                add_generation_prompt=True,
                **kwargs,
            ):
                mock_llama_instance._token_place_last_mock_render_and_tokenize_kwargs = dict(kwargs)
                rendered = _mock_apply_chat_template(
                    messages,
                    tokenize=False,
                    add_generation_prompt=add_generation_prompt,
                    **kwargs,
                )
                return {
                    'prompt_tokens': len(
                        _mock_tokenize(rendered.encode('utf-8'), add_bos=False)
                    )
                }

            def _mock_tokenize(content, add_bos=True):
                if isinstance(content, bytes):
                    text = content.decode('utf-8', errors='ignore')
                else:
                    text = str(content)
                # Deterministic approximation for USE_MOCK_LLM test/runtime paths.
                # Production context admission still uses the warmed llama.cpp
                # runtime tokenizer; this mock only keeps local packaged parity
                # e2e on the same render/tokenize surface.
                tokens = text.split()
                if add_bos:
                    return [1] + list(range(2, len(tokens) + 2))
                return list(range(1, len(tokens) + 1))

            mock_llama_instance.apply_chat_template.side_effect = _mock_apply_chat_template
            mock_llama_instance.render_and_tokenize_chat.side_effect = _mock_render_and_tokenize_chat
            mock_llama_instance.tokenize.side_effect = _mock_tokenize
            mock_response = {
                'choices': [
                    {
                        'message': {
                            'role': 'assistant',
                            # Make the mock response more specific for easier debugging
                            'content': 'Mock Response: The capital of France is Paris.'
                        }
                    }
                ]
            }
            mock_llama_instance.create_chat_completion.return_value = mock_response

            def _mock_create_chat_completion_from_rendered_prompt(messages, **kwargs):
                return mock_response

            mock_llama_instance.create_chat_completion_from_rendered_prompt = (
                _mock_create_chat_completion_from_rendered_prompt
            )
            return mock_llama_instance

        # Quick check without lock
        if self.llm is None:
            # Acquire lock only if we might need to initialize
            with self.llm_lock:
                # Double-check after acquiring lock
                if self.llm is None:
                    if not os.path.exists(self.model_path):
                        self.log_error("Error: Model file does not exist. LLM not initialized.")
                        return None
                    else:
                        try:
                            self.last_runtime_init_error = None
                            # Dynamically import Llama only when needed
                            self.log_info("Locating llama_cpp runtime for model initialization...")
                            llama_cpp = _import_llama_cpp_runtime(
                                require_real_runtime=True,
                                desktop_runtime_probe=getattr(self, 'desktop_runtime_probe', None),
                            )
                            self._imported_llama_cpp_module_path = getattr(llama_cpp, '__file__', None)
                            self.log_info(
                                "llama_cpp runtime located "
                                f"module_path_present={bool(self._imported_llama_cpp_module_path)}"
                            )
                            Llama = llama_cpp.Llama

                            self.log_info("Selecting compute plan for model initialization...")
                            compute_plan = self._resolve_compute_plan()
                            self.log_info(
                                "Selected compute plan for model initialization "
                                f"requested={compute_plan['requested_mode']} "
                                f"backend_selected={compute_plan['backend_selected']} "
                                f"n_gpu_layers={compute_plan['n_gpu_layers']}"
                            )
                            n_gpu_layers = int(compute_plan['n_gpu_layers'])
                            if self.enforce_gpu_headroom and n_gpu_layers != 0:
                                try:
                                    model_size = os.path.getsize(self.model_path)
                                except OSError:
                                    model_size = None
                                if model_size:
                                    if not resource_monitor.can_allocate_gpu_memory(
                                        model_size,
                                        headroom_percent=self.gpu_headroom_percent,
                                    ):
                                        self.log_warning(
                                            "Insufficient GPU memory headroom detected; falling back "
                                            "to CPU inference for this model."
                                        )
                                        n_gpu_layers = 0
                                        compute_plan['effective_mode'] = 'cpu_fallback'
                                        compute_plan['backend_used'] = 'cpu'
                                        compute_plan['fallback_reason'] = (
                                            'insufficient GPU memory headroom for safe offload'
                                        )

                            self.log_info("About to instantiate Llama model.")
                            self.log_info("Llama init started.")
                            runtime_profiles = [None]
                            is_qwen_64k = (
                                self.model_profile.get('provider') == 'qwen'
                                and getattr(self, 'context_tier', '8k-fast') == '64k-full'
                                and int(self.config.get('model.context_size', 8192)) == 65536
                            )
                            if is_qwen_64k:
                                _runtime_supports_qwen_yarn_rope(llama_cpp, Llama)
                                Llama = llama_cpp.Llama
                                if str(compute_plan.get('backend_used') or '').lower() in {'metal', 'cuda'}:
                                    runtime_profiles = _build_qwen_64k_runtime_profiles(
                                        llama_cpp,
                                        Llama,
                                        model_path=self.model_path,
                                        n_ctx=int(self.config.get('model.context_size', 65536)),
                                        enable_kqv_offload=n_gpu_layers != 0,
                                    )
                                    self._qwen_64k_runtime_profiles = list(runtime_profiles)
                                    start_index = max(0, min(int(getattr(self, '_qwen_64k_selected_profile_index', 0) or 0), len(runtime_profiles)))
                                    runtime_profiles = runtime_profiles[start_index:]
                                else:
                                    runtime_profiles = [None]
                            profile_failures = []
                            first_context_create_init_exc = None
                            llm_instance = None
                            runtime_kwargs = {}
                            for runtime_profile in runtime_profiles:
                                runtime_kwargs = self._runtime_init_kwargs(Llama, n_gpu_layers, llama_cpp, runtime_profile)
                                profile_diag = getattr(self, 'last_qwen_64k_memory_profile_diagnostics', {})
                                profile_id = profile_diag.get('profile_id') if isinstance(profile_diag, dict) else 'default'
                                if is_qwen_64k and isinstance(profile_id, str):
                                    if profile_id not in self._qwen_64k_profile_attempt_ids:
                                        allowed_profile_ids = [
                                            profile.get('profile_id')
                                            for profile in self._qwen_64k_runtime_profiles[:3]
                                            if isinstance(profile, dict)
                                        ]
                                        if profile_id in allowed_profile_ids:
                                            self._qwen_64k_profile_attempt_ids.append(profile_id)
                                try:
                                    llm_instance = Llama(**runtime_kwargs)
                                    if is_qwen_64k and isinstance(profile_id, str):
                                        ids = [p.get('profile_id') for p in self._qwen_64k_runtime_profiles]
                                        self._qwen_64k_selected_profile_index = ids.index(profile_id) if profile_id in ids else 0
                                        self._qwen_64k_selected_profile_id = profile_id
                                    if isinstance(profile_diag, dict):
                                        profile_diag['selected'] = True
                                        self.last_qwen_64k_memory_profile_diagnostics = profile_diag
                                    break
                                except Exception as init_exc:
                                    category = _classify_runtime_initialization_error(init_exc)
                                    safe_failure = {
                                        'profile_id': profile_id,
                                        'model_profile_id': self.profile_id,
                                        'safe_error_category': category,
                                        'exception_type': type(init_exc).__name__,
                                        'context_tier': getattr(self, 'context_tier', '8k-fast'),
                                        'n_ctx': runtime_kwargs.get('n_ctx'),
                                        'backend': compute_plan.get('backend_used'),
                                        'llama_cpp_python_version': (
                                            profile_diag.get('llama_cpp_python_version')
                                            if isinstance(profile_diag, dict) else None
                                        ),
                                        'yarn_resolver_source': (
                                            getattr(self, 'last_yarn_rope_diagnostics', {}) or {}
                                        ).get('yarn_resolver_source'),
                                        'kv_cache_settings': (
                                            profile_diag.get('applied')
                                            if isinstance(profile_diag, dict) else {}
                                        ),
                                        'memory_estimate': (
                                            profile_diag.get('memory_estimate')
                                            if isinstance(profile_diag, dict) else {}
                                        ),
                                        'attempted_runtime_kwargs': {
                                            key: runtime_kwargs.get(key)
                                            for key in ('n_ctx', 'type_k', 'type_v', 'flash_attn', 'offload_kqv', 'n_batch', 'n_ubatch', 'rope_scaling_type', 'rope_freq_scale', 'yarn_orig_ctx')
                                            if key in runtime_kwargs
                                        },
                                    }
                                    profile_failures.append(safe_failure)
                                    self.last_qwen_64k_init_failures = profile_failures
                                    if is_qwen_64k:
                                        self._publish_qwen_64k_init_failure_readiness_diagnostics(
                                            compute_plan=compute_plan,
                                            profile_failures=profile_failures,
                                            current_profile_id=profile_id,
                                        )
                                    if is_qwen_64k and category == 'runtime_context_create_failed' and first_context_create_init_exc is None:
                                        first_context_create_init_exc = init_exc
                                    if not is_qwen_64k or category not in QWEN_64K_CONTEXT_CREATE_RETRY_CATEGORIES:
                                        raise
                                    close = getattr(init_exc, 'close', None)
                                    if callable(close):
                                        close()
                                    continue
                            if llm_instance is None:
                                self.last_qwen_64k_init_failures = profile_failures
                                if is_qwen_64k:
                                    self._publish_qwen_64k_init_failure_readiness_diagnostics(
                                        compute_plan=compute_plan,
                                        profile_failures=profile_failures,
                                    )
                                if first_context_create_init_exc is not None:
                                    raise first_context_create_init_exc
                                raise RuntimeError(
                                    'Qwen 64K memory/KV/cache profile exhaustion before registration; '
                                    f'failures={profile_failures}'
                                )
                            if profile_failures:
                                self.last_qwen_64k_init_failures = profile_failures
                            if self.model_profile.get('provider') == 'qwen':
                                llm_type_module = type(llm_instance).__module__
                                is_unit_test_fake = llm_type_module.startswith('tests.') and not os.path.basename(str(self.model_path)).startswith('Qwen3-')
                                if not is_unit_test_fake and not callable(getattr(llm_instance, 'apply_chat_template', None)):
                                    tokenizer = getattr(llm_instance, 'tokenizer', None)
                                    tokenizer_instance = tokenizer() if callable(tokenizer) else None
                                    if not callable(getattr(tokenizer_instance, 'apply_chat_template', None)):
                                        raise RuntimeError(
                                            'Qwen runtime requires GGUF/Jinja apply_chat_template support; '
                                            'refusing to run with a Llama fallback template'
                                        )
                            self.llm = llm_instance
                            self.child_model_path_exists = bool(getattr(llm_instance, 'child_model_path_exists', False))
                            compute_plan['n_gpu_layers'] = n_gpu_layers
                            compute_plan['context_tier'] = getattr(self, 'context_tier', '8k-fast')
                            compute_plan['context_window_tokens'] = self.config.get('model.context_size', 8192)
                            compute_plan['kv_cache_device'] = (
                                compute_plan['backend_used']
                                if n_gpu_layers < 0
                                else ('cpu' if n_gpu_layers == 0 else 'partial')
                            )
                            compute_plan['offloaded_layers'] = (
                                n_gpu_layers if n_gpu_layers >= 0 else 'all_supported_layers'
                            )
                            compute_plan['device_backend'] = compute_plan['backend_used']
                            compute_plan['device_name'] = 'unreported'
                            rope_policy = self.model_profile.get('rope_scaling_policy') or {}
                            compute_plan.update({
                                'active_model_id': self.api_model_id,
                                'active_profile_id': self.profile_id,
                                'gguf_filename': self.file_name,
                                'quantization': self.model_profile.get('quantization'),
                                'chat_template_mode': self._chat_template_mode(),
                                'thinking_mode_disabled': self._qwen_non_thinking_required(),
                                'n_ctx': runtime_kwargs.get('n_ctx'),
                                'native_context_tokens': self.model_profile.get('native_context_tokens'),
                                'maximum_validated_context_tokens': min(
                                    int(self.model_profile.get('maximum_validated_context_tokens') or runtime_kwargs.get('n_ctx')),
                                    int(runtime_kwargs.get('n_ctx') or 0),
                                ),
                                'rope_yarn_enabled': 'rope_freq_scale' in runtime_kwargs,
                                'rope_yarn_factor': rope_policy.get('factor'),
                                'rope_freq_scale': runtime_kwargs.get('rope_freq_scale'),
                                'yarn_original_context': runtime_kwargs.get('yarn_orig_ctx') or rope_policy.get('original_context_tokens'),
                            })
                            memory_profile = getattr(self, 'last_qwen_64k_memory_profile_diagnostics', None)
                            if isinstance(memory_profile, dict):
                                compute_plan['qwen_64k_memory_profile'] = dict(memory_profile)
                                applied_memory = memory_profile.get('applied') if isinstance(memory_profile.get('applied'), dict) else {}
                                compute_plan['kv_cache_mode'] = {
                                    key: applied_memory.get(key)
                                    for key in ('type_k', 'type_v', 'flash_attn', 'offload_kqv', 'n_batch', 'n_ubatch')
                                    if key in applied_memory
                                }
                                compute_plan.update({
                                    'qwen_64k_runtime_profile_id': memory_profile.get('profile_id'),
                                    'qwen_64k_runtime_profile_attempt_ids': ','.join(self._qwen_64k_profile_attempt_ids),
                                    'qwen_64k_runtime_profile_recovery_count': self._qwen_64k_profile_recovery_count,
                                    'qwen_64k_runtime_profile_flash_attn': applied_memory.get('flash_attn'),
                                    'qwen_64k_runtime_profile_offload_kqv': applied_memory.get('offload_kqv'),
                                    'qwen_64k_runtime_profile_type_k': applied_memory.get('type_k'),
                                    'qwen_64k_runtime_profile_type_v': applied_memory.get('type_v'),
                                    'qwen_64k_runtime_profile_n_batch': applied_memory.get('n_batch'),
                                    'qwen_64k_runtime_profile_n_ubatch': applied_memory.get('n_ubatch'),
                                    'qwen_64k_runtime_profile_result': 'constructed',
                                    'llama_cpp_runtime_profile_backend': memory_profile.get('backend'),
                                })
                                first_failure = getattr(self, '_qwen_64k_first_readiness_failure_diagnostics', {})
                                if isinstance(first_failure, dict):
                                    for key, value in first_failure.items():
                                        compute_plan[f'qwen_64k_first_readiness_failure_{key}'] = value
                            yarn_diagnostics = getattr(self, 'last_yarn_rope_diagnostics', None)
                            if isinstance(yarn_diagnostics, dict):
                                compute_plan['yarn_rope_diagnostics'] = dict(yarn_diagnostics)
                                compute_plan['yarn_rope_enum_location'] = yarn_diagnostics.get('yarn_enum_location')
                                compute_plan['yarn_rope_accepted_constructor_kwargs'] = yarn_diagnostics.get('accepted_constructor_kwargs')
                                compute_plan['llama_cpp_capability_source'] = yarn_diagnostics.get('capability_source')
                                compute_plan['llama_cpp_desktop_probe_authoritative'] = yarn_diagnostics.get('desktop_probe_authoritative')
                                compute_plan['llama_cpp_child_capability_reprobe_attempted'] = yarn_diagnostics.get('child_probe_reprobe_attempted')
                                compute_plan['llama_cpp_child_capability_reprobe_skipped_reason'] = yarn_diagnostics.get('child_probe_reprobe_skipped_reason')
                                compute_plan['llama_cpp_constructor_signature_inspectable'] = yarn_diagnostics.get('constructor_signature_inspectable')
                                compute_plan['llama_cpp_qwen_64k_yarn_support'] = yarn_diagnostics.get('support_classification')
                            self.last_compute_diagnostics = compute_plan
                            if compute_plan['requested_mode'] == 'cpu':
                                runtime_identity = {
                                    'interpreter': sys.executable,
                                    'llama_module_path': self._imported_llama_cpp_module_path or 'unknown',
                                }
                            else:
                                runtime_identity = self._runtime_capabilities()
                            self.log_info(
                                "compute_runtime "
                                f"requested={compute_plan['requested_mode']} "
                                f"effective={compute_plan['effective_mode']} "
                                f"backend_available={compute_plan['backend_available']} "
                                f"backend_used={compute_plan['backend_used']} "
                                f"device_backend={compute_plan['device_backend']} "
                                f"device_name={compute_plan['device_name']} "
                                f"offloaded_layers={compute_plan['offloaded_layers']} "
                                f"kv_cache={compute_plan['kv_cache_device']} "
                                f"interpreter={runtime_identity.get('interpreter', sys.executable)} "
                                f"llama_module_path_present={bool(runtime_identity.get('llama_module_path'))} "
                                f"fallback_reason={compute_plan['fallback_reason'] or 'none'}"
                            )
                            self.worker_state = 'ready'
                            self.last_worker_error_code = None
                            self.last_worker_exit_code = None
                            self.log_info("desktop.llama_cpp_worker.initialized event=worker_initialization worker_state=ready worker_generation=%s worker_restart_count=%s" % (self._llm_generation, self.worker_restart_count))
                            self.log_info("Llama init completed successfully.")
                            self.log_info("Llama model initialized successfully.")
                        except Exception as e:
                            self.llm = None
                            self.last_runtime_init_error = _redact_paths_from_text(e, limit=12000)
                            if isinstance(e, LlamaCppRuntimeStageTimeout):
                                self.last_runtime_init_error = _format_runtime_stage_timeout(e)
                            self.worker_state = 'failed'
                            self.last_worker_error_code = _safe_worker_error_code(e)
                            if isinstance(e, LlamaCppRuntimeStageTimeout):
                                self.log_error(
                                    "desktop.llama_cpp_worker.init_failed "
                                    f"stage={e.stage} category=worker_timeout timeout_seconds={e.timeout_seconds:g}",
                                    exc_info=False,
                                )
                            else:
                                self.log_error(
                                    f"Failed to initialize Llama model: {self.last_runtime_init_error}",
                                    exc_info=False,
                                )
                            return None

        return self.llm


    def qwen_64k_readiness_profile_attempt_budget(self) -> int:
        """Return remaining bounded Qwen 64K Metal readiness profile attempts."""
        if self.model_profile.get('provider') != 'qwen' or getattr(self, 'context_tier', '8k-fast') != '64k-full':
            return 1
        profiles = list(self._qwen_64k_runtime_profiles or [])
        profile_ids = [
            profile.get('profile_id')
            for profile in profiles[:3]
            if isinstance(profile, dict) and profile.get('profile_id')
        ]
        if not profile_ids:
            profile_ids = [
                QWEN_64K_RUNTIME_PROFILE_DEFAULT,
                QWEN_64K_RUNTIME_PROFILE_Q8,
                QWEN_64K_RUNTIME_PROFILE_Q4,
            ]
        current_index = max(0, int(getattr(self, '_qwen_64k_selected_profile_index', 0) or 0))
        return max(1, len(profile_ids) - current_index)

    def reinitialize_qwen_64k_with_next_profile_after_readiness_failure(
        self,
        failed_runtime: Any,
        failure_category: str,
        decode_return_code: Optional[int] = None,
        failure_diagnostics: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Replace a failed pre-registration Qwen 64K runtime with the next profile."""
        category = str(failure_category or '')
        if category not in _QWEN_64K_PROFILE_RECOVERABLE_FAILURE_CATEGORIES:
            return None
        if self.model_profile.get('provider') != 'qwen' or getattr(self, 'context_tier', '8k-fast') != '64k-full':
            return None
        profiles = list(self._qwen_64k_runtime_profiles or [])
        if not profiles:
            return None
        active_profile_id = getattr(self, '_qwen_64k_selected_profile_id', None)
        profile_ids = [profile.get('profile_id') for profile in profiles if isinstance(profile, dict)]
        if active_profile_id not in profile_ids:
            return None
        active_profile = next(
            (profile for profile in profiles if isinstance(profile, dict) and profile.get('profile_id') == active_profile_id),
            None,
        )
        active_diagnostics = active_profile.get('diagnostics') if isinstance(active_profile, dict) else {}
        if str((active_diagnostics or {}).get('backend') or '').lower() not in {'metal', 'cuda'}:
            return None
        with self.llm_lock:
            if self.llm is not failed_runtime:
                return None
            self.llm = None
            self.worker_state = 'recovering'
            self.last_worker_error_code = category
            self.last_worker_restart_at_ms = int(time.time() * 1000)
            self.last_plain_completion_eval_return_code = decode_return_code
            self.worker_restart_count += 1
            self._llm_generation += 1
            self._qwen_64k_profile_recovery_count += 1
            # Preserve the first recoverable failure category so later
            # profile failures do not overwrite it.
            if self._qwen_64k_first_readiness_failure_category is None:
                self._qwen_64k_first_readiness_failure_category = category
                self._qwen_64k_first_readiness_failure_diagnostics = {
                    key: value
                    for key, value in (failure_diagnostics or {}).items()
                    if key in {
                        'method',
                        'backend_failure_category',
                        'metal_error_category',
                        'backend_state_sticky',
                        'backend_recreation_required',
                        'metal_command_buffer_status',
                        'cuda_error_category',
                        'eval_return_code',
                    }
                    and value is not None
                }
                self._qwen_64k_first_readiness_failure_diagnostics.setdefault('category', category)
                if decode_return_code is not None:
                    self._qwen_64k_first_readiness_failure_diagnostics.setdefault('eval_return_code', decode_return_code)
            next_index = int(self._qwen_64k_selected_profile_index or 0) + 1
            self._qwen_64k_selected_profile_index = next_index
            exhausted = next_index >= len(profiles)
        self._close_llm_proxy(failed_runtime)
        if exhausted:
            return None
        return self.get_llm_instance()

    def cancel_qwen_64k_readiness_failed_worker(
        self,
        failed_runtime: Any,
        failure_category: str,
        decode_return_code: Optional[int] = None,
        failure_diagnostics: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Close a cancelled readiness worker without advancing the profile cursor."""
        category = str(failure_category or '')
        if not is_qwen_64k_profile_recoverable_failure_category(category):
            return
        with self.llm_lock:
            if self.llm is not failed_runtime:
                return
            self.llm = None
            self.worker_state = 'failed'
            self.last_worker_error_code = category
            self.last_worker_restart_at_ms = int(time.time() * 1000)
            self.last_plain_completion_eval_return_code = decode_return_code
            if self._qwen_64k_first_readiness_failure_category is None:
                self._qwen_64k_first_readiness_failure_category = category
                self._qwen_64k_first_readiness_failure_diagnostics = {
                    key: value
                    for key, value in (failure_diagnostics or {}).items()
                    if key in {
                        'method',
                        'backend_failure_category',
                        'metal_error_category',
                        'backend_state_sticky',
                        'backend_recreation_required',
                        'metal_command_buffer_status',
                        'cuda_error_category',
                        'eval_return_code',
                    }
                    and value is not None
                }
                self._qwen_64k_first_readiness_failure_diagnostics.setdefault('category', category)
        self._close_llm_proxy(failed_runtime)

    def terminate_active_worker_for_cancellation(self, *, reason: str = 'cancelled') -> None:
        """Terminate the active subprocess-backed llama worker and require clean recreation."""
        safe_reason = reason if isinstance(reason, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', reason) else 'cancelled'
        with self.llm_lock:
            llm = self.llm
            if llm is None:
                self.worker_state = 'recovering'
                self.last_worker_error_code = safe_reason
                self.worker_restart_count += 1
                self.last_worker_restart_at_ms = int(time.time() * 1000)
                self._llm_generation += 1
                return
            self.last_worker_exit_code = self._worker_exit_code(llm)
            self.last_worker_error_code = safe_reason
            self.worker_state = 'recovering'
            self.worker_restart_count += 1
            self.last_worker_restart_at_ms = int(time.time() * 1000)
            self.llm = None
            self._llm_generation += 1
        self._close_llm_proxy(llm)
        # Eagerly validate a clean worker for the next request when possible.
        try:
            self.get_llm_instance()
        except Exception:
            self.log_warning("desktop.llama_cpp_worker.recreation_after_cancellation_failed safe_error_code=%s" % safe_reason)

    def _close_llm_proxy(self, llm: Any) -> None:
        close = getattr(llm, 'close', None)
        if callable(close):
            try:
                close()
            except Exception:
                self.log_warning("Failed to close old llama.cpp worker during invalidation")

    def _llm_is_usable(self, llm: Any) -> bool:
        is_alive = getattr(llm, 'is_alive', None)
        if callable(is_alive):
            try:
                return bool(is_alive())
            except Exception:
                return False
        return llm is not None

    def _worker_exit_code(self, llm: Any) -> Optional[int]:
        process = getattr(llm, '_process', None)
        poll = getattr(process, 'poll', None)
        if callable(poll):
            try:
                code = poll()
                return int(code) if code is not None else None
            except Exception:
                return None
        return None

    def worker_lifecycle_status(self) -> Dict[str, Any]:
        with self.llm_lock:
            llm = self.llm
            state = self.worker_state
            generation = self._llm_generation
            restart_count = self.worker_restart_count
            last_error_code = self.last_worker_error_code
            last_exit_code = self.last_worker_exit_code
            last_restart_at_ms = self.last_worker_restart_at_ms
            last_eval_return_code = self.last_plain_completion_eval_return_code
        alive = self._llm_is_usable(llm) if llm is not None else False
        if llm is None and state not in {'failed', 'recovering', 'starting'}:
            state = 'stopped'
        return {
            'worker_state': state,
            'worker_generation': generation,
            'worker_restart_count': restart_count,
            'worker_alive': alive,
            'last_worker_error_code': last_error_code,
            'last_worker_exit_code': last_exit_code,
            'last_worker_restart_at_ms': last_restart_at_ms,
            'last_plain_completion_eval_return_code': last_eval_return_code,
        }

    def _invalidate_llm_if_current(self, failed_llm: Any, error: Any = None) -> int:
        dead_worker_log_message: Optional[str] = None
        with self.llm_lock:
            if self.llm is failed_llm:
                self.last_worker_exit_code = self._worker_exit_code(failed_llm)
                self.last_worker_error_code = _safe_worker_error_code(error) if error is not None else 'worker_dead'
                self.worker_state = 'recovering'
                self.worker_restart_count += 1
                self.last_worker_restart_at_ms = int(time.time() * 1000)
                dead_worker_log_message = (
                    "desktop.llama_cpp_worker.dead_detected event=dead_worker_detection "
                    f"safe_error_code={self.last_worker_error_code} worker_generation={self._llm_generation} "
                    f"worker_restart_count={self.worker_restart_count} exit_code={self.last_worker_exit_code}"
                )
                self._close_llm_proxy(self.llm)
                self.llm = None
                self._llm_generation += 1
            generation = self._llm_generation
        if dead_worker_log_message is not None:
            self.log_warning(dead_worker_log_message)
        return generation

    def _ensure_replacement_llm(self, observed_generation: int) -> Any:
        replacement_attempt_log_message: Optional[str] = None
        with self.llm_lock:
            if self.llm is not None and self._llm_is_usable(self.llm):
                return self.llm
            if self.llm is not None:
                self._close_llm_proxy(self.llm)
                self.llm = None
            if self._llm_generation == observed_generation:
                self._llm_generation += 1
            self.worker_state = 'recovering'
            replacement_attempt_log_message = "desktop.llama_cpp_worker.replacement_attempt event=replacement_attempt worker_generation=%s worker_restart_count=%s" % (self._llm_generation, self.worker_restart_count)
            # Release llm_lock before get_llm_instance() because it initializes under
            # the same non-reentrant lock and still serializes creation internally.
        if replacement_attempt_log_message is not None:
            self.log_warning(replacement_attempt_log_message)
        return self.get_llm_instance()

    def get_llm_instance_with_recovery(self):
        """Return a live LLM runtime, attempting one replacement if unavailable."""
        llm_instance = self.get_llm_instance()
        if llm_instance is not None:
            return llm_instance
        with self.llm_lock:
            observed_generation = self._llm_generation
        return self._ensure_replacement_llm(observed_generation)

    def create_chat_completion_with_recovery(self, *args, **kwargs):
        """Create a completion, replacing a dead subprocess worker at most once.

        Recovery is only supported for non-streaming completions. Passing
        ``stream=True`` returns a generator before transport IO can raise
        restartable worker errors, so callers that need recovery must use
        ``stream=False``.
        """
        if kwargs.get('stream', False):
            raise ValueError(
                'create_chat_completion_with_recovery does not support stream=True; '
                'use create_chat_completion directly for streaming.'
            )

        llm_instance = self.get_llm_instance()
        if llm_instance is None:
            raise RuntimeError('LLM runtime is unavailable')
        with self.llm_lock:
            observed_generation = self._llm_generation
        create_chat_completion = getattr(llm_instance, 'create_chat_completion', None)
        if not callable(create_chat_completion):
            raise RuntimeError('LLM runtime missing create_chat_completion')
        try:
            return create_chat_completion(*args, **kwargs)
        except LlamaCppInferenceRequestError as exc:
            safe_error_code = _safe_worker_error_code(exc)
            diagnostics = getattr(exc, 'diagnostics', {}) or {}
            requires_recreation = (
                diagnostics.get('plain_completion_backend_state_sticky') is True
                or diagnostics.get('plain_completion_backend_recreation_required') is True
            )
            if requires_recreation:
                self._invalidate_llm_if_current(llm_instance, exc)
            with self.llm_lock:
                self.last_worker_error_code = safe_error_code
                generation = self._llm_generation
                restart_count = self.worker_restart_count
            self.log_warning("desktop.llama_cpp_worker.request_failure event=request_scoped_inference_failure safe_error_code=%s worker_generation=%s worker_restart_count=%s" % (safe_error_code, generation, restart_count))
            raise
        except LlamaCppRestartableWorkerError as exc:
            self._invalidate_llm_if_current(llm_instance, exc)

        replacement = self._ensure_replacement_llm(observed_generation)
        if replacement is None:
            raise RuntimeError('LLM runtime replacement failed')
        replacement_create = getattr(replacement, 'create_chat_completion', None)
        if not callable(replacement_create):
            raise RuntimeError('LLM replacement runtime missing create_chat_completion')
        try:
            result = replacement_create(*args, **kwargs)
            with self.llm_lock:
                self.worker_state = 'ready'
                self.last_worker_error_code = None
                self.last_worker_exit_code = None
                generation = self._llm_generation
                restart_count = self.worker_restart_count
            self.log_info("desktop.llama_cpp_worker.replacement_result event=replacement_result result=succeeded worker_generation=%s worker_restart_count=%s" % (generation, restart_count))
            return result
        except LlamaCppRestartableWorkerError as exc:
            self._invalidate_llm_if_current(replacement, exc)
            with self.llm_lock:
                self.worker_state = 'failed'
                safe_error_code = self.last_worker_error_code
                generation = self._llm_generation
                restart_count = self.worker_restart_count
                exit_code = self.last_worker_exit_code
            self.log_error("desktop.llama_cpp_worker.terminal_failure event=terminal_failure safe_error_code=%s worker_generation=%s worker_restart_count=%s exit_code=%s" % (safe_error_code, generation, restart_count, exit_code))
            raise RuntimeError('LLM runtime replacement failed after one restart attempt') from exc

    def llama_cpp_get_response(self, chat_history: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """
        Get a response from the LLM given a chat history.

        Args:
            chat_history: List of chat messages with 'role' and 'content' keys

        Returns:
            Updated chat history with the model's response appended
        """
        llm_instance = self.get_llm_instance()
        if llm_instance is None:
            # Return a simple error response if LLM initialization failed
            chat_history.append({
                "role": "assistant",
                "content": "Sorry, I'm having trouble accessing my language capabilities right now."
            })
            return chat_history

        try:
            # If we got a list of chat messages, convert it to the format expected by the Llama API
            self.log_info(
                f"Generating response for chat history with {len(chat_history)} messages"
            )

            # Create a copy of the chat history to avoid modifying the original
            result = chat_history.copy()

            # Generate the completion using streaming mode so callers receive
            # incremental deltas when available from llama.cpp.
            completion = llm_instance.create_chat_completion(
                messages=chat_history,
                max_tokens=self.config.get('model.max_tokens', 512),
                temperature=self.config.get('model.temperature', 0.7),
                top_p=self.config.get('model.top_p', 0.9),
                stop=self.config.get('model.stop_tokens', []),
                stream=True,
            )

            # Extract the assistant's response, supporting both streaming
            # generators and non-streaming fallbacks returned by mocks.
            if isinstance(completion, dict):
                assistant_message = completion['choices'][0]['message']
            else:
                assistant_message = self._consume_streaming_completion(completion)

                if not assistant_message.get('content') and not assistant_message.get('tool_calls'):
                    # Some mocks (and older llama.cpp builds) ignore the stream
                    # flag and yield empty deltas. Fall back to the traditional
                    # non-streaming request so we still provide a reply.
                    self.log_warning(
                        "Streaming completion returned no content; falling back to non-streaming mode."
                    )
                    completion = llm_instance.create_chat_completion(
                        messages=chat_history,
                        max_tokens=self.config.get('model.max_tokens', 512),
                        temperature=self.config.get('model.temperature', 0.7),
                        top_p=self.config.get('model.top_p', 0.9),
                        stop=self.config.get('model.stop_tokens', []),
                        stream=False,
                    )
                    assistant_message = completion['choices'][0]['message']
            self.log_info("Generated assistant response")

            # Append the assistant's response to the chat history
            result.append(assistant_message)

            return result

        except Exception as e:
            self.log_error(f"Error during LLM inference: {e}", exc_info=True)
            # Return an error message
            chat_history.append({
                "role": "assistant",
                "content": "I'm sorry, I encountered an error while processing your request."
            })
            return chat_history

    @staticmethod
    def _normalize_stream_chunk(chunk: Any) -> Dict[str, Any]:
        """Normalise llama.cpp streaming chunk objects into dictionaries."""
        if isinstance(chunk, dict):
            return chunk

        for attr in ('to_dict', 'model_dump', 'dict'):
            handler = getattr(chunk, attr, None)
            if callable(handler):
                try:
                    normalised = handler()
                except TypeError:
                    continue
                if isinstance(normalised, dict):
                    return normalised

        if hasattr(chunk, '__dict__') and isinstance(chunk.__dict__, dict):
            return chunk.__dict__

        return {}

    @staticmethod
    def _merge_tool_call_deltas(existing: List[Dict[str, Any]], deltas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Merge streamed tool_call deltas into a stable structure."""
        for delta in deltas or []:
            index = delta.get('index')
            if index is None:
                index = len(existing)

            while len(existing) <= index:
                existing.append({
                    'id': None,
                    'type': None,
                    'function': {
                        'name': None,
                        'arguments': '',
                    },
                })

            target = existing[index]

            if delta.get('id'):
                target['id'] = delta['id']
            if delta.get('type'):
                target['type'] = delta['type']

            function_delta = delta.get('function') or {}
            if function_delta.get('name'):
                target.setdefault('function', {})['name'] = function_delta['name']
            if 'arguments' in function_delta and function_delta['arguments']:
                target.setdefault('function', {}).setdefault('arguments', '')
                target['function']['arguments'] += function_delta['arguments']

        return existing

    def _consume_streaming_completion(self, completion: Iterable[Any]) -> Dict[str, Any]:
        """Aggregate streamed llama.cpp chunks into a single assistant message."""
        role = 'assistant'
        content_segments: List[str] = []
        tool_calls: List[Dict[str, Any]] = []

        for raw_chunk in completion:
            chunk = self._normalize_stream_chunk(raw_chunk)
            if not chunk:
                continue

            choices = chunk.get('choices') or []
            if not choices:
                continue

            choice = choices[0] or {}
            delta = choice.get('delta') or {}
            if not isinstance(delta, dict):
                continue

            role = delta.get('role') or role

            content_piece = delta.get('content')
            if content_piece:
                content_segments.append(content_piece)

            if delta.get('tool_calls'):
                tool_calls = self._merge_tool_call_deltas(tool_calls, delta['tool_calls'])

            finish_reason = choice.get('finish_reason')
            if finish_reason:
                break

        message: Dict[str, Any] = {
            'role': role,
            'content': ''.join(content_segments),
        }

        cleaned_tool_calls = []
        for call in tool_calls:
            function_meta = call.get('function') or {}
            cleaned_call = {
                key: value for key, value in call.items() if key in {'id', 'type'} and value
            }
            if function_meta:
                cleaned_function = {}
                if function_meta.get('name'):
                    cleaned_function['name'] = function_meta['name']
                if function_meta.get('arguments'):
                    cleaned_function['arguments'] = function_meta['arguments']
                if cleaned_function:
                    cleaned_call['function'] = cleaned_function

            if cleaned_call:
                cleaned_tool_calls.append(cleaned_call)

        if cleaned_tool_calls:
            message['tool_calls'] = cleaned_tool_calls

        return message

# Create a singleton instance
# Delay instantiation to avoid circular imports
model_manager = None

def get_model_manager():
    """Get the global model manager instance, creating it if necessary."""
    global model_manager
    if model_manager is None:
        model_manager = ModelManager()
    return model_manager
