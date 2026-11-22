"""Backend for Claude's memory tool (`memory_20250818`).

The memory tool is client-side: Claude asks for `view`, `create`, `str_replace`,
`insert`, `delete` or `rename` on paths under `/memories`, and the application runs
them. This module runs them against an Agent Cloud workspace, so an app gets, with no
storage code of its own: per-agent isolation, permissions, a version for every memory
edit (so a bad edit can be undone), provenance and trust labels, and the audit log.

`/memories/x.md` maps to `memories/x.md` in the agent's workspace. Return strings
follow the reference implementation in the memory tool docs, so Claude sees exactly
what it expects.
"""
from __future__ import annotations

from . import service as S
from .auth import Principal
from .paths import BadPath, normalize
from .service import ApiError, Ctx

ROOT = "/memories"
FOLDER = "memories/"
VIEW_MAX_CHARS = 16_000
MAX_LINES = 999_999


class MemoryError_(Exception):
    pass


def _rel(path: str | None) -> str:
    """'/memories/a/b.md' -> 'memories/a/b.md'; '/memories' -> 'memories/'. Rejects anything outside."""
    if not isinstance(path, str) or not path:
        raise MemoryError_("Error: path is required")
    if "%2e" in path.lower() or "\\" in path:
        raise MemoryError_(f"Error: invalid path {path}")
    p = path.rstrip("/") or "/"
    if p != ROOT and not p.startswith(ROOT + "/"):
        raise MemoryError_(f"Error: paths must start with {ROOT}")
    rest = p[len(ROOT):].lstrip("/")
    if not rest:
        return FOLDER
    try:
        return normalize(FOLDER + rest)
    except BadPath as e:
        raise MemoryError_(f"Error: invalid path {path}: {e}")


def _shown(rel: str) -> str:
    return ROOT if rel == FOLDER else ROOT + "/" + rel[len(FOLDER):].rstrip("/")


def _human(n: int) -> str:
    for unit in ("", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.1f}{unit}" if unit else f"{n}B"
        n /= 1024
    return str(n)


def _is_dir(ctx: Ctx, p: Principal, ws: dict, rel: str) -> bool:
    if rel == FOLDER:
        return True
    return bool(S.live_row(ctx.db, ws["id"], rel.rstrip("/") + "/")) or bool(
        S.list_files(ctx, p, ws, rel.rstrip("/") + "/", recursive=False))


def _is_file(ctx: Ctx, ws: dict, rel: str) -> bool:
    f = S.live_row(ctx.db, ws["id"], rel)
    return bool(f and f["kind"] == "file")


def _read(ctx: Ctx, p: Principal, ws: dict, rel: str) -> str:
    _, _, data = S.read_file(ctx, p, ws, rel)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise MemoryError_(f"Error: {_shown(rel)} is not a text file")


def _numbered(text: str, start: int = 1) -> str:
    lines = text.split("\n")
    if lines and lines[-1] == "" and len(lines) > 1:
        lines = lines[:-1]
    return "\n".join(f"{i:6d}\t{line}" for i, line in enumerate(lines, start))


def execute(ctx: Ctx, p: Principal, ws: dict, cmd: dict) -> tuple[str, bool]:
    """Run one memory command. Returns (text for the tool_result, is_error)."""
    try:
        return _execute(ctx, p, ws, cmd), False
    except MemoryError_ as e:
        return str(e), True
    except ApiError as e:
        if e.status == 403:
            return f"Error: permission denied: {e.message}", True
        if e.code == "pending" or e.status == 202:
            return e.message, False
        return f"Error: {e.message}", True


def _saved(res: dict, ok: str) -> str:
    if res.get("status") == "pending_approval":
        return f"{ok} (waiting for human approval, proposal {res['proposal_id']}; the stored memory is unchanged until then)"
    return ok


