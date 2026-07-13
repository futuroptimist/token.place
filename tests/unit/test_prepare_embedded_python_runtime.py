import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

import importlib.util

SCRIPT = Path('desktop-tauri/scripts/prepare_embedded_python_runtime.py').resolve()
spec = importlib.util.spec_from_file_location('prepare_embedded_python_runtime', SCRIPT)
prep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prep)


def _archive(tmp_path, members):
    path = tmp_path / 'fake.tar.gz'
    with tarfile.open(path, 'w:gz') as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            payload = data.encode()
            info.size = len(payload)
            if name.endswith('python3'):
                info.mode = 0o755
            tf.addfile(info, io.BytesIO(payload))
    return path


def _manifest(archive):
    return {
        'schema_version': 1,
        'cpython_version': '3.11.10',
        'target_triple': 'aarch64-apple-darwin',
        'archive_url': 'https://example.test/fake.tar.gz',
        'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
        'expected_archive_root': 'python',
        'expected_interpreter_path': 'bin/python3',
        'expected_architecture': 'arm64',
    }


def test_manifest_rejects_non_https(tmp_path):
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps({'schema_version': 1, 'archive_url': 'http://example.test/x', 'sha256': '0'*64, 'target_triple': 'aarch64-apple-darwin', 'expected_interpreter_path': 'bin/python3'}))
    with pytest.raises(prep.RuntimeErrorClosed, match='https'):
        prep.load_manifest(path)


def test_safe_extract_valid_archive(tmp_path):
    archive = _archive(tmp_path, {'python/bin/python3': '#!/bin/sh\n'})
    out = tmp_path / 'out'
    out.mkdir()
    prep.safe_extract(archive, out, 'python')
    assert (out / 'python/bin/python3').exists()


def test_safe_extract_rejects_traversal(tmp_path):
    archive = _archive(tmp_path, {'python/../evil': 'x'})
    with pytest.raises(prep.RuntimeErrorClosed, match='unsafe archive path'):
        prep.safe_extract(archive, tmp_path / 'out', 'python')


def test_safe_extract_rejects_unexpected_root(tmp_path):
    archive = _archive(tmp_path, {'not-python/bin/python3': 'x'})
    out = tmp_path / 'out'; out.mkdir()
    with pytest.raises(prep.RuntimeErrorClosed, match='unexpected archive root'):
        prep.safe_extract(archive, out, 'python')


def test_download_rejects_invalid_cache(tmp_path, monkeypatch):
    archive = _archive(tmp_path, {'python/bin/python3': 'x'})
    manifest = _manifest(archive)
    cache = tmp_path / 'cache'; cache.mkdir()
    cached = cache / 'fake.tar.gz'; cached.write_text('bad')
    def fake_urlretrieve(url, dest):
        Path(dest).write_bytes(archive.read_bytes())
    monkeypatch.setattr(prep.urllib.request, 'urlretrieve', fake_urlretrieve)
    assert prep.download(manifest, cache).read_bytes() == archive.read_bytes()


def test_provenance_contains_safe_metadata(monkeypatch, tmp_path):
    py = tmp_path / 'python/bin/python3'; py.parent.mkdir(parents=True); py.write_text('')
    monkeypatch.setattr(prep, 'packages', lambda _: {'llama-cpp-python': '0.3.32'})
    data = prep.provenance({'cpython_version': '3.11.10', 'target_triple': 'aarch64-apple-darwin', 'sha256': 'a'*64}, {'backend': 'metal'}, py)
    assert data['source_archive_digest'] == 'a'*64
    assert data['expected_backend'] == 'metal'
    assert 'installed_package_versions' in data
