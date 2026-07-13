# Embedded macOS Python runtime

Packaged Apple Silicon macOS releases include a relocatable CPython runtime at
`Contents/Resources/python-runtime/bin/python3`. Users of the release DMG do not
need Python, Homebrew, Xcode, or Xcode Command Line Tools.

The runtime is prepared from the pinned `python-build-standalone` manifest in
`embedded_python_runtime_manifest.json`:

- CPython: 3.11.13
- Build: `cpython-3.11.13+20250612-aarch64-apple-darwin-install_only`
- Target: `aarch64-apple-darwin`
- SHA-256: `e272f0baca8f5a3cef29cc9c7418b80d0316553062ad3235205a33992155043c`

Update process:

1. Choose a specific `python-build-standalone` release asset; never use `latest`.
2. Update the manifest URL, build identifier, expected layout, and SHA-256.
3. Run `desktop-tauri/scripts/prepare_embedded_python_runtime.py` on an arm64 Mac.
4. Confirm the generated provenance, Metal `llama_cpp` probe, artifact validation,
   and signing checks pass before publishing a DMG.

Generated runtime directories and downloaded archives are intentionally not
checked into git.
