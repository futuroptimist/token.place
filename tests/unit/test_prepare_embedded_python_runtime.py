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

    prep.install_packages(tmp_path / 'python3', manifest(), tmp_path / 'pip-cache')

    # The source Metal build plan command should be present.
    source_cmd = commands[-3][0]
    assert '--no-binary' in source_cmd
    assert 'llama-cpp-python==0.3.32' == source_cmd[-1]
    assert commands[-3][1]['CMAKE_ARGS'] == '-DGGML_METAL=on'


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
