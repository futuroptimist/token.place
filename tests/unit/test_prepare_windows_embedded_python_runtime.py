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


def write_minimal_pe(path: Path, *, machine: int = 0x8664, imports: list[str] | None = None, delay_imports: list[str] | None = None) -> None:
    imports = imports or []
    delay_imports = delay_imports or []
    data = bytearray(0x600)
    data[0:2] = b'MZ'
    data[0x3C:0x40] = (0x80).to_bytes(4, 'little')
    data[0x80:0x84] = b'PE\0\0'
    data[0x84:0x86] = machine.to_bytes(2, 'little')
    data[0x86:0x88] = (1).to_bytes(2, 'little')
    data[0x94:0x96] = (0xF0).to_bytes(2, 'little')
    opt = 0x98
    data[opt:opt+2] = (0x20B).to_bytes(2, 'little')
    data[opt+112+8:opt+112+12] = (0x1100).to_bytes(4, 'little')
    if delay_imports:
        data[opt+112+(8*13):opt+112+(8*13)+4] = (0x1300).to_bytes(4, 'little')
    sec = opt + 0xF0
    data[sec:sec+8] = b'.rdata\0\0'
    data[sec+8:sec+12] = (0x400).to_bytes(4, 'little')
    data[sec+12:sec+16] = (0x1000).to_bytes(4, 'little')
    data[sec+16:sec+20] = (0x400).to_bytes(4, 'little')
    data[sec+20:sec+24] = (0x200).to_bytes(4, 'little')
    base = 0x300
    for idx, name in enumerate(imports):
        desc = base + idx * 20
        name_rva = 0x1200 + idx * 32
        data[desc+12:desc+16] = name_rva.to_bytes(4, 'little')
        name_off = 0x200 + (name_rva - 0x1000)
        data[name_off:name_off+len(name)] = name.encode('ascii')
    delay_base = 0x500
    for idx, name in enumerate(delay_imports):
        desc = delay_base + idx * 32
        name_rva = 0x1380 + idx * 32
        data[desc+4:desc+8] = name_rva.to_bytes(4, 'little')
        data[desc+16:desc+20] = (0x13c0 + idx * 8).to_bytes(4, 'little')
        name_off = 0x200 + (name_rva - 0x1000)
        data[name_off:name_off+len(name)] = name.encode('ascii')
    path.write_bytes(data)

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
        'native_dll_artifacts': [
            {
                'name': name,
                'version': version,
                'url': f'https://developer.download.nvidia.com/example/{name}.zip' if name.startswith(('cuda', 'cublas')) else f'https://download.visualstudio.microsoft.com/example/{name}.zip',
                'sha256': '3' * 64,
                'architecture': 'AMD64',
                'license': 'test license',
                'provenance': 'test provenance',
                'archive_member_path': f'bin/{name}',
                'destination': name,
                'extracted_sha256': '4' * 64,
            }
            for name, version in [
                ('cudart64_12.dll', '12.4.127'),
                ('cublas64_12.dll', '12.4.5.8'),
                ('msvcp140.dll', '14.44.35211.0'),
                ('vcomp140.dll', '14.44.35211.0'),
            ]
        ],
        'python_package_wheels': [],
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



def test_load_manifest_requires_exact_windows_x86_64_target(tmp_path):
    path = tmp_path / 'manifest.json'
    write_manifest(path, manifest(target_triple='aarch64-pc-windows-msvc'))

    with pytest.raises(prep.RuntimePrepError, match='x86_64-pc-windows-msvc'):
        prep.load_manifest(path)


def test_normalizes_vendor_neutral_windows_x86_64_architectures(monkeypatch):
    assert prep.normalize_windows_x86_64_arch('AMD64') == 'x86_64'
    assert prep.normalize_windows_x86_64_arch('x64') == 'x86_64'
    assert prep.normalize_windows_x86_64_arch('x86-64') == 'x86_64'
    monkeypatch.setattr(prep.platform, 'machine', lambda: 'AMD64')
    prep.validate_host_architecture()


def test_rejects_windows_arm64_host_without_cpu_vendor_checks(monkeypatch):
    monkeypatch.setattr(prep.platform, 'machine', lambda: 'ARM64')

    with pytest.raises(prep.RuntimePrepError, match='x86-64 host'):
        prep.validate_host_architecture()


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



def test_validate_runtime_payload_allows_inert_cpython_headers_but_rejects_tools(tmp_path):
    runtime = tmp_path / 'runtime'
    (runtime / 'include').mkdir(parents=True)
    (runtime / 'include' / 'abstract.h').write_text('/* CPython header */', encoding='utf-8')
    for dll in ('python311.dll', 'vcruntime140.dll', 'llama.dll'):
        write_minimal_pe(runtime / dll)

    prep.validate_runtime_payload(
        runtime,
        manifest(required_native_dlls=['python311.dll', 'vcruntime140.dll', 'llama.dll']),
    )

    tools = runtime / 'CUDA Toolkit' / 'bin'
    tools.mkdir(parents=True)
    (tools / 'nvcc.exe').write_text('', encoding='utf-8')
    with pytest.raises(prep.RuntimePrepError, match='forbidden compiler/toolkit'):
        prep.validate_runtime_payload(
            runtime,
            manifest(required_native_dlls=['python311.dll', 'vcruntime140.dll', 'llama.dll']),
        )

