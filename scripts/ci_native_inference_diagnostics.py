"""Bounded CI metadata without native inference imports or initialization."""

from __future__ import annotations

import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform

CPU_FIELDS = {
    "vendor_id",
    "cpu family",
    "model",
    "model name",
    "stepping",
    "flags",
    "Features",
}


def cpu_metadata(text: str) -> dict[str, str]:
    first = text.split("\n\n", 1)[0]
    return {
        key.strip(): value.strip()[:4096]
        for line in first.splitlines()
        if ":" in line
        for key, value in [line.split(":", 1)]
        if key.strip() in CPU_FIELDS
    }


def package_metadata(name: str, directory: str | None = None) -> dict:
    try:
        if directory is None:
            dist = metadata.distribution(name)
        else:
            dist = next(
                (
                    item
                    for item in metadata.distributions(path=[directory])
                    if item.metadata["Name"].lower().replace("_", "-") == name
                ),
                None,
            )
            if dist is None:
                raise metadata.PackageNotFoundError(name)
    except metadata.PackageNotFoundError:
        return {"name": name, "available": False}
    result = {
        "name": name,
        "version": dist.version[:128],
        "wheel_tags": [],
        "native_files": [],
    }
    wheel = dist.read_text("WHEEL") or ""
    result["wheel_tags"] = [
        line[5:][:256]
        for line in wheel.splitlines()
        if line.startswith("Tag: ")
    ][:16]
    if name == "llama-cpp-python":
        files = [
            item
            for item in (dist.files or [])
            if Path(item).as_posix().startswith("llama_cpp/")
            and ".so" in Path(item).name
        ][:32]
        for item in files:
            path = Path(dist.locate_file(item))
            entry = {"name": Path(item).name[:256]}
            try:
                entry["size"] = path.stat().st_size
                if entry["size"] <= 256 * 1024 * 1024:
                    with path.open("rb") as stream:
                        digest = hashlib.sha256()
                        for chunk in iter(
                            lambda: stream.read(1024 * 1024), b""
                        ):
                            digest.update(chunk)
                        entry["sha256"] = digest.hexdigest()
                else:
                    entry["hash"] = "omitted: size limit"
            except OSError as error:
                entry["error"] = type(error).__name__
            result["native_files"].append(entry)
    return result


def main() -> None:
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
    ):
        raise SystemExit("GitHub-hosted CI diagnostics only")
    with Path("/proc/cpuinfo").open(encoding="utf-8") as stream:
        cpu = cpu_metadata(stream.read(65536))
    report = {
        "python": platform.python_version(),
        "architecture": platform.machine(),
        "kernel": platform.release(),
        "cpu": cpu,
        "packages": [
            package_metadata(name)
            for name in ("llama-cpp-python", "numpy", "pip")
        ],
        "desktop_target_packages": [
            package_metadata(name, ".token_place_desktop_site")
            for name in ("llama-cpp-python", "numpy")
        ],
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
