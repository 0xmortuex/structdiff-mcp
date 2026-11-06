"""Read-only file access restricted to allowed directories."""

from __future__ import annotations

import os

from .errors import StructDiffError

MAX_STRUCTURED_BYTES = 50 * 1024 * 1024

_roots: list[str] = []


def configure(dirs: list[str]) -> None:
    """Set the allowed directories (the first one is the base for relative paths)."""
    resolved = []
    for d in dirs:
        real = os.path.realpath(d)
        if not os.path.isdir(real):
            raise StructDiffError(f"--dir {d!r} is not an existing directory")
        resolved.append(real)
    _roots[:] = resolved


def roots() -> list[str]:
    if not _roots:
        _roots.append(os.path.realpath(os.getcwd()))
    return list(_roots)


def _inside(path: str, root: str) -> bool:
    p, r = os.path.normcase(path), os.path.normcase(root)
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:  # different drives on Windows
        return False


def resolve(path: str) -> str:
    """Return the real path of an existing regular file inside the sandbox."""
    allowed = roots()
    candidate = path if os.path.isabs(path) else os.path.join(allowed[0], path)
    real = os.path.realpath(candidate)
    if not any(_inside(real, r) for r in allowed):
        raise StructDiffError(
            f"{path!r} is outside the allowed directories {allowed}; "
            "start the server with --dir <folder> to allow it, or paste the content and use diff_text"
        )
    if not os.path.exists(real):
        raise StructDiffError(f"{path!r} does not exist (looked for {real})")
    if not os.path.isfile(real):
        raise StructDiffError(f"{path!r} is not a regular file")
    return real