def test_prepare_installs_baseline_packages_binary_only(tmp_path, monkeypatch):
    runtime_root = tmp_path / 'desktop-tauri' / 'src-tauri' / 'python-runtime'
    archive_root = 'cpython'
    m = manifest(
        expected_archive_root=archive_root,
        required_packages={'alpha': '1.0', 'llama-cpp-python': '0.3.32'},
        python_package_wheels=[{
            'package': 'alpha', 'version': '1.0',
            'filename': 'alpha-1.0-py3-none-any.whl',
            'url': 'https://files.pythonhosted.org/packages/alpha-1.0-py3-none-any.whl',
            'sha256': '2' * 64,
        }],
    )
    m['native_dll_artifacts'] = []
    wheel = tmp_path / m['llama_cpp_cuda_wheel']['name']
    wheel.write_bytes(b'wheel')
    archive = tmp_path / 'runtime.tar.gz'
    archive.write_bytes(b'archive')
    commands = []
    requirement_texts = []
    runtime_root.parent.mkdir(parents=True)

    monkeypatch.setattr(prep, 'ROOT', tmp_path / 'desktop-tauri')
    monkeypatch.setattr(prep, 'SRC_TAURI', tmp_path / 'desktop-tauri' / 'src-tauri')
    monkeypatch.setattr(prep, 'OUTPUT', runtime_root)
    monkeypatch.setattr(prep, 'fetch', lambda url, sha, dest: wheel if url.endswith('.whl') else archive)
    monkeypatch.setattr(prep, 'validate_wheel', lambda whl, data: None)
    monkeypatch.setattr(prep, 'validate_python_package_wheel', lambda whl, artifact: None)

    def fake_extract(_archive, dest):
        staged = dest / archive_root
        staged.mkdir(parents=True)
        (staged / 'python.exe').write_text('', encoding='utf-8')
        (staged / 'llama.dll').write_text('', encoding='utf-8')

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        if '-r' in cmd:
            requirement_texts.append(Path(cmd[-1]).read_text(encoding='utf-8'))
        class Result:
            stdout = json.dumps({'version': [3, 11, 13], 'machine': 'AMD64'})
        if cmd[1:3] == ['-m', 'pip'] or 'pip' in cmd:
            return Result()
        if 'importlib.metadata' in cmd[-1]:
            Result.stdout = json.dumps({'alpha': '1.0', 'llama-cpp-python': '0.3.32'})
        return Result()

    monkeypatch.setattr(prep, 'safe_extract_tar', fake_extract)
    monkeypatch.setattr(prep, 'run', fake_run)
    monkeypatch.setattr(prep, 'validate_runtime_payload', lambda runtime, data: [])
    monkeypatch.setattr(prep.platform, 'machine', lambda: 'AMD64')

    prep.prepare(m)

    baseline_cmd = commands[0]
    assert Path(baseline_cmd[0]).name == 'python.exe'
    assert baseline_cmd[1:5] == ['-m', 'pip', 'install', '--disable-pip-version-check']
    assert '--only-binary' in baseline_cmd
    assert ':all:' in baseline_cmd
    assert '--no-index' in baseline_cmd
    assert '--require-hashes' in baseline_cmd
    assert '--find-links' in baseline_cmd
    assert requirement_texts == ['alpha==1.0 --hash=sha256:' + '2' * 64 + '\n']


def test_sha256_file_and_fetch_rejects_unpinned_or_mismatched_artifacts(tmp_path):
    artifact = tmp_path / 'artifact.bin'
    artifact.write_bytes(b'token-place')
    digest = prep.sha256_file(artifact)
    assert digest == '68e80e55363cd61ec4d038a99d2705886d56de5a93793e48ad4824f2c45104f0'

    with pytest.raises(prep.RuntimePrepError, match='immutable HTTPS'):
        prep.fetch('https://example.com/runtime.tar.gz', digest, artifact)

    with pytest.raises(prep.RuntimePrepError, match='digest mismatch'):
        prep.fetch('https://github.com/example/runtime.tar.gz', 'f' * 64, artifact)



def test_fetch_uses_part_file_and_does_not_poison_cache_on_mismatch(tmp_path, monkeypatch):
    dest = tmp_path / 'artifact.zip'

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    monkeypatch.setattr(prep.urllib.request, 'urlopen', lambda *args, **kwargs: Response(b'bad-bytes'))

    with pytest.raises(prep.RuntimePrepError, match='digest mismatch'):
        prep.fetch('https://github.com/example/artifact.zip', '0' * 64, dest)

    assert not dest.exists()
    assert not (tmp_path / 'artifact.zip.part').exists()


def test_load_manifest_requires_native_inventory_and_unique_destinations(tmp_path):
    path = tmp_path / 'manifest.json'
    data = manifest()
    data['native_dll_artifacts'] = []
    write_manifest(path, data)
    with pytest.raises(prep.RuntimePrepError, match='non-empty'):
        prep.load_manifest(path)

    data = manifest()
    data['native_dll_artifacts'][1]['destination'] = data['native_dll_artifacts'][0]['destination']
    write_manifest(path, data)
    with pytest.raises(prep.RuntimePrepError, match='duplicate native DLL destination'):
        prep.load_manifest(path)


def test_load_manifest_rejects_wrong_schema_wheel_flavor_and_architecture(tmp_path):
    path = tmp_path / 'manifest.json'

    write_manifest(path, manifest(schema_version=2))
    with pytest.raises(prep.RuntimePrepError, match='schema_version'):
        prep.load_manifest(path)

    bad_wheel = dict(manifest()['llama_cpp_cuda_wheel'])
    bad_wheel['name'] = 'llama_cpp_python-0.3.32-py3-none-win_arm64.whl'
    write_manifest(path, manifest(llama_cpp_cuda_wheel=bad_wheel))
    with pytest.raises(prep.RuntimePrepError, match='wheel name'):
        prep.load_manifest(path)

    bad_flavor = dict(manifest()['llama_cpp_cuda_wheel'])
    bad_flavor['flavor'] = 'cpu'
    write_manifest(path, manifest(llama_cpp_cuda_wheel=bad_flavor))
    with pytest.raises(prep.RuntimePrepError, match='version/flavor'):
        prep.load_manifest(path)

    write_manifest(path, manifest(expected_architecture='ARM64'))
    with pytest.raises(prep.RuntimePrepError, match='architecture must be AMD64'):
        prep.load_manifest(path)

    bad_hash = dict(manifest()['llama_cpp_cuda_wheel'])
    bad_hash['sha256'] = 'not-a-hash'
    write_manifest(path, manifest(llama_cpp_cuda_wheel=bad_hash))
    with pytest.raises(prep.RuntimePrepError, match='wheel sha256'):
        prep.load_manifest(path)


