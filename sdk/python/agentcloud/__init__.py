"""Python SDK for Agent Cloud.

    from agentcloud import AgentCloud

    ac = AgentCloud(token=os.environ["AGENTCLOUD_TOKEN"])      # agent or user token
    ws = ac.workspace("ws_...")
    ws.write("notes/plan.md", "# Plan\n")                       # -> Version
    text = ws.read_text("notes/plan.md")
    ws.restore("notes/plan.md", version=1)

Writes that need human approval return a `Pending` instead of a `Version`; check
`result.pending` or call `ws.wait_for(result)`.
"""
from __future__ import annotations

import hashlib
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote

import httpx

__all__ = ["AgentCloud", "Workspace", "Version", "Pending", "AgentCloudError", "Conflict", "Forbidden", "NotFound", "Unauthenticated", "RateLimited"]
__version__ = "0.1.0"


class AgentCloudError(Exception):
    def __init__(self, status: int, code: str, message: str, body: dict | None = None):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message, self.body = status, code, message, body or {}


class NotFound(AgentCloudError): ...
class Forbidden(AgentCloudError): ...
class Conflict(AgentCloudError): ...
class Unauthenticated(AgentCloudError): ...
class RateLimited(AgentCloudError): ...


@dataclass
class Version:
    path: str
    version: int
    op: str
    size: int = 0
    sha256: str | None = None
    content_type: str | None = None
    pending: bool = False


@dataclass
class Pending:
    path: str
    proposal_id: str
    base_version: int
    pending: bool = True


def _raise(r: httpx.Response):
    if r.status_code < 400:
        return
    try:
        body = r.json()
    except Exception:
        body = {}
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):  # {"error": {"code", "message", "request_id", ...}} per the API spec
        err = {"code": "HTTP_ERROR", "message": r.text[:500]}
    cls = {404: NotFound, 403: Forbidden, 409: Conflict, 401: Unauthenticated, 429: RateLimited}.get(r.status_code, AgentCloudError)
    raise cls(r.status_code, err.get("code", "ERROR"), err.get("message", ""), err)


def _p(path: str) -> str:
    # HTTP clients resolve '.'/'..' in URLs before sending, which would silently
    # retarget the request. Refuse instead.
    if any(seg in (".", "..") for seg in path.split("/")):
        raise ValueError(f"'.' and '..' are not allowed in workspace paths: {path!r}")
    return quote(path.lstrip("/"), safe="/")


def _result(d: dict) -> Version | Pending:
    if d.get("status") == "pending_approval":
        return Pending(d["path"], d["proposal_id"], d["base_version"])
    return Version(d["path"], d["version"], d["op"], d.get("size", 0), d.get("sha256"), d.get("content_type"))


