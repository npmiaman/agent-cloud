"""Stdio bridge for MCP clients that can only launch a local process.

    {"command": "python", "args": ["-m", "agentcloud.mcp_stdio"],
     "env": {"AGENTCLOUD_URL": "...", "AGENTCLOUD_TOKEN": "aca_..."}}

Each newline-delimited JSON-RPC message on stdin is forwarded to the server's /mcp
endpoint; responses are written to stdout. All logic lives server-side, so stdio and
HTTP clients see identical tools and permissions.
"""
from __future__ import annotations

import json
import os
import sys

import httpx


def _err(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def main() -> int:
    url = os.environ.get("AGENTCLOUD_URL", "http://127.0.0.1:8787").rstrip("/") + "/mcp"
    token = os.environ.get("AGENTCLOUD_TOKEN")
    if not token:
        print("agentcloud.mcp_stdio: set AGENTCLOUD_TOKEN", file=sys.stderr)
        return 1
    client = httpx.Client(timeout=120, headers={"Authorization": f"Bearer {token}",
                                                "Accept": "application/json, text/event-stream"})
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out.write(json.dumps(_err(None, -32700, "parse error")) + "\n"); out.flush()
            continue
        ids = [m.get("id") for m in (msg if isinstance(msg, list) else [msg]) if isinstance(m, dict) and "id" in m]
        try:
            r = client.post(url, content=line.encode(), headers={"Content-Type": "application/json"})
        except httpx.HTTPError as e:
            for i in ids:
                out.write(json.dumps(_err(i, -32000, f"can't reach Agent Cloud at {url}: {e}")) + "\n")
            out.flush()
            continue
        if r.status_code == 202 or not r.content:
            continue
        try:
            body = r.json()
        except ValueError:
            body = None
        if body is None:
            for i in ids:
                out.write(json.dumps(_err(i, -32000, f"HTTP {r.status_code} from Agent Cloud")) + "\n")
        elif isinstance(body, dict) and body.get("id") is None and "error" in body and ids:
            for i in ids:  # transport-level error (e.g. 401): attach to each pending request
                out.write(json.dumps({**body, "id": i}) + "\n")
        else:
            out.write(json.dumps(body) + "\n")
        out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