def test_manifest_validates_local_wheelhouse_contract(tmp_path):
    path = tmp_path / 'manifest.json'
    wheel_artifact = {
        'package': 'requests',
        'version': '2.32.5',
        'filename': 'requests-2.32.5-py3-none-any.whl',
        'url': 'https://files.pythonhosted.org/packages/requests-2.32.5-py3-none-any.whl',
        'sha256': 'a' * 64,
    }
    write_manifest(path, manifest(required_packages={'pip': '25.2', 'requests': '2.32.5', 'llama-cpp-python': '0.3.32'}, python_package_wheels=[wheel_artifact]))
    assert prep.load_manifest(path)['python_package_wheels'] == [wheel_artifact]

    bad = dict(wheel_artifact, filename='requests-2.32.5.tar.gz')
    write_manifest(path, manifest(python_package_wheels=[bad]))
    with pytest.raises(prep.RuntimePrepError, match='must be wheels'):
        prep.load_manifest(path)

    bad = dict(wheel_artifact, url='https://example.com/requests.whl')
    write_manifest(path, manifest(python_package_wheels=[bad]))
    with pytest.raises(prep.RuntimePrepError, match='immutable HTTPS'):
        prep.load_manifest(path)

    bad = dict(wheel_artifact, filename='requests-2.32.5-py3-none-win_arm64.whl')
    write_manifest(path, manifest(python_package_wheels=[bad]))
    with pytest.raises(prep.RuntimePrepError, match='win_amd64 or none-any'):
        prep.load_manifest(path)


def _write_wheel(path: Path, *, metadata: str, wheel_text: str, include_dll: bool = True) -> None:
    import zipfile

    with zipfile.ZipFile(path, 'w') as zf:
        zf.writestr('llama_cpp_python-0.3.32.dist-info/METADATA', metadata)
        zf.writestr('llama_cpp_python-0.3.32.dist-info/WHEEL', wheel_text)
        if include_dll:
            zf.writestr('llama_cpp/lib/llama.dll', b'dll')


def test_validate_wheel_rejects_metadata_tag_and_native_runtime_mismatches(tmp_path):
    m = manifest()
    wheel = tmp_path / m['llama_cpp_cuda_wheel']['name']

    _write_wheel(wheel, metadata='Name: other\nVersion: 0.3.32\n', wheel_text='Tag: py3-none-win_amd64\n')
    with pytest.raises(prep.RuntimePrepError, match='package name mismatch'):
        prep.validate_wheel(wheel, m)

    _write_wheel(wheel, metadata='Name: llama-cpp-python\nVersion: 0.3.31\n', wheel_text='Tag: py3-none-win_amd64\n')
    with pytest.raises(prep.RuntimePrepError, match='version mismatch'):
        prep.validate_wheel(wheel, m)

    _write_wheel(wheel, metadata='Name: llama-cpp-python\nVersion: 0.3.32\n', wheel_text='Tag: py3-none-win_arm64\n')
    with pytest.raises(prep.RuntimePrepError, match='wheel tag'):
        prep.validate_wheel(wheel, m)

    _write_wheel(wheel, metadata='Name: llama-cpp-python\nVersion: 0.3.32\n', wheel_text='Tag: py3-none-win_amd64\n', include_dll=False)
    with pytest.raises(prep.RuntimePrepError, match='llama.dll'):
        prep.validate_wheel(wheel, m)


def test_validate_python_package_wheel_rejects_metadata_and_tag_mismatches(tmp_path):
    import zipfile

    artifact = {
        'package': 'typing-extensions',
        'version': '4.15.0',
        'filename': 'typing_extensions-4.15.0-py3-none-any.whl',
        'url': 'https://files.pythonhosted.org/packages/typing_extensions.whl',
        'sha256': 'f' * 64,
    }
    wheel = tmp_path / artifact['filename']

    def write_pkg_wheel(metadata: str, wheel_text: str) -> None:
        with zipfile.ZipFile(wheel, 'w') as zf:
            zf.writestr('typing_extensions-4.15.0.dist-info/METADATA', metadata)
            zf.writestr('typing_extensions-4.15.0.dist-info/WHEEL', wheel_text)

    write_pkg_wheel('Name: typing-extensions\nVersion: 4.15.0\n', 'Tag: py3-none-any\n')
    prep.validate_python_package_wheel(wheel, artifact)

    write_pkg_wheel('Name: typing-extensions\nVersion: 4.14.0\n', 'Tag: py3-none-any\n')
    with pytest.raises(prep.RuntimePrepError, match='metadata mismatch'):
        prep.validate_python_package_wheel(wheel, artifact)

    write_pkg_wheel('Name: typing-extensions\nVersion: 4.15.0\n', 'Tag: py3-none-win_arm64\n')
    with pytest.raises(prep.RuntimePrepError, match='win_amd64 or none-any'):
        prep.validate_python_package_wheel(wheel, artifact)