class AgentCloud:
    def __init__(self, token: str | None = None, base_url: str | None = None, timeout: float = 60, *,
                 api_key: str | None = None, exchange: bool = True, organisation: str | None = None):
        """`token`/`api_key`: a user API key (acu_…) or an agent credential (aca_…).
        With an agent credential the SDK exchanges it for short-lived access tokens and
        refreshes them automatically (exchange=False sends the credential directly)."""
        self.base_url = (base_url or os.environ.get("AGENTCLOUD_URL", "http://127.0.0.1:8787")).rstrip("/")
        self.http = httpx.Client(base_url=self.base_url, timeout=timeout)
        self._setup(token or api_key or os.environ.get("AGENTCLOUD_TOKEN"), exchange, organisation)

    def _setup(self, token, exchange, organisation=None):
        self.token = token
        self._exchange = bool(exchange and token and token.startswith("aca_"))
        self._access: tuple[str, float] | None = None
        self.exchanges = 0
        if organisation:
            self.http.headers["X-Organisation"] = organisation
        if token and not self._exchange:
            self.http.headers["Authorization"] = f"Bearer {token}"

    # Allows tests to inject an ASGI/WSGI transport client.
    @classmethod
    def from_client(cls, client: httpx.Client, token: str | None = None, exchange: bool = False) -> "AgentCloud":
        self = cls.__new__(cls)
        self.base_url, self.http = str(client.base_url), client
        self._setup(token, exchange)
        return self

    def _access_header(self, force: bool = False) -> str:
        if not self._exchange:
            return self.http.headers.get("Authorization", "")
        if force or not self._access or self._access[1] - time.time() < 60:
            r = self.http.post("/v1/auth/agent/token", headers={"Authorization": f"Bearer {self.token}"})
            _raise(r)
            d = r.json()
            self._access = (d["access_token"], d["expires_at"])
            self.exchanges += 1
        return f"Bearer {self._access[0]}"

    def _req(self, method: str, url: str, *, idempotent_retry: bool = False, **kw) -> httpx.Response:
        """Send a request. 401 with an exchanged token -> refresh once and resend (the server
        rejected it before acting, so this is safe for any method). Network errors are
        retried only for requests carrying an Idempotency-Key."""
        headers = dict(kw.pop("headers", None) or {})
        if self._exchange:
            headers["Authorization"] = self._access_header()
        attempts = 3 if idempotent_retry else 1
        if idempotent_retry:
            headers.setdefault("Idempotency-Key", "sdk-" + uuid.uuid4().hex)
        for i in range(attempts):
            try:
                r = self.http.request(method, url, headers=headers, **kw)
            except httpx.TransportError:
                if i == attempts - 1:
                    raise
                time.sleep(0.5 * (2 ** i))
                continue
            if r.status_code == 401 and self._exchange:
                headers["Authorization"] = self._access_header(force=True)
                r = self.http.request(method, url, headers=headers, **kw)
            break
        _raise(r)
        return r

    def _json(self, method: str, url: str, **kw) -> Any:
        return self._req(method, url, **kw).json()

    # -------- account (user tokens)
    @classmethod
    def signup(cls, email: str, password: str, base_url: str | None = None) -> "AgentCloud":
        c = cls(base_url=base_url)
        d = c._json("POST", "/v1/auth/signup", json={"email": email, "password": password})
        return cls(d["token"], c.base_url)

    @classmethod
    def login(cls, email: str, password: str, base_url: str | None = None) -> "AgentCloud":
        c = cls(base_url=base_url)
        d = c._json("POST", "/v1/auth/login", json={"email": email, "password": password})
        return cls(d["token"], c.base_url)

    def me(self) -> dict:
        return self._json("GET", "/v1/me")

    def create_api_key(self, name: str = "default") -> dict:
        return self._json("POST", "/v1/api-keys", json={"name": name})

    # -------- workspaces
    def create_workspace(self, name: str, description: str = "", quota_bytes: int | None = None,
                         project_id: str | None = None, retention_days: int | None = None) -> "Workspace":
        d = self._json("POST", "/v1/workspaces", json={"name": name, "description": description, "quota_bytes": quota_bytes,
                                                       "project_id": project_id, "retention_days": retention_days})
        return Workspace(self, d["id"], d)

    @property
    def workspaces(self) -> "Workspaces":
        """client.workspaces.get(id) / .list() / .create(...); calling it lists (v0 style)."""
        return Workspaces(self)

    def workspace(self, workspace_id: str) -> "Workspace":
        return Workspace(self, workspace_id)

    # -------- agents
    def create_agent(self, name: str, description: str = "") -> dict:
        return self._json("POST", "/v1/agents", json={"name": name, "description": description})

    def provision_agent(self, name: str, *, storage_mb: int | None = None, access: str = "read_write",
                        approval: str = "none", description: str = "") -> dict:
        """Create an agent with its own workspace, storage, permissions and a token, in one call.
        Returns {"agent", "workspace", "token"}; the token is only shown once."""
        body = {"name": name, "description": description, "access": access, "approval": approval}
        if storage_mb:
            body["storage_mb"] = storage_mb
        return self._json("POST", "/v1/agents/provision", json=body)

    @property
    def agents(self) -> "Agents":
        """client.agents.create(...) / .list() / .get(id); calling it lists (v0 style)."""
        return Agents(self)

    def create_agent_token(self, agent_id: str, name: str = "default", ttl_seconds: int | None = None) -> str:
        return self._json("POST", f"/v1/agents/{agent_id}/tokens", json={"name": name, "ttl_seconds": ttl_seconds})["token"]

    def revoke_agent_token(self, agent_id: str, token_id: str) -> None:
        self._req("DELETE", f"/v1/agents/{agent_id}/tokens/{token_id}")

    def delete_agent(self, agent_id: str) -> None:
        self._req("DELETE", f"/v1/agents/{agent_id}")

    def activity(self, agent_id: str | None = None, limit: int = 100) -> list[dict]:
        """Owner-wide activity; with agent_id, that agent's full history."""
        params: dict = {"limit": limit}
        if agent_id:
            params["agent_id"] = agent_id
        return self._json("GET", "/v1/activity", params=params)

    # -------- approvals (user)
    def pending(self) -> list[dict]:
        return self._json("GET", "/v1/proposals")

    def proposal(self, proposal_id: str) -> dict:
        return self._json("GET", f"/v1/proposals/{proposal_id}")

    def approve(self, proposal_id: str, note: str = "", force: bool = False) -> dict:
        return self._json("POST", f"/v1/proposals/{proposal_id}/approve", json={"note": note, "force": force})

    def reject(self, proposal_id: str, note: str = "") -> dict:
        return self._json("POST", f"/v1/proposals/{proposal_id}/reject", json={"note": note})


