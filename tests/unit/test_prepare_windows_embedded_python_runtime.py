import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'desktop-tauri' / 'scripts' / 'prepare_windows_embedded_python_runtime.py'
spec = importlib.util.spec_from_file_location('prepare_windows_embedded_python_runtime', SCRIPT)
assert spec is not None
assert spec.loader is not None
prep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prep)


def manifest(**overrides):
    data = {
        'schema_version': 1,
        'cpython_version': '3.11.13',
        'target_triple': 'x86_64-pc-windows-msvc',
        'archive_url': 'https://github.com/example/runtime.tar.gz',
        'sha256': '0' * 64,
        'expected_archive_root': 'python',
        'expected_interpreter_path': 'python.exe',
        'expected_architecture': 'AMD64',
        'llama_cpp_cuda_wheel': {
            'name': 'llama_cpp_python-0.3.32-py3-none-win_amd64.whl',
            'version': '0.3.32',
            'flavor': 'cu124',
            'url': 'https://github.com/abetlen/llama-cpp-python/releases/download/v0.3.32-cu124/llama_cpp_python-0.3.32-py3-none-win_amd64.whl',
            'sha256': '1' * 64,
        },
        'required_packages': {'pip': '25.2', 'llama-cpp-python': '0.3.32'},
        'required_native_dlls': ['llama.dll'],
    }
    data.update(overrides)
    return data


def write_manifest(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding='utf-8')


def test_load_manifest_requires_target_triple(tmp_path):
    path = tmp_path / 'manifest.json'
    data = manifest()
    data.pop('target_triple')
    write_manifest(path, data)

    with pytest.raises(prep.RuntimePrepError, match='target_triple'):
        prep.load_manifest(path)


def test_safe_extract_tar_rejects_prefix_escape_and_links(tmp_path):
    archive = tmp_path / 'bad.tar.gz'
    with tarfile.open(archive, 'w:gz') as tf:
        info = tarfile.TarInfo('../dest2/evil.txt')
        payload = b'evil'
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))

    with pytest.raises(prep.RuntimePrepError, match='escapes destination'):
        prep.safe_extract_tar(archive, tmp_path / 'dest')

    link_archive = tmp_path / 'link.tar.gz'
    with tarfile.open(link_archive, 'w:gz') as tf:
        info = tarfile.TarInfo('python/link')
        info.type = tarfile.SYMTYPE
        info.linkname = '../outside'
        tf.addfile(info)

    with pytest.raises(prep.RuntimePrepError, match='links are not allowed'):
        prep.safe_extract_tar(link_archive, tmp_path / 'dest')


def test_prepare_installs_baseline_packages_binary_only(tmp_path, monkeypatch):
    runtime_root = tmp_path / 'desktop-tauri' / 'src-tauri' / 'python-runtime'
    archive_root = 'cpython'
    m = manifest(
        expected_archive_root=archive_root,
        required_packages={'alpha': '1.0', 'llama-cpp-python': '0.3.32'},
    )
    wheel = tmp_path / m['llama_cpp_cuda_wheel']['name']
    wheel.write_bytes(b'wheel')
    archive = tmp_path / 'runtime.tar.gz'
    archive.write_bytes(b'archive')
    commands = []
    runtime_root.parent.mkdir(parents=True)

    monkeypatch.setattr(prep, 'ROOT', tmp_path / 'desktop-tauri')
    monkeypatch.setattr(prep, 'SRC_TAURI', tmp_path / 'desktop-tauri' / 'src-tauri')
    monkeypatch.setattr(prep, 'OUTPUT', runtime_root)
    monkeypatch.setattr(prep, 'fetch', lambda url, sha, dest: wheel if url.endswith('.whl') else archive)
    monkeypatch.setattr(prep, 'validate_wheel', lambda whl, data: None)

    def fake_extract(_archive, dest):
        staged = dest / archive_root
        staged.mkdir(parents=True)
        (staged / 'python.exe').write_text('', encoding='utf-8')
        (staged / 'llama.dll').write_text('', encoding='utf-8')

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        class Result:
            stdout = json.dumps({'version': [3, 11], 'machine': 'AMD64'})
        return Result()

    monkeypatch.setattr(prep, 'safe_extract_tar', fake_extract)
    monkeypatch.setattr(prep, 'run', fake_run)

    prep.prepare(m)

    baseline_cmd = commands[0]
    assert Path(baseline_cmd[0]).name == 'python.exe'
    assert baseline_cmd[1:5] == ['-m', 'pip', 'install', '--disable-pip-version-check']
    assert '--only-binary' in baseline_cmd
    assert ':all:' in baseline_cmd
    assert '--prefer-binary' in baseline_cmd
    assert 'alpha==1.0' in baseline_cmd