def test_write_provenance_records_windows_x86_64_runtime_contract(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    m = manifest(required_native_dlls=['llama.dll', 'python311.dll'])

    prep.write_provenance(runtime, m)

    payload = json.loads((runtime / prep.PROVENANCE).read_text(encoding='utf-8'))
    assert payload['runtime_id'] == 'bundled-cpython-3.11-win-x86_64-cu124'
    assert payload['target_triple'] == 'x86_64-pc-windows-msvc'
    assert payload['llama_cpp_cuda_wheel']['name'].endswith('win_amd64.whl')
    assert payload['expected_backend'] == 'cuda'
    assert payload['required_native_dlls'] == ['llama.dll', 'python311.dll']

def test_fetch_downloads_missing_github_artifact_and_safe_extracts_file(tmp_path, monkeypatch):
    payload = b'downloaded-runtime'
    digest = prep.hashlib.sha256(payload).hexdigest()

    class Response:
        def __enter__(self):
            return io.BytesIO(payload)
        def __exit__(self, *args):
            return False

    monkeypatch.setattr(prep.urllib.request, 'urlopen', lambda url, timeout: Response())

    dest = tmp_path / 'cache' / 'runtime.tar.gz'
    assert prep.fetch('https://github.com/example/runtime.tar.gz', digest, dest) == dest
    assert dest.read_bytes() == payload

    archive = tmp_path / 'good.tar.gz'
    with tarfile.open(archive, 'w:gz') as tf:
        data = b'ok'
        info = tarfile.TarInfo('python/python.exe')
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))

    extract_to = tmp_path / 'extract'
    prep.safe_extract_tar(archive, extract_to)
    assert (extract_to / 'python' / 'python.exe').read_bytes() == data


def test_safe_extract_tar_rejects_device_members(tmp_path):
    archive = tmp_path / 'device.tar.gz'
    with tarfile.open(archive, 'w:gz') as tf:
        info = tarfile.TarInfo('python/device')
        info.type = tarfile.CHRTYPE
        tf.addfile(info)

    with pytest.raises(prep.RuntimePrepError, match='devices are not allowed'):
        prep.safe_extract_tar(archive, tmp_path / 'dest')


def test_validate_wheel_rejects_wrong_filename_and_missing_metadata(tmp_path):
    wrong = tmp_path / 'llama_cpp_python-0.3.32-py3-none-win_arm64.whl'
    _write_wheel(wrong, metadata='Name: llama-cpp-python\nVersion: 0.3.32\n', wheel_text='Tag: py3-none-win_amd64\n')
    with pytest.raises(prep.RuntimePrepError, match='wrong wheel filename'):
        prep.validate_wheel(wrong, manifest())

    missing_meta = tmp_path / manifest()['llama_cpp_cuda_wheel']['name']
    import zipfile
    with zipfile.ZipFile(missing_meta, 'w') as zf:
        zf.writestr('llama_cpp_python-0.3.32.dist-info/WHEEL', 'Tag: py3-none-win_amd64\n')
        zf.writestr('llama_cpp/lib/llama.dll', b'dll')
    with pytest.raises(prep.RuntimePrepError, match='missing METADATA'):
        prep.validate_wheel(missing_meta, manifest())