class Workspace:
    def __init__(self, client: AgentCloud, workspace_id: str, info: dict | None = None):
        self.c, self.id, self.info = client, workspace_id, info or {}

    def __repr__(self):
        return f"Workspace({self.id!r}, name={self.info.get('name')!r})"

    def _u(self, rest: str) -> str:
        return f"/v1/workspaces/{self.id}/{rest}"

    def refresh(self) -> dict:
        self.info = self.c._json("GET", f"/v1/workspaces/{self.id}")
        return self.info

    # -------- files
    def write(self, path: str, data: bytes | str | BinaryIO, *, content_type: str | None = None,
              if_version: int | None = None, message: str = "") -> Version | Pending:
        """Create or update a file. `if_version` makes it conditional: the write fails
        with Conflict unless the file is currently at that version (0 = must not exist)."""
        if isinstance(data, str):
            data = data.encode()
            content_type = content_type or ("text/plain; charset=utf-8" if "." not in path.rsplit("/", 1)[-1] else None)
        elif not isinstance(data, (bytes, bytearray)):
            data = data.read()
        h = {}
        if content_type:
            h["Content-Type"] = content_type
        if if_version is not None:
            h["If-Match"] = str(if_version)
        if message:
            h["X-Message"] = message
        return _result(self.c._json("PUT", self._u(f"files/{_p(path)}"), content=bytes(data), headers=h))

    def edit(self, path: str, edits: list[dict], *, message: str = "", if_version: int | None = None) -> Version | Pending:
        """Exact-text replacements, e.g. ws.edit("plan.md", [{"old": "draft", "new": "final"}])."""
        return _result(self.c._json("POST", self._u(f"edit/{_p(path)}"), json={"edits": edits, "message": message, "if_version": if_version}))

    def append(self, path: str, text: str, *, message: str = "") -> Version | Pending:
        return _result(self.c._json("POST", self._u(f"append/{_p(path)}"), json={"content": text, "message": message}))

    def read_lines(self, path: str, offset: int = 1, limit: int | None = None, line_numbers: bool = False) -> str:
        params = {"offset": offset, "line_numbers": line_numbers}
        if limit:
            params["limit"] = limit
        return self.c._req("GET", self._u(f"files/{_p(path)}"), params=params).text

    def upload(self, local_path: str | os.PathLike, path: str | None = None, **kw) -> Version | Pending:
        lp = Path(local_path)
        return self.write(path or lp.name, lp.read_bytes(), **kw)

    def read(self, path: str, version: int | None = None) -> bytes:
        params = {"version": version} if version is not None else None
        return self.c._req("GET", self._u(f"files/{_p(path)}"), params=params).content

    def read_text(self, path: str, version: int | None = None, encoding: str = "utf-8") -> str:
        return self.read(path, version).decode(encoding)

    def download(self, path: str, local_path: str | os.PathLike, version: int | None = None) -> Path:
        lp = Path(local_path)
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_bytes(self.read(path, version))
        return lp

    def exists(self, path: str) -> bool:
        try:
            return not self.stat(path)["deleted"]
        except NotFound:
            return False

    def stat(self, path: str) -> dict:
        return self.c._json("GET", self._u(f"meta/{_p(path)}"))

    def list(self, prefix: str = "", recursive: bool = False, query: str | None = None,
             include_deleted: bool = False) -> list[dict]:
        params = {"prefix": prefix, "recursive": recursive, "include_deleted": include_deleted}
        if query:
            params["q"] = query
        return self.c._json("GET", self._u("files"), params=params)["entries"]

    def delete(self, path: str, *, if_version: int | None = None, message: str = "") -> Version | Pending | dict:
        h = {"If-Match": str(if_version)} if if_version is not None else {}
        if message:
            h["X-Message"] = message
        d = self.c._json("DELETE", self._u(f"files/{_p(path)}"), headers=h)
        return _result(d) if ("version" in d or "proposal_id" in d) else d

    def mkdir(self, path: str) -> dict:
        return self.c._json("POST", self._u("folders"), json={"path": path})

    def move(self, src: str, dst: str, message: str = "") -> dict:
        return self.c._json("POST", self._u("move"), json={"from": src, "to": dst, "message": message})

    # -------- versions
    def versions(self, path: str) -> list[dict]:
        return self.c._json("GET", self._u(f"versions/{_p(path)}"))["versions"]

    def restore(self, path: str, version: int, message: str = "") -> Version | Pending:
        return _result(self.c._json("POST", self._u(f"restore/{_p(path)}"), json={"version": version, "message": message}))

    def diff(self, path: str, from_version: int, to_version: int | None = None) -> dict:
        params = {"from": from_version}
        if to_version is not None:
            params["to"] = to_version
        return self.c._json("GET", self._u(f"diff/{_p(path)}"), params=params)

    # -------- sharing
    def grant(self, agent_id: str, path: str = "", *, read: bool = True, write: bool = False, delete: bool = False,
              share: bool = False, approval: str = "none", ttl_seconds: int | None = None) -> dict:
        """Give an agent access to the workspace ('' ), a folder ('reports/') or a file."""
        return self.c._json("POST", self._u("grants"), json={
            "agent_id": agent_id, "path": path, "read": read, "write": write, "delete": delete,
            "share": share, "approval": approval, "ttl_seconds": ttl_seconds})

    share = grant

    def grants(self) -> list[dict]:
        return self.c._json("GET", self._u("grants"))

    def revoke(self, grant_id: str) -> None:
        self.c._req("DELETE", self._u(f"grants/{grant_id}"))

    # -------- human handoff (owner only)
    def link(self, path: str, *, version: int | None = None, ttl_hours: float = 72,
             max_downloads: int | None = None) -> dict:
        """Expiring download link for someone without an account. Pinned to a version.
        The result's "url" is shown once."""
        d = self.c._json("POST", self._u("links"), json={"path": path, "version": version, "ttl_hours": ttl_hours,
                                                         "max_downloads": max_downloads})
        d["url"] = self.c.base_url.rstrip("/") + d["url_path"]
        return d

    def links(self) -> list[dict]:
        return self.c._json("GET", self._u("links"))

    def revoke_link(self, link_id: str) -> None:
        self.c._req("DELETE", self._u(f"links/{link_id}"))

    # -------- approvals / activity
    def proposals(self, status: str = "pending") -> list[dict]:
        return self.c._json("GET", self._u("proposals"), params={"status": status})

    def proposal_status(self, pending: Pending | str) -> str:
        pid = pending.proposal_id if isinstance(pending, Pending) else pending
        return self.c.proposal(pid)["status"]

    def wait_for(self, pending: Pending, timeout: float = 3600, poll: float = 5) -> str:
        """Block until a human approves or rejects. Returns 'approved' or 'rejected'."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = self.proposal_status(pending)
            if s != "pending":
                return s
            time.sleep(poll)
        raise TimeoutError(f"proposal {pending.proposal_id} still pending after {timeout}s")

    def activity(self, limit: int = 100, path: str | None = None) -> list[dict]:
        params: dict = {"limit": limit}
        if path:
            params["path"] = path
        return self.c._json("GET", self._u("activity"), params=params)

    # -------- sync helpers: an agent's local scratch dir <-> its workspace
    def push_dir(self, local_dir: str | os.PathLike, prefix: str = "", message: str = "") -> list[Version | Pending]:
        """Upload every file under local_dir. Unchanged files don't create new versions."""
        root = Path(local_dir)
        out = []
        for fp in sorted(root.rglob("*")):
            if fp.is_file():
                rel = fp.relative_to(root).as_posix()
                out.append(self.write(prefix + rel, fp.read_bytes(), message=message))
        return out

    def pull_dir(self, local_dir: str | os.PathLike, prefix: str = "") -> list[Path]:
        """Download every readable file under prefix into local_dir."""
        root = Path(local_dir).resolve()
        out = []
        for e in self.list(prefix, recursive=True):
            if e["kind"] != "file":
                continue
            dest = (root / e["path"][len(prefix):]).resolve()
            if root not in dest.parents:
                raise AgentCloudError(0, "unsafe_path", f"refusing to write outside {root}: {e['path']}")
            out.append(self.download(e["path"], dest))
        return out


