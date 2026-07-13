#!/usr/bin/env python3
"""Prepare the self-contained macOS arm64 Python runtime for token.place desktop."""
from __future__ import annotations
import argparse, hashlib, json, os, shutil, subprocess, sys, tarfile, tempfile, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "desktop-tauri/src-tauri/python/embedded_python_runtime_manifest.json"
OUT = ROOT / "desktop-tauri/src-tauri/python-runtime"
REQ = ROOT / "desktop-tauri/src-tauri/python/requirements_desktop_runtime.txt"
PROVENANCE = "embedded-runtime-provenance.json"

class RuntimeErrorClosed(RuntimeError): pass

def load_manifest(path=MANIFEST):
    data=json.loads(Path(path).read_text())
    if data.get("schema_version") != 1: raise RuntimeErrorClosed("unsupported manifest schema_version")
    if not str(data.get("archive_url","")).startswith("https://"): raise RuntimeErrorClosed("archive_url must be https")
    digest=str(data.get("sha256", ""))
    if len(digest)!=64 or any(c not in "0123456789abcdef" for c in digest.lower()): raise RuntimeErrorClosed("sha256 is malformed")
    if data.get("target_triple") != "aarch64-apple-darwin": raise RuntimeErrorClosed("unexpected target_triple")
    if data.get("expected_interpreter_path") != "bin/python3": raise RuntimeErrorClosed("unexpected interpreter path")
    return data

def sha256(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024), b''): h.update(b)
    return h.hexdigest()

def download(manifest, cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    name=manifest["archive_url"].rsplit('/',1)[-1].replace('%2B','+')
    dest=cache/name
    if dest.exists() and sha256(dest)==manifest['sha256']: return dest
    if dest.exists(): dest.unlink()
    tmp=dest.with_suffix(dest.suffix+'.tmp')
    urllib.request.urlretrieve(manifest['archive_url'], tmp)  # nosec B310 - manifest validation requires immutable HTTPS URL
    if sha256(tmp)!=manifest['sha256']:
        tmp.unlink(missing_ok=True); raise RuntimeErrorClosed("download digest mismatch")
    tmp.replace(dest); return dest

def safe_extract(archive: Path, dest: Path, expected_root: str):
    with tarfile.open(archive) as tf:
        members=tf.getmembers()
        for m in members:
            n=Path(m.name)
            if n.is_absolute() or '..' in n.parts: raise RuntimeErrorClosed(f"unsafe archive path: {m.name}")
            if n.parts[:1] != (expected_root,): raise RuntimeErrorClosed(f"unexpected archive root: {m.name}")
            if m.issym() or m.islnk():
                target=Path(m.linkname)
                if target.is_absolute() or '..' in target.parts: raise RuntimeErrorClosed(f"escaping link: {m.name}")
        tf.extractall(dest, filter='data')  # nosec B202 - members are validated above before extraction

def run(cmd, **kw):
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, **kw)

def interpreter_probe(py: Path, manifest):
    code="""import json, platform, sys; print(json.dumps({'version':sys.version_info[:2], 'machine':platform.machine(), 'executable':sys.executable, 'prefix':sys.prefix}))"""
    data=json.loads(run([str(py),'-c',code]).stdout)
    if data['version'] != [3,11]: raise RuntimeErrorClosed("bundled interpreter is not Python 3.11")
    if data['machine'] != manifest['expected_architecture']: raise RuntimeErrorClosed("bundled interpreter has wrong architecture")
    root=py.parents[1].resolve()
    if not Path(data['executable']).resolve().is_relative_to(root): raise RuntimeErrorClosed("sys.executable escaped runtime")
    if not Path(data['prefix']).resolve().is_relative_to(root): raise RuntimeErrorClosed("sys.prefix escaped runtime")

def pip_install(py: Path, cache: Path, manifest):
    run([str(py),'-m','ensurepip','--upgrade'])
    env=os.environ.copy(); env['PIP_CACHE_DIR']=str(cache/'pip'); env['PYTHONNOUSERSITE']='1'
    reqs=[str(REQ), 'llama-cpp-python==0.3.32']
    run([str(py),'-m','pip','install','--upgrade','pip'], env=env)
    run([str(py),'-m','pip','install','-r',reqs[0], reqs[1]], env=env)
    run([str(py),'-m','pip','check'], env=env)

def import_checks(py: Path):
    mods=['psutil','requests','dotenv','cryptography','jinja2','numpy','diskcache','llama_cpp']
    run([str(py),'-c','import '+','.join(mods)])

