"""Helpers for constructing cross-platform test matrices."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, MutableMapping, Sequence


@dataclass(frozen=True, slots=True)
class PlatformMatrixEntry:
    """A single target entry for the CI test matrix."""

    os: str
    python: str
    node: str
    marker_expression: str
    env: MutableMapping[str, str]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation."""

        payload = asdict(self)
        payload["env"] = dict(self.env)
        return payload


_DEFAULT_MATRIX: Sequence[PlatformMatrixEntry] = (
    PlatformMatrixEntry(
        os="ubuntu-latest",
        python="3.11",
        node="20",
        marker_expression="not slow and not browser",
        env={"TOKEN_PLACE_TARGET_OS": "ubuntu", "PYTEST_ADDOPTS": "-ra"},
    ),
    PlatformMatrixEntry(
        os="macos-latest",
        python="3.11",
        node="20",
        marker_expression="not slow and not browser",
        env={"TOKEN_PLACE_TARGET_OS": "macos"},
    ),
    PlatformMatrixEntry(
        os="windows-latest",
        python="3.11",
        node="20",
        marker_expression="not slow and not browser",
        env={"TOKEN_PLACE_TARGET_OS": "windows"},
    ),
)


def get_platform_matrix() -> Iterable[dict[str, object]]:
    """Return the canonical platform test matrix as dictionaries."""

    return tuple(entry.to_dict() for entry in _DEFAULT_MATRIX)


def build_pytest_args(
    entry: MutableMapping[str, object], *, base_args: Sequence[str] | None = None
) -> list[str]:
    """Compose pytest CLI arguments for the provided matrix entry."""

    marker_expression = entry["marker_expression"]
    args = list(base_args or [])
    if "-m" in args:
        marker_index = args.index("-m")
        if marker_index == len(args) - 1:
            raise ValueError("'-m' flag must be followed by a marker expression")
        args[marker_index + 1] = f"{args[marker_index + 1]} and ({marker_expression})"
    else:
        args = ["-m", str(marker_expression), *args]
    return args