def _execute(ctx: Ctx, p: Principal, ws: dict, cmd: dict) -> str:
    c = cmd.get("command")
    if c == "view":
        rel = _rel(cmd.get("path"))
        if rel != FOLDER and _is_file(ctx, ws, rel):
            text = _read(ctx, p, ws, rel)
            n_lines = text.count("\n") + (0 if text.endswith("\n") else 1)
            if n_lines > MAX_LINES:
                return f"File {_shown(rel)} exceeds maximum line limit of 999,999 lines."
            vr = cmd.get("view_range")
            start = 1
            if vr:
                lines = text.split("\n")
                a = max(1, int(vr[0]))
                b = len(lines) if int(vr[1]) == -1 else min(len(lines), int(vr[1]))
                text, start = "\n".join(lines[a - 1:b]), a
            body = _numbered(text, start)
            if len(body) > VIEW_MAX_CHARS and not vr:
                body = body[:VIEW_MAX_CHARS] + "\n... (truncated; view a line range with view_range)"
            return f"Here's the content of {_shown(rel)} with line numbers:\n{body}"
        if _is_dir(ctx, p, ws, rel):
            prefix = rel if rel.endswith("/") else rel + "/"
            entries = S.list_files(ctx, p, ws, prefix if prefix != FOLDER else FOLDER, recursive=True)
            depth0 = prefix.count("/")
            rows, total = [], 0
            for e in sorted(entries, key=lambda e: e["path"]):
                name = e["path"].rstrip("/")
                if name.rsplit("/", 1)[-1].startswith(".") or "/node_modules" in name:
                    continue
                if e["path"].rstrip("/").count("/") - depth0 + 1 > 2:
                    total += e.get("size") or 0
                    continue
                size = e.get("size") or 0
                if e["kind"] == "folder":
                    size = sum(x.get("size") or 0 for x in entries if x["path"].startswith(e["path"]) and x["kind"] == "file")
                else:
                    total += size
                rows.append(f"{_human(size)}\t{_shown(e['path'])}")
            return ("Here're the files and directories up to 2 levels deep in "
                    f"{_shown(rel)}, excluding hidden items and node_modules:\n" + "\n".join([f"{_human(total)}\t{_shown(rel)}"] + rows))
        raise MemoryError_(f"The path {cmd.get('path')} does not exist. Please provide a valid path.")

    if c == "create":
        rel = _rel(cmd.get("path"))
        if rel == FOLDER or rel.endswith("/"):
            raise MemoryError_(f"Error: {cmd.get('path')} is a directory")
        res = S.write_file(ctx, p, ws, rel, (cmd.get("file_text") or "").encode("utf-8"), None, None, "memory: create")
        return _saved(res, f"File created successfully at: {_shown(rel)}")

    if c == "str_replace":
        rel = _rel(cmd.get("path"))
        if rel == FOLDER or not _is_file(ctx, ws, rel):
            raise MemoryError_(f"Error: The path {cmd.get('path')} does not exist. Please provide a valid path.")
        text = _read(ctx, p, ws, rel)
        old, new = cmd.get("old_str") or "", cmd.get("new_str") or ""
        count = text.count(old) if old else 0
        if count == 0:
            raise MemoryError_(f"No replacement was performed, old_str `{old}` did not appear verbatim in {_shown(rel)}.")
        if count > 1:
            lines = [str(i) for i, line in enumerate(text.split("\n"), 1) if old.split("\n")[0] in line]
            raise MemoryError_(f"No replacement was performed. Multiple occurrences of old_str `{old}` in lines: "
                               f"{', '.join(lines)}. Please ensure it is unique")
        res = S.edit_file(ctx, p, ws, rel, [{"old": old, "new": new}], "memory: str_replace")
        updated = text.replace(old, new, 1)
        at = updated[:updated.find(new) if new else text.find(old)].count("\n") + 1
        lines = updated.split("\n")
        a, b = max(1, at - 3), min(len(lines), at + new.count("\n") + 3)
        snippet = "\n".join(f"{i:6d}\t{lines[i - 1]}" for i in range(a, b + 1))
        return _saved(res, "The memory file has been edited.") + f"\n{snippet}"

    if c == "insert":
        rel = _rel(cmd.get("path"))
        if rel == FOLDER or not _is_file(ctx, ws, rel):
            raise MemoryError_(f"Error: The path {cmd.get('path')} does not exist")
        text = _read(ctx, p, ws, rel)
        lines = text.split("\n")
        n = int(cmd.get("insert_line") or 0)
        if n < 0 or n > len(lines):
            raise MemoryError_(f"Error: Invalid `insert_line` parameter: {n}. It should be within the range of lines of "
                               f"the file: [0, {len(lines)}]")
        lines[n:n] = [(cmd.get("insert_text") or "").rstrip("\n")]
        f = S.live_row(ctx.db, ws["id"], rel)
        res = S.write_file(ctx, p, ws, rel, "\n".join(lines).encode("utf-8"), None, f["current_version"], "memory: insert")
        return _saved(res, f"The file {_shown(rel)} has been edited.")

    if c == "delete":
        rel = _rel(cmd.get("path"))
        if rel == FOLDER:
            raise MemoryError_(f"Error: cannot delete the {ROOT} directory itself")
        if _is_file(ctx, ws, rel):
            res = S.delete_file(ctx, p, ws, rel, None, "memory: delete")
            return _saved(res, f"Successfully deleted {_shown(rel)}")
        if _is_dir(ctx, p, ws, rel):
            prefix = rel.rstrip("/") + "/"
            entries = S.list_files(ctx, p, ws, prefix, recursive=True)
            for e in entries:
                if e["kind"] == "file":
                    S.delete_file(ctx, p, ws, e["path"], None, "memory: delete")
            for e in sorted((e for e in entries if e["kind"] == "folder"), key=lambda e: -len(e["path"])):
                S.delete_folder(ctx, p, ws, e["path"], "memory: delete")
            if S.live_row(ctx.db, ws["id"], prefix):
                S.delete_folder(ctx, p, ws, prefix, "memory: delete")
            return f"Successfully deleted {_shown(rel)}"
        raise MemoryError_(f"Error: The path {cmd.get('path')} does not exist")

    if c == "rename":
        src, dst = _rel(cmd.get("old_path")), _rel(cmd.get("new_path"))
        if src == FOLDER:
            raise MemoryError_(f"Error: cannot rename the {ROOT} directory itself")
        src_is_dir = not _is_file(ctx, ws, src) and _is_dir(ctx, p, ws, src)
        if not src_is_dir and not _is_file(ctx, ws, src):
            raise MemoryError_(f"Error: The path {cmd.get('old_path')} does not exist")
        if dst == FOLDER or _is_file(ctx, ws, dst) or _is_dir(ctx, p, ws, dst):
            raise MemoryError_(f"Error: The destination {cmd.get('new_path')} already exists")
        if src_is_dir:
            S.move(ctx, p, ws, src.rstrip("/") + "/", dst.rstrip("/") + "/", "memory: rename")
        else:
            S.move(ctx, p, ws, src, dst, "memory: rename")
        return f"Successfully renamed {_shown(src)} to {_shown(dst)}"

    raise MemoryError_(f"Error: unknown command {c}")