def runtime_probe(py: Path):
    script=ROOT/'desktop-tauri/src-tauri/python/desktop_runtime_setup.py'
    code=f"""import importlib.util, json, pathlib
p=pathlib.Path({str(script)!r}); spec=importlib.util.spec_from_file_location('desktop_runtime_setup', p); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); r=m._probe_llama_runtime(runtime_root=p.parent); print(json.dumps(m._probe_result_payload(r)))
"""
    out=json.loads(run([str(py),'-c',code], cwd=script.parent).stdout)
    required=['rope_scaling_type_supported','rope_freq_scale_supported','yarn_orig_ctx_supported']
    if out.get('backend')!='metal' or not out.get('gpu_offload_supported'): raise RuntimeErrorClosed('bundled llama_cpp is not Metal-capable')
    if out.get('llama_cpp_python_version')!='0.3.32': raise RuntimeErrorClosed('unexpected llama-cpp-python version')
    if not out.get('qwen_64k_yarn_support'): raise RuntimeErrorClosed('missing Qwen 64K YaRN support')
    if not all(out.get(k) for k in required): raise RuntimeErrorClosed('missing Qwen constructor capability')
    return out

def clean(root: Path):
    for p in root.rglob('__pycache__'): shutil.rmtree(p, ignore_errors=True)
    for pat in ('*.pyc','*.pyo'):
        for p in root.rglob(pat): p.unlink(missing_ok=True)
    for p in root.rglob('tests'):
        if p.is_dir() and 'site-packages' in str(p): shutil.rmtree(p, ignore_errors=True)

def packages(py: Path):
    code="""import importlib.metadata as m, json; print(json.dumps({d.metadata['Name']: d.version for d in m.distributions()}, sort_keys=True))"""
    return json.loads(run([str(py),'-c',code]).stdout)

def provenance(manifest, probe, py):
    return {'cpython_version':manifest['cpython_version'],'target_triple':manifest['target_triple'],'source_archive_digest':manifest['sha256'],'installed_package_versions':packages(py),'expected_backend':'metal','probe':probe,'build_timestamp':datetime.now(timezone.utc).isoformat(),'repository_commit':subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,text=True,stdout=subprocess.PIPE).stdout.strip() or None}

def existing_valid(manifest):
    py=OUT/'bin/python3'; prov=OUT/PROVENANCE
    if not py.exists() or not prov.exists(): return False
    try:
        data=json.loads(prov.read_text()); interpreter_probe(py, manifest); import_checks(py); run([str(py),'-m','pip','check']);
        return data.get('source_archive_digest')==manifest['sha256'] and data.get('expected_backend')=='metal'
    except Exception: return False

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--cache-dir', type=Path, default=Path(os.environ.get('TOKEN_PLACE_PYTHON_RUNTIME_CACHE', ROOT/'.cache/embedded-python'))); ap.add_argument('--skip-install', action='store_true', help='tests only')
    args=ap.parse_args(); manifest=load_manifest()
    if existing_valid(manifest): print('embedded runtime already valid'); return 0
    archive=download(manifest,args.cache_dir)
    with tempfile.TemporaryDirectory(prefix='token-place-python-runtime-') as td:
        tmp=Path(td); safe_extract(archive,tmp,manifest['expected_archive_root'])
        src=tmp/manifest['expected_archive_root']; py=src/manifest['expected_interpreter_path']
        if not py.exists(): raise RuntimeErrorClosed('archive missing interpreter')
        interpreter_probe(py, manifest)
        if not args.skip_install:
            pip_install(py,args.cache_dir,manifest); import_checks(py); probe=runtime_probe(py)
        else: probe={'backend':'metal','gpu_offload_supported':True}
        clean(src); (src/PROVENANCE).write_text(json.dumps(provenance(manifest, probe, py), indent=2, sort_keys=True))
        for notice in manifest.get('runtime_license_notices',[]):
            lp=src/notice['path']; lp.parent.mkdir(parents=True, exist_ok=True)
            bundled=ROOT/'desktop-tauri/src-tauri/python'/notice['path']
            if bundled.exists(): shutil.copy2(bundled, lp)
        staging=OUT.with_name('python-runtime.staging')
        if staging.exists(): shutil.rmtree(staging)
        shutil.copytree(src, staging, symlinks=True)
        backup=OUT.with_name('python-runtime.previous')
        if backup.exists(): shutil.rmtree(backup)
        if OUT.exists(): OUT.replace(backup)
        staging.replace(OUT); shutil.rmtree(backup, ignore_errors=True)
    shutil.rmtree(args.cache_dir/'pip', ignore_errors=True)
    print(f'prepared {OUT}')
    return 0
if __name__=='__main__':
    try: raise SystemExit(main())
    except RuntimeErrorClosed as e: print(f'embedded_runtime_prepare_failed: {e}', file=sys.stderr); raise SystemExit(2)
