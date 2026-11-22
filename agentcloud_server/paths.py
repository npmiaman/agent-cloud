"""Workspace path rules. Paths are logical keys, never filesystem paths, but we still
reject anything that looks like traversal so that clients, exports and future
filesystem-backed integrations can't be tricked."""
from __future__ import annotations

import unicodedata

MAX_PATH = 1024
MAX_SEGMENT = 255


class BadPath(ValueError):
    pass


def normalize(path: str, *, folder: bool = False, allow_root: bool = False) -> str:
    """Return the canonical form: 'a/b/c.txt' for files, 'a/b/' for folders, '' for root."""
    if path is None:
        raise BadPath("path required")
    p = unicodedata.normalize("NFC", path)
    if "\\" in p:
        raise BadPath("backslashes are not allowed")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in p):
        raise BadPath("control characters are not allowed")
    p = p.strip("/") if folder or allow_root else p.lstrip("/")
    if p == "":
        if allow_root:
            return ""
        raise BadPath("path required")
    if p.endswith("/") and not folder:
        raise BadPath("file path cannot end with '/'")
    segments = p.split("/")
    for s in segments:
        if s in ("", ".", ".."):
            raise BadPath(f"invalid path segment {s!r}")
        if len(s) > MAX_SEGMENT:
            raise BadPath("path segment too long")
        if s != s.strip():
            raise BadPath("path segments cannot start or end with whitespace")
    out = "/".join(segments)
    if folder:
        out += "/"
    if len(out) > MAX_PATH:
        raise BadPath("path too long")
    return out


def under(path: str, prefix: str) -> bool:
    """True if `path` is covered by a grant on `prefix`.

    '' covers everything. 'reports/' covers 'reports/x.md'. 'leads.csv' covers exactly
    that file. A folder prefix also covers the folder marker itself.
    """
    if prefix == "":
        return True
    if prefix.endswith("/"):
        return path.startswith(prefix) or path == prefix
    return path == prefix
