import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'desktop-tauri' / 'scripts' / 'prepare_embedded_python_runtime.py'
spec = importlib.util.spec_from_file_location('prepare_embedded_python_runtime', SCRIPT)
assert spec is not None
assert spec.loader is not None
prep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prep)

def manifest(**overrides):
    m = {
        'schema_version': 1,
        'cpython_version': '3.11.13',
        'python_build_standalone_release': '20250818',
        'python_build_standalone_build': 'cpython-3.11.13+20250818-aarch64-apple-darwin-install_only',
        'target_triple': 'aarch64-apple-darwin',
        'archive_url': 'https://example.test/runtime.tar.gz',
        'sha256': '0' * 64,
        'expected_archive_root': 'python',
        'expected_interpreter_path': 'bin/python3',
        'expected_architecture': 'arm64',
        'minimum_macos_version': '12.0',
        'expected_packaged_runtime_path': 'Contents/Resources/python-runtime/bin/python3',
        'required_packages': {
            'psutil': '7.1.0',
            'requests': '2.32.5',
            'python-dotenv': '1.1.1',
            'cryptography': '46.0.1',
            'Jinja2': '3.1.6',
            'numpy': '2.3.3',
            'diskcache': '5.6.3',
            'llama-cpp-python': '0.3.32',
        },
        'runtime_notices': [],
    }
    m.update(overrides)
    return m

def make_tar(path: Path, members: dict[str, bytes | None]):
    with tarfile.open(path, 'w:gz') as tf:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            else:
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))

def test_manifest_rejects_unknown_schema_and_non_https(tmp_path):
    p = tmp_path / 'm.json'
    p.write_text(json.dumps(manifest(schema_version=99)))
    try:
        prep.load_manifest(p)
        assert False
    except prep.RuntimePrepError as exc:
        assert 'schema_version' in str(exc)
    p.write_text(json.dumps(manifest(archive_url='http://example.test/runtime.tar.gz')))
    try:
        prep.load_manifest(p)
        assert False
    except prep.RuntimePrepError as exc:
        assert 'HTTPS' in str(exc)

def test_digest_mismatch_rejects_invalid_cache(tmp_path, monkeypatch):
    cache = tmp_path / 'cache'; cache.mkdir()
    cached = cache / 'runtime.tar.gz'; cached.write_bytes(b'bad')
    def fake_urlretrieve(url, dst): Path(dst).write_bytes(b'also-bad')
    monkeypatch.setattr(prep.urllib.request, 'urlretrieve', fake_urlretrieve)
    try:
        prep.download_verified(manifest(sha256='1' * 64), cache)
        assert False
    except prep.RuntimePrepError as exc:
        assert 'digest mismatch' in str(exc)

def test_valid_cache_reuse(tmp_path, monkeypatch):
    cache = tmp_path / 'cache'; cache.mkdir()
    cached = cache / 'runtime.tar.gz'; cached.write_bytes(b'ok')
    called = False
    monkeypatch.setattr(prep.urllib.request, 'urlretrieve', lambda *_: (_ for _ in ()).throw(AssertionError('downloaded')))
    result = prep.download_verified(manifest(sha256=hashlib.sha256(b'ok').hexdigest()), cache)
    assert result == cached

def test_extract_rejects_traversal_absolute_and_incomplete_layout(tmp_path):
    for name in ['python/../evil', '/python/bin/python3']:
        archive = tmp_path / (name.replace('/', '_') + '.tar.gz')
        make_tar(archive, {name: b'x'})
        try:
            prep.extract_archive(archive, manifest(), tmp_path / ('x' + archive.name))
            assert False
        except prep.RuntimePrepError:
            pass
    archive = tmp_path / 'incomplete.tar.gz'
    make_tar(archive, {'python/README': b'x'})
    try:
        prep.extract_archive(archive, manifest(), tmp_path / 'incomplete')
        assert False
    except prep.RuntimePrepError as exc:
        assert 'interpreter' in str(exc)

def test_extract_valid_archive(tmp_path):
    archive = tmp_path / 'valid.tar.gz'
    make_tar(archive, {'python/bin/python3': b'#!/bin/sh\n'})
    root = prep.extract_archive(archive, manifest(), tmp_path / 'valid')
    assert (root / 'bin' / 'python3').is_file()

def test_runtime_capability_probe_uses_serialized_runtime_probe_field_names():
    payload = {
        'rope_scaling_type_supported': True,
        'rope_freq_scale_supported': True,
        'yarn_orig_ctx_supported': True,
        'constructor_kwarg_support': {
            'flash_attn': True,
            'offload_kqv': True,
            'n_batch': True,
            'n_ubatch': True,
        },
    }

    assert prep._missing_runtime_capabilities(payload) == []


def test_runtime_capability_probe_reports_nested_constructor_gaps():
    payload = {
        'rope_scaling_type_supported': True,
        'rope_freq_scale_supported': False,
        'yarn_orig_ctx_supported': True,
        'constructor_kwarg_support': {'flash_attn': True},
    }

    assert prep._missing_runtime_capabilities(payload) == [
        'rope_freq_scale',
        'offload_kqv',
        'n_batch',
        'n_ubatch',
    ]
