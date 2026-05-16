from pathlib import Path
import re

LEGACY_ROUTES = ("/sink", "/faucet", "/source", "/retrieve", "/next_server")


ACTIVE_RUNTIME_ROOTS = (
    "api",
    "client.py",
    "server.py",
    "relay.py",
    "utils",
    "static",
    "desktop",
    "desktop-tauri",
)

RUNTIME_SUFFIXES = (
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".rs",
    ".html",
)

LOUD_API_V1_ONLY_FAILURE = """
token.place v0.1.0 is API v1-only.
Active runtime code must not call /sink, /faucet, /source, /retrieve, or /next_server.
Use API v1 E2EE relay routes instead:
  - /api/v1/relay/servers/register
  - /api/v1/relay/servers/poll
  - /api/v1/relay/servers/next
  - /api/v1/relay/requests
  - /api/v1/relay/responses
  - /api/v1/relay/responses/retrieve
Relay must see ciphertext only.
Do not add allowlist entries unless the occurrence is a deprecated route definition, a deprecation test, or documentation.
""".strip()

DESKTOP_BRIDGE_FORBIDDEN_LEGACY_MARKERS = (
    "is_legacy_relay_payload",
    "legacy_payload",
    *LEGACY_ROUTES,
)


def _legacy_route_pattern(route: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(route)}(?![A-Za-z0-9_])")


def _runtime_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for runtime_root in ACTIVE_RUNTIME_ROOTS:
        path = root / runtime_root
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = [candidate for candidate in path.rglob("*") if candidate.is_file()]
        else:
            continue
        files.extend(
            candidate
            for candidate in candidates
            if candidate.suffix in RUNTIME_SUFFIXES
            and "node_modules" not in candidate.parts
            and "target" not in candidate.parts
            and "dist" not in candidate.parts
            and "build" not in candidate.parts
        )
    return sorted(set(files))


def _line_number_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _relay_deprecated_route_definition_lines(text: str) -> set[int]:
    """Return line numbers that are part of Flask legacy route definitions."""
    allowed_lines: set[int] = set()
    route_markers = tuple(f"@app.route('{route}'" for route in LEGACY_ROUTES)
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith(route_markers):
            block_start = index
            index += 1
            while index < len(lines):
                next_stripped = lines[index].strip()
                if next_stripped.startswith("@app.route(") and index > block_start:
                    break
                index += 1
            allowed_lines.update(range(block_start + 1, index + 1))
            continue
        index += 1
    return allowed_lines


def _relay_client_legacy_compatibility_lines(text: str) -> set[int]:
    """Return line numbers for isolated legacy compatibility helpers.

    These helpers are retained for deprecated compatibility tests while API v1
    runtime sections are checked separately above and must stay legacy-route-free.
    """
    allowed_lines: set[int] = set()
    legacy_sections = (
        ("def ping_relay", "def poll_api_v1_encrypted_work"),
        ("def process_client_request", "def process_api_v1_chat_request"),
        ("def poll_relay_continuously", "def get_task_from_relay"),
    )
    lines = text.splitlines()
    for start_marker, end_marker in legacy_sections:
        try:
            start = next(i for i, line in enumerate(lines) if start_marker in line)
            end = next(
                i
                for i, line in enumerate(lines[start + 1 :], start + 1)
                if end_marker in line
            )
        except StopIteration:
            continue
        allowed_lines.update(range(start + 1, end + 1))
    return allowed_lines


def _allowed_legacy_route_lines(path: Path, root: Path, text: str) -> set[int]:
    relative = path.relative_to(root).as_posix()
    if relative == "relay.py":
        return _relay_deprecated_route_definition_lines(text)
    if relative == "utils/networking/relay_client.py":
        return _relay_client_legacy_compatibility_lines(text)
    return set()


def test_api_v1_only_active_runtime_paths_do_not_reference_deprecated_legacy_routes():
    root = Path(__file__).resolve().parents[2]
    violations: list[str] = []

    for path in _runtime_files(root):
        text = path.read_text(encoding="utf-8")
        allowed_lines = _allowed_legacy_route_lines(path, root, text)
        relative = path.relative_to(root)
        for route in LEGACY_ROUTES:
            pattern = _legacy_route_pattern(route)
            for match in pattern.finditer(text):
                line_number = _line_number_for_offset(text, match.start())
                if line_number in allowed_lines:
                    continue
                violations.append(f"{relative}:{line_number} references deprecated legacy relay route {route}")

    assert not violations, (
        LOUD_API_V1_ONLY_FAILURE
        + "\n\nForbidden legacy route references:\n"
        + "\n".join(violations)
    )


def test_desktop_tauri_compute_node_bridge_has_no_legacy_payload_or_route_markers():
    root = Path(__file__).resolve().parents[2]
    path = root / "desktop-tauri" / "src-tauri" / "python" / "compute_node_bridge.py"
    text = path.read_text(encoding="utf-8")

    violations = [marker for marker in DESKTOP_BRIDGE_FORBIDDEN_LEGACY_MARKERS if marker in text]

    assert not violations, (
        LOUD_API_V1_ONLY_FAILURE
        + "\n\ndesktop-tauri/src-tauri/python/compute_node_bridge.py must not contain: "
        + ", ".join(DESKTOP_BRIDGE_FORBIDDEN_LEGACY_MARKERS)
        + "\nFound: "
        + ", ".join(violations)
    )


def test_active_production_paths_do_not_reference_legacy_relay_routes():
    root = Path(__file__).resolve().parents[2]
    targets = [
        root / "api" / "v1" / "compute_provider.py",
        root / "client.py",
        root / "utils" / "crypto_helpers.py",
        root / "utils" / "compute_node_runtime.py",
        root / "static" / "chat.js",
        root / "desktop" / "src" / "services" / "desktopBridgeClient.ts",
        root / "desktop" / "src" / "services" / "desktopApiClient.ts",
        root / "desktop-tauri" / "src" / "App.tsx",
        root / "desktop-tauri" / "src-tauri" / "src" / "forward.rs",
    ]

    violations: list[str] = []
    for path in targets:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for route in LEGACY_ROUTES:
            pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(route)}(?![A-Za-z0-9_])")
            if pattern.search(text):
                violations.append(f"{path.relative_to(root)} uses deprecated route {route}")

    assert not violations, "\n".join(violations)


def test_api_v1_relay_client_paths_do_not_reference_legacy_relay_routes():
    root = Path(__file__).resolve().parents[2]
    path = root / "utils" / "networking" / "relay_client.py"
    text = path.read_text(encoding="utf-8")
    active_sections = [
        ("def poll_api_v1_encrypted_work", "def _api_v1_response_relay_url"),
        ("def _api_v1_response_relay_url", "def process_client_request"),
        ("if api_v1_request_payload is not None:", "chat_history = _extract_chat_history"),
        ("def poll_api_v1_encrypted_work_continuously", "def poll_relay_continuously"),
    ]

    violations: list[str] = []
    for start_marker, end_marker in active_sections:
        start = text.index(start_marker)
        end = text.index(end_marker, start + len(start_marker))
        section = text[start:end]
        for route in LEGACY_ROUTES:
            pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(route)}(?![A-Za-z0-9_])")
            if pattern.search(section):
                violations.append(f"{path.relative_to(root)} active API v1 path uses {route}")

    assert not violations, "\n".join(violations)