# ---------------------------------------------------------------- spec-shaped namespaces (§12.1)

class Workspaces:
    def __init__(self, c: AgentCloud):
        self.c = c

    def __call__(self) -> list[Workspace]:
        return self.list()

    def __iter__(self):
        return iter(self.list())

    def __len__(self):
        return len(self.list())

    def __getitem__(self, i):
        return self.list()[i]

    def list(self, project_id: str | None = None) -> list[Workspace]:
        params = {"project_id": project_id} if project_id else None
        return [Workspace(self.c, d["id"], d) for d in self.c._json("GET", "/v1/workspaces", params=params)]

    def get(self, workspace_id: str) -> Workspace:
        return Workspace(self.c, workspace_id, self.c._json("GET", f"/v1/workspaces/{workspace_id}"))

    def create(self, name: str, *, storage_limit_bytes: int | None = None, project_id: str | None = None,
               description: str = "", retention_days: int | None = None) -> Workspace:
        return self.c.create_workspace(name, description, storage_limit_bytes, project_id, retention_days)


class Agents:
    def __init__(self, c: AgentCloud):
        self.c = c

    def __call__(self) -> list[dict]:
        return self.list()

    def __iter__(self):
        return iter(self.list())

    def __len__(self):
        return len(self.list())

    def __getitem__(self, i):
        return self.list()[i]

    def list(self, project_id: str | None = None) -> list[dict]:
        return self.c._json("GET", "/v1/agents", params={"project_id": project_id} if project_id else None)

    def get(self, agent_id: str) -> dict:
        return self.c._json("GET", f"/v1/agents/{agent_id}")

    def create(self, name: str, *, storage: str | int | None = None, project_id: str | None = None, description: str = "",
               access: str = "read_write", approval: str = "none", metadata: dict | None = None) -> dict:
        """Create an agent. With `storage` (e.g. "2GB" or bytes) it is provisioned with its own
        workspace and a credential, as in the spec's quickstart."""
        if storage is None:
            return self.c._json("POST", "/v1/agents", json={"name": name, "description": description,
                                                             "project_id": project_id, "metadata": metadata or {}})
        mb = max(1, _parse_size(storage) // (1024 * 1024))
        out = self.c._json("POST", "/v1/agents/provision", json={"name": name, "description": description, "project_id": project_id,
                                                                   "storage_mb": mb, "access": access, "approval": approval})
        return {**out["agent"], "agent_id": out["agent"]["id"], "workspace_id": out["workspace"]["id"],
                "credential": out["credential"], "connect": out["connect"], "workspace": out["workspace"]}

    def delete(self, agent_id: str) -> None:
        self.c._req("DELETE", f"/v1/agents/{agent_id}")

    def credentials(self, agent_id: str) -> list[dict]:
        return self.c._json("GET", f"/v1/agents/{agent_id}/credentials")

    def create_credential(self, agent_id: str, *, scopes: list[str] | None = None, ttl_seconds: int | None = None,
                          name: str = "default") -> dict:
        return self.c._json("POST", f"/v1/agents/{agent_id}/credentials", json={"name": name, "scopes": scopes, "ttl_seconds": ttl_seconds})

    def rotate_credential(self, agent_id: str, credential_id: str, grace_seconds: int = 3600) -> dict:
        return self.c._json("POST", f"/v1/agents/{agent_id}/credentials/{credential_id}/rotate", json={"grace_seconds": grace_seconds})

    def revoke_credential(self, agent_id: str, credential_id: str) -> None:
        self.c._req("DELETE", f"/v1/agents/{agent_id}/credentials/{credential_id}")

    def activity(self, agent_id: str, limit: int = 100) -> list[dict]:
        return self.c._json("GET", f"/v1/agents/{agent_id}/activity", params={"limit": limit})


def _parse_size(v: str | int) -> int:
    if isinstance(v, int):
        return v
    import re as _re
    m = _re.fullmatch(r"\s*([\d.]+)\s*([KMGT]?B?)\s*", v.upper())
    if not m:
        raise ValueError(f"can't parse size {v!r}; use e.g. '500MB' or '2GB'")
    mult = {"": 1, "B": 1, "K": 1024, "KB": 1024, "M": 1024**2, "MB": 1024**2, "G": 1024**3, "GB": 1024**3, "T": 1024**4, "TB": 1024**4}
    return int(float(m.group(1)) * mult[m.group(2)])


@dataclass
class FileRef:
    id: str
    path: str
    version: int
    size: int = 0
    sha256: str | None = None
    pending: bool = False
    proposal_id: str | None = None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class Files:
    """workspace.files: the spec's file API, by stable file id where it matters."""

    def __init__(self, ws: Workspace):
        self.ws, self.c = ws, ws.c

    def upload(self, path: str, local_path: str | os.PathLike | None = None, *, data: bytes | str | None = None,
               content_type: str | None = None, if_version: int | None = None, message: str = "") -> FileRef:
        """Upload through a verified upload session: reserve quota, PUT to a signed URL,
        complete with a SHA-256 the server checks. Streams from disk; any size."""
        if (local_path is None) == (data is None):
            raise ValueError("pass exactly one of local_path or data")
        if data is not None:
            raw = data.encode() if isinstance(data, str) else bytes(data)
            size, sha, body = len(raw), hashlib.sha256(raw).hexdigest(), raw
        else:
            lp = Path(local_path)
            size, sha, body = lp.stat().st_size, _sha256_file(lp), None
        start = self.c._json("POST", f"/v1/workspaces/{self.ws.id}/uploads", idempotent_retry=True, json={
            "path": path.lstrip("/"), "size_bytes": size, "content_type": content_type, "checksum_sha256": sha,
            "if_version": if_version, "message": message})
        if body is None:
            with open(local_path, "rb") as f:
                r = self.c.http.put(start["upload_url"], content=iter(lambda: f.read(1024 * 1024), b""),
                                    headers={"Content-Length": str(size)})
        else:
            r = self.c.http.put(start["upload_url"], content=body)
        _raise(r)
        done = self.c._json("POST", f"/v1/uploads/{start['upload_id']}/complete", idempotent_retry=True,
                            json={"checksum_sha256": sha})
        res = done["result"]
        if res.get("status") == "pending_approval":
            return FileRef(id="", path=res["path"], version=res["base_version"], size=size, sha256=sha,
                           pending=True, proposal_id=res["proposal_id"])
        return FileRef(id=res["id"], path=res["path"], version=res["version"], size=size, sha256=sha)

    def list(self, path: str = "", *, recursive: bool = False, search: str | None = None, ext: str | None = None,
             created_by: str | None = None, modified_after: float | None = None, modified_before: float | None = None,
             include_deleted: bool = False) -> list[dict]:
        """All matching entries; follows the server's cursor pagination."""
        params: dict = {"path": path.lstrip("/"), "recursive": recursive, "include_deleted": include_deleted, "limit": 1000}
        for k, v in (("search", search), ("ext", ext), ("created_by", created_by),
                     ("modified_after", modified_after), ("modified_before", modified_before)):
            if v is not None:
                params[k] = v
        out, cursor = [], None
        while True:
            if cursor:
                params["cursor"] = cursor
            d = self.c._json("GET", f"/v1/workspaces/{self.ws.id}/files", params=params)
            out += d["entries"]
            cursor = d.get("next_cursor")
            if not cursor:
                return out

    def get(self, file_id: str) -> dict:
        return self.c._json("GET", f"/v1/files/{file_id}")

    def id_of(self, path: str) -> str:
        return self.ws.stat(path)["id"]

    def read(self, file_id: str, version: int | None = None) -> bytes:
        return self.c._req("GET", f"/v1/files/{file_id}/content", params={"version": version} if version else None).content

    def download(self, file_id: str, destination: str | os.PathLike, version: int | None = None, verify: bool = True) -> Path:
        """Fetch a signed URL, stream it to disk, and check the SHA-256."""
        d = self.c._json("GET", f"/v1/files/{file_id}/download", params={"version": version} if version else None)
        dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256()
        with self.c.http.stream("GET", d["url"]) as r:
            _raise(r) if r.status_code >= 400 else None
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes():
                    h.update(chunk)
                    f.write(chunk)
        if verify and h.hexdigest() != d["checksum_sha256"]:
            dest.unlink(missing_ok=True)
            raise AgentCloudError(0, "CHECKSUM_MISMATCH", "downloaded bytes don't match the server's checksum")
        return dest

    def versions(self, file_id: str) -> list[dict]:
        return self.c._json("GET", f"/v1/files/{file_id}/versions")["versions"]

    def restore(self, file_id: str, version_id: str | None = None, *, version: int | None = None, message: str = "") -> dict:
        return self.c._json("POST", f"/v1/files/{file_id}/restore", json={"version_id": version_id, "version": version, "message": message})

    def move(self, file_id: str, path: str) -> dict:
        return self.c._json("PATCH", f"/v1/files/{file_id}", json={"path": path})

    def rename(self, file_id: str, name: str) -> dict:
        return self.c._json("PATCH", f"/v1/files/{file_id}", json={"name": name})

    def copy(self, file_id: str, path: str) -> dict:
        return self.c._json("POST", f"/v1/files/{file_id}/copy", json={"path": path})

    def delete(self, file_id: str) -> dict:
        return self.c._json("DELETE", f"/v1/files/{file_id}")

    def share(self, file_id: str, recipient_agent_id: str, permission: str = "read", ttl_seconds: int | None = None) -> dict:
        return self.c._json("POST", f"/v1/files/{file_id}/shares", json={"recipient_agent_id": recipient_agent_id,
                                                                         "permission": permission, "ttl_seconds": ttl_seconds})

    def propose(self, file_id: str, data: bytes | str, message: str = "") -> dict:
        raw = data.encode() if isinstance(data, str) else data
        return self.c._json("POST", f"/v1/files/{file_id}/changes", content=raw, headers={"X-Message": message} if message else None)

    def activity(self, file_id: str) -> list[dict]:
        return self.c._json("GET", f"/v1/files/{file_id}/activity")


def _ws_files(self) -> Files:
    return Files(self)


def _ws_members(self) -> list[dict]:
    return self.c._json("GET", self._u("members"))


def _ws_set_member(self, agent_id: str, role: str, approval: str = "none") -> dict:
    """Assign an agent to this workspace: owner/admin/editor/viewer/restricted."""
    return self.c._json("POST", self._u("members"), json={"agent_id": agent_id, "role": role, "approval": approval})


def _ws_remove_member(self, agent_id: str) -> None:
    self.c._req("DELETE", self._u(f"members/{agent_id}"))


def _ws_deny(self, agent_id: str, path: str, *, read: bool = True, write: bool = True, delete: bool = True,
             share: bool = True) -> dict:
    """A deny rule: beats any broader allow for this agent under `path`."""
    return self.c._json("POST", self._u("grants"), json={"agent_id": agent_id, "path": path, "effect": "deny",
                                                       "read": read, "write": write, "delete": delete, "share": share})


Workspace.files = property(_ws_files)
Workspace.members = _ws_members
Workspace.set_member = _ws_set_member
Workspace.remove_member = _ws_remove_member
Workspace.deny = _ws_deny


def _projects(self) -> list[dict]:
    return self._json("GET", "/v1/projects")


def _create_project(self, name: str, description: str = "") -> dict:
    return self._json("POST", "/v1/projects", json={"name": name, "description": description})


AgentCloud.projects = _projects
AgentCloud.create_project = _create_project
AgentCloud.environment = lambda self: self._json("GET", "/v1/environment")
AgentCloud.search = lambda self, q, workspace=None, limit=20: self._json(
    "GET", "/v1/search", params={"q": q, "limit": limit, **({"workspace": workspace} if workspace else {})})["results"]
AgentCloud.storage = lambda self: self._json("GET", "/v1/storage")
AgentCloud.changes = lambda self, hours=24: self._json("GET", "/v1/changes", params={"hours": hours})
AgentCloud.organisation = lambda self: self._json("GET", "/v1/organisation")
AgentCloud.add_org_member = lambda self, email, role="member": self._json("POST", "/v1/organisation/members",
                                                                           json={"email": email, "role": role})

__all__ += ["Workspaces", "Agents", "Files", "FileRef"]
