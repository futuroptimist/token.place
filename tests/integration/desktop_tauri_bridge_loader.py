from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def load_compute_node_bridge():
    bridge_path = (
        Path(__file__).resolve().parents[2]
        / "desktop-tauri"
        / "src-tauri"
        / "python"
        / "compute_node_bridge.py"
    )
    spec = spec_from_file_location("desktop_compute_node_bridge", bridge_path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
