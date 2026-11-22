"""Back Claude's memory tool (`memory_20250818`) with Agent Cloud.

    from anthropic import Anthropic
    from agentcloud.memory import AgentCloudMemoryTool

    memory = AgentCloudMemoryTool(url="http://127.0.0.1:8787", token="aca_...")
    runner = Anthropic().beta.messages.tool_runner(
        model="claude-opus-5-5", max_tokens=4096, tools=[memory],
        messages=[{"role": "user", "content": "Remember that Acme prefers email follow-ups."}])
    runner.until_done()

Each agent's memories live under /memories in its own workspace: isolated by credential,
versioned (every edit can be undone), labelled with provenance and trust, and audited.

Without the anthropic SDK, use `AgentCloudMemory.execute(tool_use.input)` in your own
tool loop; it returns (text, is_error) for the tool_result block.
"""
from __future__ import annotations

import os
from typing import Any

import httpx


class AgentCloudMemory:
    """SDK-agnostic: send one memory command, get (text, is_error)."""

    def __init__(self, url: str | None = None, token: str | None = None, *, workspace: str | None = None,
                 task: str | None = None, model: str | None = None, timeout: float = 30.0,
                 http: httpx.Client | None = None):
        url = url or os.environ.get("AGENTCLOUD_URL", "http://127.0.0.1:8787")
        token = token or os.environ.get("AGENTCLOUD_TOKEN")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if task:
            headers["X-Agent-Task"] = task
        if model:
            headers["X-Agent-Model"] = model
        self.http = http or httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=timeout)
        if http is not None:
            self.http.headers.update(headers)
        self.workspace = workspace

    def execute(self, command: dict | Any) -> tuple[str, bool]:
        if hasattr(command, "model_dump"):
            command = command.model_dump(exclude_none=True)
        params = {"workspace": self.workspace} if self.workspace else None
        r = self.http.post("/v1/memory", json=dict(command), params=params)
        if r.status_code >= 400:
            try:
                msg = r.json()["error"]["message"]
            except Exception:
                msg = r.text[:300]
            return f"Error: {msg}", True
        d = r.json()
        return d["content"], bool(d["is_error"])


try:  # the anthropic SDK is optional
    from anthropic.lib.tools import BetaAbstractMemoryTool, ToolError
except Exception:  # pragma: no cover - SDK not installed
    BetaAbstractMemoryTool = None  # type: ignore[assignment,misc]

if BetaAbstractMemoryTool is not None:
    class AgentCloudMemoryTool(BetaAbstractMemoryTool):  # type: ignore[misc,valid-type]
        """Drop-in memory backend for the anthropic SDK's tool runner."""

        def __init__(self, url: str | None = None, token: str | None = None, *, workspace: str | None = None,
                     task: str | None = None, model: str | None = None, http: httpx.Client | None = None,
                     cache_control=None):
            super().__init__(cache_control=cache_control)
            self.backend = AgentCloudMemory(url, token, workspace=workspace, task=task, model=model, http=http)

        def _run(self, command):
            text, err = self.backend.execute(command)
            if err:
                raise ToolError(text)
            return text

        def view(self, command):
            return self._run(command)

        def create(self, command):
            return self._run(command)

        def str_replace(self, command):
            return self._run(command)

        def insert(self, command):
            return self._run(command)

        def delete(self, command):
            return self._run(command)

        def rename(self, command):
            return self._run(command)
else:  # pragma: no cover
    def AgentCloudMemoryTool(*a, **kw):  # type: ignore[no-redef]
        raise ImportError("pip install anthropic to use AgentCloudMemoryTool (or use AgentCloudMemory directly)")