def test_run_delegates_to_subprocess_with_fail_closed_defaults(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return 'completed'

    monkeypatch.setattr(prep.subprocess, 'run', fake_run)

    assert prep.run(['python.exe', '-V'], cwd='runtime') == 'completed'
    assert calls == [(['python.exe', '-V'], {'check': True, 'text': True, 'capture_output': True, 'cwd': 'runtime'})]


def test_prepare_error_paths_for_missing_python_probe_mismatch_and_missing_dll(tmp_path, monkeypatch):
    runtime_root = tmp_path / 'desktop-tauri' / 'src-tauri' / 'python-runtime'
    runtime_root.parent.mkdir(parents=True)
    monkeypatch.setattr(prep, 'ROOT', tmp_path / 'desktop-tauri')
    monkeypatch.setattr(prep, 'SRC_TAURI', tmp_path / 'desktop-tauri' / 'src-tauri')
    monkeypatch.setattr(prep, 'OUTPUT', runtime_root)
    monkeypatch.setattr(prep.platform, 'machine', lambda: 'AMD64')
    monkeypatch.setattr(prep, 'fetch', lambda url, sha, dest: tmp_path / ('wheel.whl' if url.endswith('.whl') else 'runtime.tar.gz'))
    monkeypatch.setattr(prep, 'validate_wheel', lambda whl, data: None)

    def no_native(data):
        data = dict(data)
        data['native_dll_artifacts'] = []
        return data

    def fake_extract_without_python(_archive, dest):
        (dest / 'cpython').mkdir(parents=True)

    monkeypatch.setattr(prep, 'safe_extract_tar', fake_extract_without_python)
    with pytest.raises(prep.RuntimePrepError, match='archive missing python.exe'):
        prep.prepare(no_native(manifest(expected_archive_root='cpython', required_packages={'llama-cpp-python': '0.3.32'})))

    def fake_extract_with_python(_archive, dest):
        staged = dest / 'cpython'
        staged.mkdir(parents=True)
        (staged / 'python.exe').write_text('', encoding='utf-8')

    class Result:
        stdout = json.dumps({'version': [3, 10], 'machine': 'AMD64'})

    monkeypatch.setattr(prep, 'safe_extract_tar', fake_extract_with_python)
    monkeypatch.setattr(prep, 'run', lambda cmd, **kwargs: Result())
    with pytest.raises(prep.RuntimePrepError, match='interpreter probe mismatch'):
        prep.prepare(no_native(manifest(expected_archive_root='cpython', required_packages={'llama-cpp-python': '0.3.32'})))

    class GoodProbe:
        stdout = json.dumps({'version': [3, 11, 13], 'machine': 'AMD64'})

    def fake_good_run(cmd, **kwargs):
        if 'importlib.metadata' in cmd[-1]:
            class Inventory:
                stdout = json.dumps({'llama-cpp-python': '0.3.32'})
            return Inventory()
        return GoodProbe()

    monkeypatch.setattr(prep, 'run', fake_good_run)
    with pytest.raises(prep.RuntimePrepError, match='missing required DLL'):
        prep.prepare(no_native(manifest(expected_archive_root='cpython', required_packages={'llama-cpp-python': '0.3.32'}, required_native_dlls=['llama.dll'])))


def test_main_check_manifest_only_success_and_error(tmp_path, monkeypatch, capsys):
    good = tmp_path / 'manifest.json'
    write_manifest(good, manifest())
    monkeypatch.setattr(prep.sys, 'argv', ['prepare', '--manifest', str(good), '--check-manifest-only'])
    assert prep.main() == 0

    bad = tmp_path / 'bad.json'
    write_manifest(bad, manifest(schema_version=99))
    monkeypatch.setattr(prep.sys, 'argv', ['prepare', '--manifest', str(bad), '--check-manifest-only'])
    assert prep.main() == 1
    assert 'windows embedded runtime preparation failed' in capsys.readouterr().err


def test_production_manifest_includes_llama_direct_dependency_closure(tmp_path):
    m = prep.load_manifest(prep.MANIFEST)
    required = m['required_packages']
    wheels = {artifact['package']: artifact for artifact in m['python_package_wheels']}

    typing_wheel = wheels['typing-extensions']
    assert required['typing-extensions'] == '4.15.0'
    assert typing_wheel == {
        'package': 'typing-extensions',
        'version': '4.15.0',
        'filename': 'typing_extensions-4.15.0-py3-none-any.whl',
        'url': 'https://files.pythonhosted.org/packages/18/67/36e9267722cc04a6b9f15c7f3441c2363321a3ea07da7ae0c0707beb2a9c/typing_extensions-4.15.0-py3-none-any.whl',
        'sha256': 'f0fa19c6845758ab08074a0cfa8b7aecb71c999ca73d62883bc25cc018c4e548',
    }

    requirements = tmp_path / 'requirements.txt'
    lines = prep.write_hash_requirements(requirements, m)
    assert 'typing-extensions==4.15.0 --hash=sha256:f0fa19c6845758ab08074a0cfa8b7aecb71c999ca73d62883bc25cc018c4e548' in lines

    llama_direct_dependencies = {'diskcache', 'Jinja2', 'numpy', 'typing-extensions'}
    assert llama_direct_dependencies <= set(required)
    assert llama_direct_dependencies <= set(wheels)


def test_validate_installed_inventory_rejects_missing_extra_and_runs_pip_check(monkeypatch, tmp_path):
    py = tmp_path / 'python.exe'
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Result:
            stdout = json.dumps({'alpha': '1.0', 'llama-cpp-python': '0.3.32'})
        return Result()

    monkeypatch.setattr(prep, 'run', fake_run)
    prep.validate_installed_inventory(py, {
        'required_packages': {'alpha': '1.0', 'llama-cpp-python': '0.3.32'},
    })
    assert calls[-1][1:3] == ['-m', 'pip']
    assert 'check' in calls[-1]

    def missing_run(cmd, **kwargs):
        class Result:
            stdout = json.dumps({'alpha': '2.0'})
        return Result()

    monkeypatch.setattr(prep, 'run', missing_run)
    with pytest.raises(prep.RuntimePrepError, match='inventory mismatch'):
        prep.validate_installed_inventory(py, {'required_packages': {'alpha': '1.0'}})

    def extra_run(cmd, **kwargs):
        class Result:
            stdout = json.dumps({'alpha': '1.0', 'surprise': '9.9'})
        return Result()

    monkeypatch.setattr(prep, 'run', extra_run)
    with pytest.raises(prep.RuntimePrepError, match='unexpected installed packages'):
        prep.validate_installed_inventory(py, {'required_packages': {'alpha': '1.0'}})


def test_validate_runtime_payload_rejects_missing_dll_and_toolkit_source(tmp_path):
    runtime = tmp_path / 'python-runtime'
    runtime.mkdir()
    write_minimal_pe(runtime / 'python311.dll')
    prep.validate_runtime_payload(runtime, {'required_native_dlls': ['python311.dll'], 'pe_dll_closure': [{'name': 'python311.dll', 'machine': 'IMAGE_FILE_MACHINE_AMD64'}]})

    with pytest.raises(prep.RuntimePrepError, match='missing required DLL'):
        prep.validate_runtime_payload(runtime, {'required_native_dlls': ['llama.dll']})

    write_minimal_pe(runtime / 'nvcc.exe')
    with pytest.raises(prep.RuntimePrepError, match='forbidden compiler/toolkit/source payload'):
        prep.validate_runtime_payload(runtime, {'required_native_dlls': ['python311.dll'], 'pe_dll_closure': [{'name': 'python311.dll', 'machine': 'IMAGE_FILE_MACHINE_AMD64'}]})



def test_validate_runtime_payload_resolves_recursive_pe_imports(tmp_path):
    runtime = tmp_path / 'python-runtime'
    runtime.mkdir()
    write_minimal_pe(runtime / 'python.exe', imports=['python311.dll'])
    write_minimal_pe(runtime / 'python311.dll', imports=['kernel32.dll'])
    closure = prep.validate_runtime_payload(runtime, {
        'required_native_dlls': ['python311.dll'],
        'pe_dll_closure': [{'name': 'python311.dll', 'machine': 'IMAGE_FILE_MACHINE_AMD64'}],
    })
    assert {entry['name'] for entry in closure} >= {'python.exe', 'python311.dll'}

    write_minimal_pe(runtime / 'bad.pyd', imports=['missing_vendor.dll'])
    with pytest.raises(prep.RuntimePrepError, match='unresolved non-system DLL import'):
        prep.validate_runtime_payload(runtime, {'required_native_dlls': ['python311.dll']})

    (runtime / 'bad.pyd').unlink()
    write_minimal_pe(runtime / 'arm64.dll', machine=0xAA64)
    with pytest.raises(prep.RuntimePrepError, match='ARM64 PE payload rejected'):
        prep.validate_runtime_payload(runtime, {'required_native_dlls': ['python311.dll']})


def test_api_set_and_netapi_imports_are_os_provided_but_app_dependencies_must_bundle(tmp_path):
    runtime = tmp_path / 'python-runtime'
    runtime.mkdir()
    write_minimal_pe(runtime / 'python.exe', imports=['api-ms-win-core-path-l1-1-0.dll', 'netapi32.dll', 'pdh.dll', 'powrprof.dll', 'psapi.dll', 'userenv.dll'])
    write_minimal_pe(runtime / 'python311.dll')

    closure = prep.validate_runtime_payload(runtime, {
        'required_native_dlls': ['python311.dll'],
        'pe_dll_closure': [{'name': 'python311.dll', 'machine': 'IMAGE_FILE_MACHINE_AMD64'}],
    })
    assert {entry['name'] for entry in closure} >= {'python.exe', 'python311.dll'}

    for dll in ('bcryptprimitives.dll', 'cabinet.dll', 'comctl32.dll', 'comdlg32.dll', 'imm32.dll', 'iphlpapi.dll', 'msi.dll', 'netapi32.dll', 'pdh.dll', 'powrprof.dll', 'psapi.dll', 'userenv.dll'):
        assert prep.is_windows_system_dll(dll)
    assert prep.is_windows_system_dll('nvcuda.dll')
    for dll in ('cudart64_12.dll', 'cublas64_12.dll', 'libssl-3-x64.dll', 'vcruntime140.dll'):
        assert not prep.is_windows_system_dll(dll)

    write_minimal_pe(runtime / 'vendor.pyd', imports=['cudart64_12.dll'])
    with pytest.raises(prep.RuntimePrepError, match='unresolved non-system DLL imports: .*cudart64_12.dll'):
        prep.validate_runtime_payload(runtime, {'required_native_dlls': ['python311.dll']})



def test_prunes_only_known_distlib_non_x64_launchers_before_pe_validation(tmp_path):
    runtime = tmp_path / 'python-runtime'
    distlib = runtime / 'Lib' / 'site-packages' / 'pip' / '_vendor' / 'distlib'
    distlib.mkdir(parents=True)
    for name in ('t32.exe', 'w32.exe', 't64-arm.exe', 'w64-arm.exe'):
        write_minimal_pe(distlib / name, machine=0x014C if name[1:3] == '32' else 0xAA64)
    write_minimal_pe(distlib / 't64.exe')
    write_minimal_pe(distlib / 'w64.exe')

    removed = prep.prune_distlib_unused_non_x64_launchers(runtime)

    assert removed == [
        'Lib/site-packages/pip/_vendor/distlib/t32.exe',
        'Lib/site-packages/pip/_vendor/distlib/t64-arm.exe',
        'Lib/site-packages/pip/_vendor/distlib/w32.exe',
        'Lib/site-packages/pip/_vendor/distlib/w64-arm.exe',
    ]
    assert (distlib / 't64.exe').exists()
    assert (distlib / 'w64.exe').exists()
    prep.validate_runtime_payload(runtime, {'required_native_dlls': []})


def test_unexpected_wrong_architecture_pe_fails_with_relative_path(tmp_path):
    runtime = tmp_path / 'python-runtime'
    nested = runtime / 'Lib' / 'site-packages'
    nested.mkdir(parents=True)
    write_minimal_pe(nested / 'unexpected.exe', machine=0x014C)

    with pytest.raises(prep.RuntimePrepError, match=r'x86 PE payload rejected: Lib/site-packages/unexpected.exe'):
        prep.validate_runtime_payload(runtime, {'required_native_dlls': []})


def test_delay_load_imports_are_included_in_pe_closure(tmp_path):
    runtime = tmp_path / 'python-runtime'
    runtime.mkdir()
    write_minimal_pe(runtime / 'python.exe', delay_imports=['delayed.dll'])
    write_minimal_pe(runtime / 'delayed.dll')

    closure = prep.validate_runtime_payload(runtime, {'required_native_dlls': ['delayed.dll']})

    assert {entry['name'] for entry in closure} >= {'python.exe', 'delayed.dll'}

def test_prepare_restores_previous_runtime_when_promotion_fails(tmp_path, monkeypatch):
    runtime_root = tmp_path / 'desktop-tauri' / 'src-tauri' / 'python-runtime'
    runtime_root.mkdir(parents=True)
    marker = runtime_root / 'previous.txt'
    marker.write_text('keep', encoding='utf-8')
    m = manifest(expected_archive_root='cpython', required_packages={'llama-cpp-python': '0.3.32'})
    m['native_dll_artifacts'] = []
    monkeypatch.setattr(prep, 'ROOT', tmp_path / 'desktop-tauri')
    monkeypatch.setattr(prep, 'SRC_TAURI', tmp_path / 'desktop-tauri' / 'src-tauri')
    monkeypatch.setattr(prep, 'OUTPUT', runtime_root)
    monkeypatch.setattr(prep.platform, 'machine', lambda: 'AMD64')
    monkeypatch.setattr(prep, 'fetch', lambda url, sha, dest: tmp_path / ('wheel.whl' if url.endswith('.whl') else 'runtime.tar.gz'))
    monkeypatch.setattr(prep, 'validate_wheel', lambda whl, data: None)
    monkeypatch.setattr(prep, 'validate_installed_inventory', lambda py, data: None)
    monkeypatch.setattr(prep, 'validate_runtime_payload', lambda runtime, data: [])

    def fake_extract(_archive, dest):
        staged = dest / 'cpython'
        staged.mkdir(parents=True)
        (staged / 'python.exe').write_text('', encoding='utf-8')

    class Probe:
        stdout = json.dumps({'version': [3, 11, 13], 'machine': 'AMD64'})

    original_rename = Path.rename

    def flaky_rename(self, target):
        if self.name == 'python-runtime' and Path(target) == runtime_root:
            raise OSError('final rename failed')
        return original_rename(self, target)

    monkeypatch.setattr(prep, 'safe_extract_tar', fake_extract)
    monkeypatch.setattr(prep, 'run', lambda cmd, **kwargs: Probe())
    monkeypatch.setattr(Path, 'rename', flaky_rename)

    with pytest.raises(OSError, match='final rename failed'):
        prep.prepare(m)
    assert marker.read_text(encoding='utf-8') == 'keep'


def test_validate_runtime_payload_reports_sorted_bounded_unresolved_import_set(tmp_path):
    runtime = tmp_path / 'python-runtime'
    runtime.mkdir()
    write_minimal_pe(runtime / 'python.exe', imports=['zvendor.dll', 'avendor.dll', 'kernel32.dll'])

    with pytest.raises(prep.RuntimePrepError) as excinfo:
        prep.validate_runtime_payload(runtime, {'required_native_dlls': []})

    message = str(excinfo.value)
    assert 'unresolved non-system DLL imports:' in message
    assert message.index('avendor.dll') < message.index('zvendor.dll')
    assert 'kernel32.dll' not in message


def test_duplicate_non_pe_basenames_are_allowed_but_differing_duplicate_dlls_fail(tmp_path):
    runtime = tmp_path / 'python-runtime'
    (runtime / 'include').mkdir(parents=True)
    (runtime / 'Lib' / 'site-packages' / 'pkg').mkdir(parents=True)
    (runtime / 'include' / 'abstract.h').write_text('cpython', encoding='utf-8')
    (runtime / 'Lib' / 'site-packages' / 'pkg' / 'abstract.h').write_text('numpy', encoding='utf-8')
    write_minimal_pe(runtime / 'python.exe', imports=['python311.dll'])
    write_minimal_pe(runtime / 'python311.dll')

    prep.validate_runtime_payload(runtime, {'required_native_dlls': ['python311.dll']})

    (runtime / 'a').mkdir()
    (runtime / 'b').mkdir()
    write_minimal_pe(runtime / 'a' / 'vendor.dll')
    write_minimal_pe(runtime / 'b' / 'vendor.dll', imports=['kernel32.dll'])
    write_minimal_pe(runtime / 'uses_vendor.pyd', imports=['vendor.dll'])
    with pytest.raises(prep.RuntimePrepError, match='ambiguous duplicate DLL basename: vendor.dll'):
        prep.validate_runtime_payload(runtime, {'required_native_dlls': ['python311.dll']})


def test_identical_duplicate_dlls_resolve_deterministically(tmp_path):
    runtime = tmp_path / 'python-runtime'
    (runtime / 'a').mkdir(parents=True)
    (runtime / 'b').mkdir(parents=True)
    write_minimal_pe(runtime / 'python.exe', imports=['vendor.dll'])
    write_minimal_pe(runtime / 'a' / 'vendor.dll')
    (runtime / 'b' / 'vendor.dll').write_bytes((runtime / 'a' / 'vendor.dll').read_bytes())

    closure = prep.validate_runtime_payload(runtime, {'required_native_dlls': ['vendor.dll']})

    assert {entry['name'] for entry in closure} >= {'python.exe', 'vendor.dll'}


def test_distlib_launcher_pruning_is_restricted_to_distlib_resources(tmp_path):
    runtime = tmp_path / 'python-runtime'
    distlib = runtime / 'Lib' / 'site-packages' / 'pip' / '_vendor' / 'distlib'
    distlib.mkdir(parents=True)
    write_minimal_pe(distlib / 't32.exe', machine=0x014C)
    write_minimal_pe(distlib / 'w64-arm.exe', machine=0xAA64)
    unrelated = runtime / 'tools'
    unrelated.mkdir()
    write_minimal_pe(unrelated / 't32.exe', machine=0x014C)

    removed = prep.prune_distlib_unused_non_x64_launchers(runtime)

    assert removed == [
        'Lib/site-packages/pip/_vendor/distlib/t32.exe',
        'Lib/site-packages/pip/_vendor/distlib/w64-arm.exe',
    ]
    assert (unrelated / 't32.exe').exists()
    with pytest.raises(prep.RuntimePrepError, match='x86 PE payload rejected: tools/t32.exe'):
        prep.validate_runtime_payload(runtime, {'required_native_dlls': []})

def test_prunes_known_setuptools_non_x64_launchers_but_preserves_x64(tmp_path):
    runtime = tmp_path / 'python-runtime'
    setuptools = runtime / 'Lib' / 'site-packages' / 'setuptools'
    setuptools.mkdir(parents=True)
    for name in ('cli.exe', 'cli-32.exe', 'gui-32.exe'):
        write_minimal_pe(setuptools / name, machine=0x014C)
    for name in ('gui.exe', 'cli-arm64.exe', 'gui-arm64.exe'):
        write_minimal_pe(setuptools / name, machine=0xAA64)
    write_minimal_pe(setuptools / 'cli-64.exe')
    write_minimal_pe(setuptools / 'gui-64.exe')

    removed = prep.prune_packaging_unused_non_x64_launchers(runtime)

    assert removed == [
        'Lib/site-packages/setuptools/cli-32.exe',
        'Lib/site-packages/setuptools/cli-arm64.exe',
        'Lib/site-packages/setuptools/cli.exe',
        'Lib/site-packages/setuptools/gui-32.exe',
        'Lib/site-packages/setuptools/gui-arm64.exe',
        'Lib/site-packages/setuptools/gui.exe',
    ]
    assert (setuptools / 'cli-64.exe').exists()
    assert (setuptools / 'gui-64.exe').exists()
    prep.validate_runtime_payload(runtime, {'required_native_dlls': []})


def test_setuptools_launcher_names_outside_setuptools_still_fail(tmp_path):
    runtime = tmp_path / 'python-runtime'
    tools = runtime / 'tools'
    tools.mkdir(parents=True)
    write_minimal_pe(tools / 'cli.exe', machine=0x014C)

    assert prep.prune_packaging_unused_non_x64_launchers(runtime) == []
    with pytest.raises(prep.RuntimePrepError, match='x86 PE payload rejected: tools/cli.exe'):
        prep.validate_runtime_payload(runtime, {'required_native_dlls': []})


def test_manifest_pins_native_vendor_runtime_dll_artifacts():
    m = prep.load_manifest()
    required = {'cudart64_12.dll', 'cublas64_12.dll', 'msvcp140.dll', 'vcomp140.dll'}
    assert required.issubset({dll.lower() for dll in m['required_native_dlls']})
    artifacts = {artifact['name'].lower(): artifact for artifact in m['native_dll_artifacts']}
    assert required.issubset(artifacts)
    assert artifacts['cudart64_12.dll']['flavor'] == 'cuda-12.4-runtime'
    assert artifacts['cublas64_12.dll']['flavor'] == 'cuda-12.4-cublas'
    assert artifacts['msvcp140.dll']['architecture'] == 'AMD64'
    for name in required:
        assert prep.SHA256_RE.fullmatch(artifacts[name]['sha256'])
        assert prep.SHA256_RE.fullmatch(artifacts[name]['extracted_sha256'])
        assert artifacts[name]['archive_member_path']
        assert artifacts[name]['url'].endswith('.zip')
        assert artifacts[name]['url'].startswith(('https://developer.download.nvidia.com/', 'https://download.visualstudio.microsoft.com/'))
    assert artifacts['cudart64_12.dll']['version'] == '12.4.127'
    assert artifacts['cublas64_12.dll']['version'] == '12.4.5.8'
    assert artifacts['cublaslt64_12.dll']['version'] == '12.4.5.8'


def test_manifest_rejects_native_artifact_missing_exact_member_or_hash(tmp_path):
    m = prep.load_manifest()
    del m['native_dll_artifacts'][0]['archive_member_path']
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(m), encoding='utf-8')
    with pytest.raises(prep.RuntimePrepError, match='archive_member_path'):
        prep.load_manifest(path)

    m = prep.load_manifest()
    del m['native_dll_artifacts'][0]['extracted_sha256']
    path.write_text(json.dumps(m), encoding='utf-8')
    with pytest.raises(prep.RuntimePrepError, match='extracted_sha256'):
        prep.load_manifest(path)


