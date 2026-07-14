import hashlib
import importlib.util
import io
import json
import subprocess
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
        'qwen_64k_yarn_support': 'supported',
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
        'qwen_64k_yarn_support': 'supported',
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


def test_runtime_capability_probe_requires_qwen_64k_yarn_support():
    payload = {
        'qwen_64k_yarn_support': 'unsupported',
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

    assert prep._missing_runtime_capabilities(payload) == ['qwen_64k_yarn_support']


def test_install_packages_uses_shared_root_llama_cpp_pin_for_metal_plan(tmp_path, monkeypatch):
    commands = []

    class Plan:
        package_spec = 'llama-cpp-python==0.3.32'
        backend = 'metal'
        only_binary = True

        def pip_install_args(self):
            return ['--only-binary', 'llama-cpp-python']

        def pip_env(self):
            return {'CMAKE_ARGS': '-DGGML_METAL=on'}

    def fake_fallbacks(*, platform, requirements_path):
        assert platform == 'darwin'
        assert requirements_path == prep.ROOT.parent / 'requirements.txt'
        return [Plan()]

    def fake_run(cmd, **kwargs):
        commands.append((cmd, kwargs.get('env') or {}))
        class Result:
            stdout = ''
        return Result()

    monkeypatch.setattr(prep, 'llama_cpp_install_plan_fallbacks', fake_fallbacks)
    monkeypatch.setattr(prep, 'run', fake_run)
    monkeypatch.setattr(prep, '_validate_candidate_install', lambda py, m, runtime: None)

    prep.install_packages(tmp_path / 'python3', manifest(), tmp_path / 'pip-cache')

    # commands[0]: ensurepip, commands[1]: pip upgrade
    # commands[2]: install non-llama packages (from -r requirements + pinned_packages)
    # commands[3]: install llama-cpp-python with Metal plan
    non_llama_cmd = commands[2][0]
    assert non_llama_cmd[:4] == [str(tmp_path / 'python3'), '-m', 'pip', 'install']
    assert str(prep.SRC_TAURI / 'python' / 'requirements_desktop_runtime.txt') in non_llama_cmd
    assert 'numpy==2.3.3' in non_llama_cmd
    assert 'diskcache==5.6.3' in non_llama_cmd
    assert 'llama-cpp-python==0.3.32' not in non_llama_cmd

    llama_cmd = commands[3][0]
    llama_env = commands[3][1]
    assert llama_cmd[:4] == [str(tmp_path / 'python3'), '-m', 'pip', 'install']
    assert 'llama-cpp-python==0.3.32' == llama_cmd[-1]
    assert str(prep.SRC_TAURI / 'python' / 'requirements_desktop_runtime.txt') not in llama_cmd
    assert llama_env['CMAKE_ARGS'] == '-DGGML_METAL=on'


def test_install_packages_falls_back_to_source_metal_build(tmp_path, monkeypatch):
    """When the prebuilt Metal wheel fails, the source Metal build plan is tried."""
    commands = []
    attempt_counts = []

    class WheelPlan:
        package_spec = 'llama-cpp-python==0.3.32'
        backend = 'metal'
        only_binary = True

        def pip_install_args(self):
            return ['--only-binary', 'llama-cpp-python']

        def pip_env(self):
            return {}

    class SourcePlan:
        package_spec = 'llama-cpp-python==0.3.32'
        backend = 'metal'
        only_binary = False
        force_cmake = True

        def pip_install_args(self):
            return ['--no-binary', 'llama-cpp-python']

        def pip_env(self):
            return {'CMAKE_ARGS': '-DGGML_METAL=on', 'FORCE_CMAKE': '1'}

    def fake_fallbacks(*, platform, requirements_path):
        return [WheelPlan(), SourcePlan()]

    def fake_run(cmd, **kwargs):
        if 'llama-cpp-python==0.3.32' in cmd and '--only-binary' in cmd:
            import subprocess as _sp
            raise _sp.CalledProcessError(2, cmd, '', 'no matching distribution')
        commands.append((cmd, kwargs.get('env') or {}))
        class Result:
            stdout = ''
        return Result()

    monkeypatch.setattr(prep, 'llama_cpp_install_plan_fallbacks', fake_fallbacks)
    monkeypatch.setattr(prep, 'run', fake_run)
    monkeypatch.setattr(prep, '_validate_candidate_install', lambda py, m, runtime: None)

    prep.install_packages(tmp_path / 'python3', manifest(), tmp_path / 'pip-cache')

    # The source Metal build plan command should be present.
    source_cmd, source_env = next((cmd, env) for cmd, env in commands if '--no-binary' in cmd)
    assert 'llama-cpp-python==0.3.32' == source_cmd[-1]
    assert source_env['CMAKE_ARGS'] == '-DGGML_METAL=on'
    assert source_env['CMAKE_OSX_ARCHITECTURES'] == 'arm64'
    assert source_env['CMAKE_OSX_DEPLOYMENT_TARGET'] == '12.0'
    assert source_env['MACOSX_DEPLOYMENT_TARGET'] == '12.0'


def test_clean_preserves_pip_internal_build_package(tmp_path):
    runtime = tmp_path / 'python-runtime'
    pip_build = runtime / 'lib' / 'python3.11' / 'site-packages' / 'pip' / '_internal' / 'operations' / 'build'
    pip_build.mkdir(parents=True)
    (pip_build / '__init__.py').write_text('# required pip module\n')
    test_dir = runtime / 'lib' / 'python3.11' / 'site-packages' / 'somepkg' / 'tests'
    test_dir.mkdir(parents=True)
    (test_dir / 'test_sample.py').write_text('def test_sample(): pass\n')
    pycache = runtime / 'lib' / 'python3.11' / 'site-packages' / 'somepkg' / '__pycache__'
    pycache.mkdir(parents=True)
    (pycache / 'module.pyc').write_bytes(b'cache')

    prep.clean(runtime)

    assert (pip_build / '__init__.py').is_file()
    assert not test_dir.exists()
    assert not pycache.exists()


def test_load_manifest_rejects_latest_url_uppercase_sha_and_package_drift(tmp_path):
    p = tmp_path / 'm.json'
    cases = [
        (manifest(archive_url='https://example.test/latest/runtime.tar.gz'), 'latest'),
        (manifest(sha256='A' * 64), 'lowercase'),
        (manifest(required_packages={**manifest()['required_packages'], 'numpy': '9.9.9'}), 'required_packages'),
        ({k: v for k, v in manifest().items() if k != 'runtime_notices'}, 'missing manifest field'),
        (manifest(expected_packaged_runtime_path='Contents/Resources/python-runtime'), 'packaged runtime path'),
    ]
    for data, expected in cases:
        p.write_text(json.dumps(data), encoding='utf-8')
        try:
            prep.load_manifest(p)
            assert False, f'expected failure containing {expected}'
        except prep.RuntimePrepError as exc:
            assert expected in str(exc)


def test_validate_tar_member_rejects_escaping_symlink_and_hardlink():
    for link_type in (tarfile.SYMTYPE, tarfile.LNKTYPE):
        info = tarfile.TarInfo('python/bin/python3')
        info.type = link_type
        info.linkname = '../outside'
        try:
            prep.validate_tar_member(info, 'python')
            assert False
        except prep.RuntimePrepError as exc:
            assert 'escapes extraction root' in str(exc)


def test_prove_interpreter_rejects_wrong_version_architecture_and_paths(tmp_path, monkeypatch):
    py = tmp_path / 'runtime' / 'bin' / 'python3'
    py.parent.mkdir(parents=True)
    py.write_text('#!/bin/sh\n', encoding='utf-8')
    runtime = tmp_path / 'runtime'

    class Result:
        def __init__(self, payload):
            self.stdout = json.dumps(payload)

    payloads = [
        {'version': [2, 7], 'machine': 'arm64', 'executable': str(py), 'prefix': str(runtime)},
        {'version': [3, 11], 'machine': 'x86_64', 'executable': str(py), 'prefix': str(runtime)},
        {'version': [3, 11], 'machine': 'arm64', 'executable': '/tmp/outside/python3', 'prefix': str(runtime)},
        {'version': [3, 11], 'machine': 'arm64', 'executable': str(py), 'prefix': '/tmp/outside'},
    ]
    expected = ['not Python 3.11', 'wrong architecture', 'executable is outside', 'prefix is outside']
    for payload, message in zip(payloads, expected, strict=True):
        monkeypatch.setattr(prep, 'run', lambda *_, _payload=payload, **__: Result(_payload))
        try:
            prep.prove_interpreter(py, runtime, manifest())
            assert False
        except prep.RuntimePrepError as exc:
            assert message in str(exc)


def test_probe_runtime_rejects_backend_version_and_missing_capabilities(tmp_path, monkeypatch):
    py = tmp_path / 'python3'

    class Result:
        def __init__(self, payload):
            self.stdout = json.dumps(payload)

    valid_payload = {
        'backend': 'metal',
        'gpu_offload_supported': True,
        'llama_cpp_python_version': '0.3.32',
        'qwen_64k_yarn_support': 'supported',
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
    cases = [
        ({**valid_payload, 'backend': 'cpu'}, 'not Metal-capable'),
        ({**valid_payload, 'gpu_offload_supported': False}, 'not Metal-capable'),
        ({**valid_payload, 'llama_cpp_python_version': '0.0.1'}, 'wrong llama-cpp-python version'),
        ({**valid_payload, 'qwen_64k_yarn_support': 'unsupported'}, 'missing Qwen 64K runtime capabilities'),
    ]
    for payload, message in cases:
        monkeypatch.setattr(prep, 'run', lambda *_, _payload=payload, **__: Result(_payload))
        try:
            prep.probe_runtime(py, manifest())
            assert False
        except prep.RuntimePrepError as exc:
            assert message in str(exc)
    monkeypatch.setattr(prep, 'run', lambda *_, **__: Result(valid_payload))
    assert prep.probe_runtime(py, manifest()) == valid_payload


def test_existing_valid_rejects_missing_or_damaged_provenance(tmp_path, monkeypatch):
    output = tmp_path / 'python-runtime'
    monkeypatch.setattr(prep, 'OUTPUT', output)
    assert prep.existing_valid(manifest()) is False
    output.mkdir()
    (output / prep.PROVENANCE).write_text(json.dumps({'source_archive_sha256': 'bad', 'expected_backend': 'metal'}), encoding='utf-8')
    (output / 'bin').mkdir()
    (output / 'bin' / 'python3').write_text('#!/bin/sh\n', encoding='utf-8')
    assert prep.existing_valid(manifest()) is False


def test_existing_valid_accepts_matching_provenance_after_full_validation(tmp_path, monkeypatch):
    output = tmp_path / 'python-runtime'
    (output / 'bin').mkdir(parents=True)
    (output / 'bin' / 'python3').write_text('#!/bin/sh\n', encoding='utf-8')
    packages = manifest()['required_packages']
    (output / prep.PROVENANCE).write_text(json.dumps({
        'source_archive_sha256': '0' * 64,
        'expected_backend': 'metal',
        'installed_packages': packages,
        'build_profile': prep.BUILD_PROFILE,
    }), encoding='utf-8')
    monkeypatch.setattr(prep, 'OUTPUT', output)
    monkeypatch.setattr(prep, 'prove_interpreter', lambda py, runtime, m: None)
    monkeypatch.setattr(prep, 'probe_runtime', lambda py, m: {'backend': 'metal'})
    monkeypatch.setattr(prep, 'audit_macho_runtime', lambda runtime: None)
    assert prep.existing_valid(manifest()) is True


def test_prepare_reuses_valid_existing_runtime_without_download(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(prep, 'OUTPUT', tmp_path / 'python-runtime')
    monkeypatch.setattr(prep, 'load_manifest', lambda: manifest())
    monkeypatch.setattr(prep, 'existing_valid', lambda m: True)
    monkeypatch.setattr(prep, 'download_verified', lambda *_: (_ for _ in ()).throw(AssertionError('downloaded')))
    prep.prepare(tmp_path / 'cache')
    assert 'already valid' in capsys.readouterr().out


def test_prepare_keeps_old_runtime_when_validation_fails(tmp_path, monkeypatch):
    output = tmp_path / 'python-runtime'
    output.mkdir()
    (output / 'keep.txt').write_text('old runtime', encoding='utf-8')
    archive = tmp_path / 'runtime.tar.gz'
    make_tar(archive, {'python/bin/python3': b'#!/bin/sh\n'})
    monkeypatch.setattr(prep, 'OUTPUT', output)
    monkeypatch.setattr(prep, 'load_manifest', lambda: manifest())
    monkeypatch.setattr(prep, 'existing_valid', lambda m: False)
    monkeypatch.setattr(prep, 'download_verified', lambda m, cache: archive)
    monkeypatch.setattr(prep, 'prove_interpreter', lambda py, runtime, m: None)
    monkeypatch.setattr(prep, 'install_packages', lambda py, m, cache: (_ for _ in ()).throw(prep.RuntimePrepError('install failed')))

    try:
        prep.prepare(tmp_path / 'cache')
        assert False
    except prep.RuntimePrepError as exc:
        assert 'install failed' in str(exc)
    assert (output / 'keep.txt').read_text(encoding='utf-8') == 'old runtime'


def test_install_packages_rejects_non_relocatable_wheel_uninstalls_and_tries_source(tmp_path, monkeypatch):
    events = []

    class WheelPlan:
        package_spec = 'llama-cpp-python==0.3.32'
        backend = 'metal'
        force_cmake = False
        def pip_install_args(self): return ['--only-binary', 'llama-cpp-python']
        def pip_env(self): return {}

    class SourcePlan:
        package_spec = 'llama-cpp-python==0.3.32'
        backend = 'metal'
        force_cmake = True
        def pip_install_args(self): return ['--no-binary', 'llama-cpp-python']
        def pip_env(self): return {'CMAKE_ARGS': '-DLLAMA_OPENSSL=OFF -DGGML_OPENMP=OFF', 'FORCE_CMAKE': '1'}

    def fake_run(cmd, **kwargs):
        if 'pip' in cmd and 'install' in cmd and 'llama-cpp-python==0.3.32' in cmd:
            events.append(('install', tuple(cmd), kwargs.get('env') or {}))
        if 'pip' in cmd and 'uninstall' in cmd:
            events.append(('uninstall', tuple(cmd), kwargs.get('env') or {}))
        class Result: stdout = ''
        return Result()

    validations = {'count': 0}
    def fake_validate(py, m, runtime):
        validations['count'] += 1
        if validations['count'] == 1:
            raise prep.RuntimePrepError('/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib')

    monkeypatch.setattr(prep, 'llama_cpp_install_plan_fallbacks', lambda **_: [WheelPlan(), SourcePlan()])
    monkeypatch.setattr(prep, 'run', fake_run)
    monkeypatch.setattr(prep, '_validate_candidate_install', fake_validate)
    monkeypatch.setattr(prep, '_uninstall_llama_cpp', lambda py: events.append(('uninstall', (), {})))

    prep.install_packages(tmp_path / 'runtime' / 'bin' / 'python3', manifest(), tmp_path / 'cache')

    assert [event[0] for event in events].count('install') == 2
    assert ('uninstall', (), {}) in events
    source_install = [event for event in events if event[0] == 'install'][-1]
    assert '--no-binary' in source_install[1]
    assert source_install[2]['CMAKE_OSX_ARCHITECTURES'] == 'arm64'


def test_install_packages_ignores_cpu_plans(tmp_path, monkeypatch):
    class CpuPlan:
        package_spec = 'llama-cpp-python'
        backend = 'cpu'
        force_cmake = False
        def pip_install_args(self): return []
        def pip_env(self): return {}

    monkeypatch.setattr(prep, 'llama_cpp_install_plan_fallbacks', lambda **_: [CpuPlan()])
    monkeypatch.setattr(prep, 'run', lambda *_, **__: type('Result', (), {'stdout': ''})())
    try:
        prep.install_packages(tmp_path / 'python3', manifest(), tmp_path / 'cache')
        assert False
    except prep.RuntimePrepError as exc:
        assert 'no Metal install plan' in str(exc)


def test_audit_macho_runtime_rejects_homebrew_openssl(monkeypatch, tmp_path):
    binary = tmp_path / 'python-runtime' / 'lib' / 'libllama-common.0.dylib'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'macho')
    monkeypatch.setattr(prep.platform, 'system', lambda: 'Darwin')
    def fake_run(cmd, *, text=True, capture_output=True, check=False):
        if cmd[0] == 'file':
            return subprocess.CompletedProcess(cmd, 0, 'Mach-O 64-bit dynamically linked shared library arm64', '')
        raise AssertionError(cmd)
    monkeypatch.setattr(prep.subprocess, 'run', fake_run)
    def fake_otool(cmd):
        if cmd[0] == 'lipo': return 'arm64'
        if cmd[0] == 'otool' and '-L' in cmd:
            return f'{binary}:\n\t/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib (compatibility version 3.0.0, current version 3.0.0)'
        if cmd[0] == 'otool' and '-l' in cmd:
            return 'cmd LC_ID_DYLIB\ncmdsize 48\nname @rpath/libllama-common.0.dylib (offset 24)'
        return ''
    monkeypatch.setattr(prep, '_otool', fake_otool)
    try:
        prep.audit_macho_runtime(tmp_path / 'python-runtime')
        assert False
    except prep.RuntimePrepError as exc:
        assert 'category=dependency ref=libssl.3.dylib' in str(exc)


def test_existing_valid_rejects_forbidden_linkage(tmp_path, monkeypatch):
    output = tmp_path / 'python-runtime'
    (output / 'bin').mkdir(parents=True)
    (output / 'bin' / 'python3').write_text('#!/bin/sh\n', encoding='utf-8')
    (output / prep.PROVENANCE).write_text(json.dumps({
        'source_archive_sha256': '0' * 64,
        'expected_backend': 'metal',
        'installed_packages': manifest()['required_packages'],
        'build_profile': prep.BUILD_PROFILE,
    }), encoding='utf-8')
    monkeypatch.setattr(prep, 'OUTPUT', output)
    monkeypatch.setattr(prep, 'prove_interpreter', lambda py, runtime, m: None)
    monkeypatch.setattr(prep, 'probe_runtime', lambda py, m: {})
    monkeypatch.setattr(prep, 'audit_macho_runtime', lambda runtime: (_ for _ in ()).throw(prep.RuntimePrepError('forbidden')))
    assert prep.existing_valid(manifest()) is False


def test_normalize_libpython_install_id_rewrites_exact_build_prefix(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    lib = runtime / 'lib' / 'libpython3.11.dylib'
    lib.parent.mkdir(parents=True)
    lib.write_bytes(b'macho')
    ids = {lib: '/install/lib/libpython3.11.dylib'}
    mutations = []

    monkeypatch.setattr(prep.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(prep, '_otool_install_id', lambda path: ids[path])
    monkeypatch.setattr(prep, '_normalize_stale_libpython_loads', lambda *args: None)

    def fake_install(args):
        mutations.append(args)
        assert args == ['-id', '@rpath/libpython3.11.dylib', str(lib)]
        ids[lib] = '@rpath/libpython3.11.dylib'

    monkeypatch.setattr(prep, '_install_name_tool', fake_install)

    prep.normalize_python_build_standalone_macos_runtime(runtime, manifest())

    assert mutations == [['-id', '@rpath/libpython3.11.dylib', str(lib)]]


def test_normalize_libpython_install_id_is_idempotent(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    lib = runtime / 'lib' / 'libpython3.11.dylib'
    lib.parent.mkdir(parents=True)
    lib.write_bytes(b'macho')
    mutations = []

    monkeypatch.setattr(prep.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(prep, '_otool_install_id', lambda path: '@rpath/libpython3.11.dylib')
    monkeypatch.setattr(prep, '_normalize_stale_libpython_loads', lambda *args: None)
    monkeypatch.setattr(prep, '_install_name_tool', lambda args: mutations.append(args))

    prep.normalize_python_build_standalone_macos_runtime(runtime, manifest())

    assert mutations == []


def test_normalize_libpython_install_id_rejects_unexpected_absolute_id(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    lib = runtime / 'lib' / 'libpython3.11.dylib'
    lib.parent.mkdir(parents=True)
    lib.write_bytes(b'macho')

    monkeypatch.setattr(prep.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(prep, '_otool_install_id', lambda path: '/elsewhere/lib/libpython3.11.dylib')

    try:
        prep.normalize_python_build_standalone_macos_runtime(runtime, manifest())
        assert False
    except prep.RuntimePrepError as exc:
        assert 'unexpected libpython install ID' in str(exc)


def test_normalize_stale_libpython_load_rewrites_interpreter_rpath_once(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    py = runtime / 'bin' / 'python3.11'
    py.parent.mkdir(parents=True)
    py.write_bytes(b'macho')
    deps = {py.resolve(): ['/install/lib/libpython3.11.dylib']}
    rpaths = {py.resolve(): []}
    mutations = []

    monkeypatch.setattr(prep.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(prep, '_is_macho_file', lambda path: True)
    monkeypatch.setattr(prep, '_otool_load_deps_by_arch', lambda path: {'arm64': deps[path]})
    monkeypatch.setattr(prep, '_parse_otool_rpaths', lambda out: rpaths[py.resolve()])
    monkeypatch.setattr(prep, '_otool_load_commands_by_arch', lambda path: {'arm64': ''})
    monkeypatch.setattr(prep, '_otool', lambda cmd: '')

    def fake_install(args):
        mutations.append(args)
        if args[:3] == ['-change', '/install/lib/libpython3.11.dylib', '@rpath/libpython3.11.dylib']:
            deps[py.resolve()] = ['@rpath/libpython3.11.dylib']
        if args[:2] == ['-add_rpath', '@executable_path/../lib']:
            rpaths[py.resolve()].append('@executable_path/../lib')

    monkeypatch.setattr(prep, '_install_name_tool', fake_install)

    prep._normalize_stale_libpython_loads(runtime, '/install/lib/libpython3.11.dylib', '@rpath/libpython3.11.dylib', 3, 11)

    assert ['-change', '/install/lib/libpython3.11.dylib', '@rpath/libpython3.11.dylib', str(py.resolve())] in mutations
    assert ['-add_rpath', '@executable_path/../lib', str(py.resolve())] in mutations

    mutations.clear()
    prep._normalize_stale_libpython_loads(runtime, '/install/lib/libpython3.11.dylib', '@rpath/libpython3.11.dylib', 3, 11)
    assert mutations == []


def test_normalize_stale_libpython_load_adds_dynload_rpath_without_duplicate(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    ext = runtime / 'lib' / 'python3.11' / 'lib-dynload' / '_ssl.cpython-311-darwin.so'
    ext.parent.mkdir(parents=True)
    ext.write_bytes(b'macho')
    deps = {ext.resolve(): ['/install/lib/libpython3.11.dylib']}
    rpaths = {ext.resolve(): ['@loader_path/../..']}
    mutations = []

    monkeypatch.setattr(prep.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(prep, '_is_macho_file', lambda path: True)
    monkeypatch.setattr(prep, '_otool_load_deps_by_arch', lambda path: {'arm64': deps[path]})
    monkeypatch.setattr(prep, '_parse_otool_rpaths', lambda out: rpaths[ext.resolve()])
    monkeypatch.setattr(prep, '_otool_load_commands_by_arch', lambda path: {'arm64': ''})
    monkeypatch.setattr(prep, '_otool', lambda cmd: '')

    def fake_install(args):
        mutations.append(args)
        if args[0] == '-change':
            deps[ext.resolve()] = ['@rpath/libpython3.11.dylib']

    monkeypatch.setattr(prep, '_install_name_tool', fake_install)

    prep._normalize_stale_libpython_loads(runtime, '/install/lib/libpython3.11.dylib', '@rpath/libpython3.11.dylib', 3, 11)

    assert any(args[0] == '-change' for args in mutations)
    assert not any(args[:2] == ['-add_rpath', '@loader_path/../..'] for args in mutations)


def test_normalize_stale_libpython_load_rejects_ambiguous_layout_and_arbitrary_install(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    other = runtime / 'lib' / 'other.dylib'
    other.parent.mkdir(parents=True)
    other.write_bytes(b'macho')
    deps = {other.resolve(): ['/install/lib/libpython3.11.dylib']}
    mutations = []

    monkeypatch.setattr(prep.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(prep, '_is_macho_file', lambda path: True)
    monkeypatch.setattr(prep, '_otool_load_deps_by_arch', lambda path: {'arm64': deps[path]})
    monkeypatch.setattr(prep, '_install_name_tool', lambda args: mutations.append(args))

    try:
        prep._normalize_stale_libpython_loads(runtime, '/install/lib/libpython3.11.dylib', '@rpath/libpython3.11.dylib', 3, 11)
        assert False
    except prep.RuntimePrepError as exc:
        assert 'ambiguous' in str(exc)
    assert mutations == []

    assert prep._forbidden_native_ref('/install/lib/libother.dylib') is False
    try:
        prep._validate_native_ref('/install/lib/libother.dylib', other)
        assert False
    except prep.RuntimePrepError as exc:
        assert 'absolute non-system' in str(exc)


def test_unique_runtime_macho_files_rejects_symlink_escape(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    outside = tmp_path / 'outside.dylib'
    outside.write_bytes(b'macho')
    (runtime / 'escape.dylib').symlink_to(outside)
    monkeypatch.setattr(prep.platform, 'system', lambda: 'Darwin')

    try:
        prep._unique_runtime_macho_files(runtime)
        assert False
    except prep.RuntimePrepError as exc:
        assert 'escapes staged runtime' in str(exc)


def test_prepare_normalizes_and_audits_before_install_packages(tmp_path, monkeypatch):
    output = tmp_path / 'python-runtime'
    archive = tmp_path / 'runtime.tar.gz'
    make_tar(archive, {'python/bin/python3': b'#!/bin/sh\n', 'python/lib/libpython3.11.dylib': b'macho'})
    events = []

    monkeypatch.setattr(prep, 'OUTPUT', output)
    monkeypatch.setattr(prep, 'load_manifest', lambda: manifest())
    monkeypatch.setattr(prep, 'existing_valid', lambda m: False)
    monkeypatch.setattr(prep, 'download_verified', lambda m, cache: archive)
    monkeypatch.setattr(prep, 'normalize_python_build_standalone_macos_runtime', lambda runtime, m: events.append('normalize'))
    monkeypatch.setattr(prep, 'audit_macho_runtime', lambda runtime: events.append('audit'))
    monkeypatch.setattr(prep, 'prove_interpreter', lambda py, runtime, m: events.append('prove'))
    monkeypatch.setattr(prep, 'install_packages', lambda py, m, cache: events.append('install'))
    monkeypatch.setattr(prep, 'probe_runtime', lambda py, m: events.append('probe'))
    monkeypatch.setattr(prep, 'run', lambda *_, **__: type('Result', (), {'stdout': '{}'})())

    prep.prepare(tmp_path / 'cache')

    assert events[:4] == ['normalize', 'audit', 'prove', 'install']


def test_prepare_baseline_normalization_failure_does_not_install_or_uninstall(tmp_path, monkeypatch):
    output = tmp_path / 'python-runtime'
    archive = tmp_path / 'runtime.tar.gz'
    make_tar(archive, {'python/bin/python3': b'#!/bin/sh\n', 'python/lib/libpython3.11.dylib': b'macho'})
    calls = []

    monkeypatch.setattr(prep, 'OUTPUT', output)
    monkeypatch.setattr(prep, 'load_manifest', lambda: manifest())
    monkeypatch.setattr(prep, 'existing_valid', lambda m: False)
    monkeypatch.setattr(prep, 'download_verified', lambda m, cache: archive)
    monkeypatch.setattr(prep, 'normalize_python_build_standalone_macos_runtime', lambda runtime, m: (_ for _ in ()).throw(prep.RuntimePrepError('bad id')))
    monkeypatch.setattr(prep, 'install_packages', lambda *args: calls.append('install'))
    monkeypatch.setattr(prep, '_uninstall_llama_cpp', lambda *args: calls.append('uninstall'))

    try:
        prep.prepare(tmp_path / 'cache')
        assert False
    except prep.RuntimePrepError as exc:
        assert 'bad id' in str(exc)
    assert calls == []


def test_build_profile_rejects_previous_profile(tmp_path, monkeypatch):
    output = tmp_path / 'python-runtime'
    (output / 'bin').mkdir(parents=True)
    (output / 'bin' / 'python3').write_text('#!/bin/sh\n', encoding='utf-8')
    (output / prep.PROVENANCE).write_text(json.dumps({
        'source_archive_sha256': '0' * 64,
        'expected_backend': 'metal',
        'installed_packages': manifest()['required_packages'],
        'build_profile': 'metal-relocatable-no-openssl-v1',
    }), encoding='utf-8')
    monkeypatch.setattr(prep, 'OUTPUT', output)

    assert prep.existing_valid(manifest()) is False


def test_audit_still_rejects_original_install_libpython_reference(monkeypatch, tmp_path):
    binary = tmp_path / 'python-runtime' / 'lib' / 'libpython3.11.dylib'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'macho')
    monkeypatch.setattr(prep.platform, 'system', lambda: 'Darwin')
    def fake_run(cmd, *, text=True, capture_output=True, check=False):
        if cmd[0] == 'file':
            return subprocess.CompletedProcess(cmd, 0, 'Mach-O 64-bit dynamically linked shared library arm64', '')
        raise AssertionError(cmd)
    monkeypatch.setattr(prep.subprocess, 'run', fake_run)

    def fake_otool(cmd):
        if cmd[0] == 'lipo':
            return 'arm64'
        if cmd[0] == 'otool' and '-L' in cmd:
            return f'{binary}:\n\t/install/lib/libpython3.11.dylib (compatibility version 3.11.0, current version 3.11.0)'
        if cmd[0] == 'otool' and '-l' in cmd:
            return 'cmd LC_ID_DYLIB\ncmdsize 56\nname /install/lib/libpython3.11.dylib (offset 24)'
        return ''

    monkeypatch.setattr(prep, '_otool', fake_otool)
    try:
        prep.audit_macho_runtime(tmp_path / 'python-runtime')
        assert False
    except prep.RuntimePrepError as exc:
        assert 'category=dependency ref=libpython3.11.dylib' in str(exc)

def test_parse_otool_install_ids_handles_spaces_and_rejects_bad_blocks():
    assert prep._parse_otool_install_ids(
        'Load command 0\ncmd LC_ID_DYLIB\ncmdsize 80\nname @rpath/lib with spaces.dylib (offset 24)\n'
    ) == ['@rpath/lib with spaces.dylib']
    assert prep._parse_otool_install_ids('Load command 0\ncmd LC_LOAD_DYLIB\n') == []
    for load_commands in (
        'Load command 0\ncmd LC_ID_DYLIB\ncmdsize 48\n',
        'Load command 0\ncmd LC_ID_DYLIB\nname @rpath/a.dylib (offset 24)\nLoad command 1\ncmd LC_ID_DYLIB\nname @rpath/b.dylib (offset 24)\n',
    ):
        try:
            prep._parse_otool_install_ids(load_commands)
            assert False
        except prep.RuntimePrepError:
            pass


def test_audit_macho_runtime_allows_bundle_and_executable_without_install_id(monkeypatch, tmp_path):
    runtime = tmp_path / 'python-runtime'
    bundle = runtime / 'lib/python3.11/site-packages/ada92cb5d92a588d1b93__mypyc.cpython-311-darwin.so'
    executable = runtime / 'bin/python3.11'
    bundle.parent.mkdir(parents=True)
    executable.parent.mkdir(parents=True)
    bundle.write_bytes(b'macho')
    executable.write_bytes(b'macho')
    monkeypatch.setattr(prep.platform, 'system', lambda: 'Darwin')

    def fake_run(cmd, *, text=True, capture_output=True, check=False):
        if cmd[0] == 'file':
            description = 'Mach-O 64-bit bundle arm64' if str(cmd[1]).endswith('.so') else 'Mach-O 64-bit executable arm64'
            return subprocess.CompletedProcess(cmd, 0, description, '')
        raise AssertionError(cmd)

    calls = []

    def fake_otool(cmd):
        calls.append(cmd[:2])
        if cmd[0] == 'lipo':
            return 'arm64'
        if cmd[0] == 'otool' and '-L' in cmd:
            return f'{cmd[-1]}:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1.0.0)'
        if cmd[0] == 'otool' and '-l' in cmd:
            return 'Load command 0\ncmd LC_RPATH\ncmdsize 48\npath @loader_path (offset 12)\n'
        if cmd[0] == 'otool' and '-D' in cmd:
            raise AssertionError('generic otool -D must not be used')
        raise AssertionError(cmd)

    monkeypatch.setattr(prep.subprocess, 'run', fake_run)
    monkeypatch.setattr(prep, '_otool', fake_otool)
    prep.audit_macho_runtime(runtime)
    assert ['otool', '-D'] not in calls


def test_audit_macho_runtime_requires_real_dylib_install_id(monkeypatch, tmp_path):
    binary = tmp_path / 'python-runtime/lib/libexample.dylib'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'macho')
    monkeypatch.setattr(prep.platform, 'system', lambda: 'Darwin')

    def fake_run(cmd, *, text=True, capture_output=True, check=False):
        if cmd[0] == 'file':
            return subprocess.CompletedProcess(cmd, 0, 'Mach-O 64-bit dynamically linked shared library arm64', '')
        raise AssertionError(cmd)

    def fake_otool(cmd):
        if cmd[0] == 'lipo':
            return 'arm64'
        if cmd[0] == 'otool' and '-L' in cmd:
            return f'{binary}:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1.0.0)'
        if cmd[0] == 'otool' and '-l' in cmd:
            return 'Load command 0\ncmd LC_RPATH\ncmdsize 48\npath @loader_path (offset 12)\n'
        if cmd[0] == 'otool' and '-D' in cmd:
            raise AssertionError('generic otool -D must not be used')
        raise AssertionError(cmd)

    monkeypatch.setattr(prep.subprocess, 'run', fake_run)
    monkeypatch.setattr(prep, '_otool', fake_otool)
    try:
        prep.audit_macho_runtime(tmp_path / 'python-runtime')
        assert False
    except prep.RuntimePrepError as exc:
        assert 'missing LC_ID_DYLIB' in str(exc)


def test_parse_otool_libraries_preserves_spaces_and_rejects_mismatched_arch(tmp_path):
    owner = tmp_path / 'python-runtime' / 'lib' / 'path with spaces' / 'libexample.dylib'
    output = (
        f'\n{owner} (architecture arm64):\n'
        '\t@loader_path/lib with spaces.dylib '
        '(compatibility version 1.0.0, current version 1.0.0)\n'
    )
    assert prep._parse_otool_libraries(output, owner, 'arm64') == ['@loader_path/lib with spaces.dylib']
    try:
        prep._parse_otool_libraries(output, owner, 'x86_64')
        assert False
    except prep.RuntimePrepError as exc:
        assert 'unexpected otool -L header' in str(exc)


def test_universal_mypyc_bundle_loads_are_parsed_per_architecture(monkeypatch, tmp_path):
    runtime = tmp_path / 'python-runtime'
    bundle = runtime / 'lib/python3.11/site-packages/ada92cb5d92a588d1b93__mypyc.cpython-311-darwin.so'
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(b'macho')
    monkeypatch.setattr(prep.platform, 'system', lambda: 'Darwin')

    def fake_run(cmd, *, text=True, capture_output=True, check=False):
        if cmd[0] == 'file':
            return subprocess.CompletedProcess(cmd, 0, 'Mach-O universal binary with 2 architectures: [x86_64:Mach-O 64-bit bundle x86_64] [arm64:Mach-O 64-bit bundle arm64]', '')
        raise AssertionError(cmd)

    calls = []

    def fake_otool(cmd):
        calls.append(cmd)
        if cmd[0] == 'lipo':
            return 'x86_64 arm64'
        if cmd[0] == 'otool' and '-L' in cmd:
            arch = cmd[cmd.index('-arch') + 1]
            return (
                f'{bundle} (architecture {arch}):\n'
                '\t/usr/lib/libSystem.B.dylib '
                '(compatibility version 1.0.0, current version 1292.100.5)\n'
            )
        if cmd[0] == 'otool' and '-l' in cmd:
            return 'Load command 0\ncmd LC_RPATH\ncmdsize 32\npath @loader_path (offset 12)\n'
        if cmd[0] == 'otool' and '-D' in cmd:
            raise AssertionError('generic otool -D must not be used')
        raise AssertionError(cmd)

    monkeypatch.setattr(prep.subprocess, 'run', fake_run)
    monkeypatch.setattr(prep, '_otool', fake_otool)

    prep.audit_macho_runtime(runtime)
    deps = prep._otool_load_deps_by_arch(bundle)
    assert deps == {'x86_64': ['/usr/lib/libSystem.B.dylib'], 'arm64': ['/usr/lib/libSystem.B.dylib']}
    assert all(str(bundle) not in arch_deps for arch_deps in deps.values())
    assert not any(cmd[0] == 'otool' and '-D' in cmd for cmd in calls)


def test_universal_dylib_install_ids_must_match_by_arch(monkeypatch, tmp_path):
    runtime = tmp_path / 'python-runtime'
    dylib = runtime / 'lib/libexample.dylib'
    dylib.parent.mkdir(parents=True)
    dylib.write_bytes(b'macho')
    monkeypatch.setattr(prep.platform, 'system', lambda: 'Darwin')

    def fake_run(cmd, *, text=True, capture_output=True, check=False):
        if cmd[0] == 'file':
            return subprocess.CompletedProcess(cmd, 0, 'Mach-O universal binary with 2 architectures: dynamically linked shared library', '')
        raise AssertionError(cmd)

    ids = {'x86_64': '@rpath/libexample.dylib', 'arm64': '@rpath/libexample.dylib'}

    def fake_otool(cmd):
        if cmd[0] == 'lipo':
            return 'x86_64 arm64'
        if cmd[0] == 'otool' and '-L' in cmd:
            arch = cmd[cmd.index('-arch') + 1]
            return f'{dylib} (architecture {arch}):\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1.0.0)\n'
        if cmd[0] == 'otool' and '-l' in cmd:
            arch = cmd[cmd.index('-arch') + 1]
            return f'Load command 0\ncmd LC_ID_DYLIB\ncmdsize 64\nname {ids[arch]} (offset 24)\n'
        raise AssertionError(cmd)

    monkeypatch.setattr(prep.subprocess, 'run', fake_run)
    monkeypatch.setattr(prep, '_otool', fake_otool)
    prep.audit_macho_runtime(runtime)

    ids['arm64'] = '@rpath/libdifferent.dylib'
    try:
        prep.audit_macho_runtime(runtime)
        assert False
    except prep.RuntimePrepError as exc:
        assert 'install IDs differ by architecture' in str(exc)


def test_stale_libpython_duplicate_within_one_arch_fails_but_repeated_by_arch_allowed(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    py = runtime / 'bin' / 'python3.11'
    py.parent.mkdir(parents=True)
    py.write_bytes(b'macho')
    old = '/install/lib/libpython3.11.dylib'
    new = '@rpath/libpython3.11.dylib'
    deps_by_arch = {'x86_64': [old], 'arm64': [old]}
    rpaths = []
    mutations = []

    monkeypatch.setattr(prep.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(prep, '_is_macho_file', lambda path: True)
    monkeypatch.setattr(prep, '_otool_load_deps_by_arch', lambda path: deps_by_arch)
    monkeypatch.setattr(prep, '_otool_load_commands_by_arch', lambda path: {'x86_64': '', 'arm64': ''})
    monkeypatch.setattr(prep, '_parse_otool_rpaths', lambda output: rpaths)

    def fake_install(args):
        mutations.append(args)
        if args[0] == '-change':
            deps_by_arch['x86_64'] = [new]
            deps_by_arch['arm64'] = [new]
        if args[0] == '-add_rpath':
            rpaths.append(args[1])

    monkeypatch.setattr(prep, '_install_name_tool', fake_install)
    prep._normalize_stale_libpython_loads(runtime, old, new, 3, 11)
    assert [args[0] for args in mutations].count('-change') == 1

    deps_by_arch['arm64'] = [old, old]
    try:
        prep._normalize_stale_libpython_loads(runtime, old, new, 3, 11)
        assert False
    except prep.RuntimePrepError as exc:
        assert 'duplicate stale libpython load command' in str(exc)