def test_manifest_rejects_native_artifact_non_zip_and_coarse_cuda_version(tmp_path):
    m = prep.load_manifest()
    m['native_dll_artifacts'][0]['url'] = 'https://developer.download.nvidia.com/example/cuda.exe'
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(m), encoding='utf-8')
    with pytest.raises(prep.RuntimePrepError, match='zip archives'):
        prep.load_manifest(path)

    m = prep.load_manifest()
    m['native_dll_artifacts'][0]['version'] = '12.4'
    path.write_text(json.dumps(m), encoding='utf-8')
    with pytest.raises(prep.RuntimePrepError, match='12.4.127'):
        prep.load_manifest(path)


def test_manifest_rejects_missing_native_vendor_artifact_pin(tmp_path):
    m = prep.load_manifest()
    m['native_dll_artifacts'] = [
        artifact for artifact in m['native_dll_artifacts']
        if artifact['name'].lower() != 'cudart64_12.dll'
    ]
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(m), encoding='utf-8')

    with pytest.raises(prep.RuntimePrepError, match='missing native DLL artifact pins'):
        prep.load_manifest(path)


def test_stage_native_dll_artifacts_extracts_exact_pinned_member(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    member = 'vendor/bin/cudart64_12.dll'
    dll_bytes = bytearray(0x600)
    dll = tmp_path / 'cudart64_12.dll'
    write_minimal_pe(dll)
    dll_bytes = dll.read_bytes()
    archive = tmp_path / 'cuda.zip'
    import zipfile
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr(member, dll_bytes)
    digest = prep.sha256_file(archive)
    file_digest = prep.sha256_file(dll)
    m = {'native_dll_artifacts': [{
        'name': 'cudart64_12.dll',
        'version': '12.4.127',
        'url': 'https://developer.download.nvidia.com/example/cuda.zip',
        'sha256': digest,
        'architecture': 'AMD64',
        'license': 'NVIDIA CUDA Toolkit EULA',
        'provenance': 'test pinned runtime DLL',
        'archive_member_path': member,
        'destination': 'cudart64_12.dll',
        'extracted_sha256': file_digest,
    }]}
    monkeypatch.setattr(prep, 'fetch', lambda url, sha, dest: archive)

    prep.stage_native_dll_artifacts(m, tmp_path / 'cache', runtime)

    assert (runtime / 'cudart64_12.dll').read_bytes() == dll_bytes


def test_manifest_rejects_non_exact_native_versions(tmp_path):
    m = prep.load_manifest()
    m['native_dll_artifacts'][0]['version'] = '12.x'
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(m), encoding='utf-8')

    with pytest.raises(prep.RuntimePrepError, match='versions must be exact'):
        prep.load_manifest(path)
