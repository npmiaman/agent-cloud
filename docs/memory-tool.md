# Claude memory tool backend

Claude's [memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)
(`memory_20250818`) is client-side: Claude asks for `view`, `create`, `str_replace`,
`insert`, `delete` or `rename` under `/memories`, and your application runs them.
Agent Cloud can run them for you. Each agent's memories then get:

- isolation by credential
- a version for every edit, so a bad edit can be undone
- provenance and trust labels
- the audit log

`/memories/x.md` is stored as `memories/x.md` in the agent's workspace. Return strings
follow the reference implementation in the memory tool docs.

## With the anthropic Python SDK

```python
import anthropic
from agentcloud.memory import AgentCloudMemoryTool

memory = AgentCloudMemoryTool(url="http://127.0.0.1:8787", token="aca_…", task="support-ticket-123")
runner = anthropic.Anthropic().beta.messages.tool_runner(
    model="claude-opus-5-5",
    max_tokens=4096,
    tools=[memory],
    messages=[{"role": "user", "content": "Remember that Acme prefers email follow-ups."}],
)
print(runner.until_done().content)
```

Errors raise the SDK's `ToolError`, so the runner returns them with `is_error: true`.
Verified against `anthropic` 2.12.

## Any language: REST

Send the `tool_use` block's `input` as the request body:

```bash
curl -X POST http://127.0.0.1:8787/v1/memory -H "Authorization: Bearer aca_…" \
  -d '{"command": "view", "path": "/memories"}'
# {"content": "Here're the files and directories up to 2 levels deep in /memories, ...", "is_error": false}
```

Put `content` in your `tool_result` and set `is_error` from the response. Pass
`?workspace=<name>` to use a workspace other than the agent's home.

## Behaviour notes

- **`create` on an existing file overwrites it.** That is the documented alternative to
  the reference implementation's error, and the old version stays in history.
- **`delete` on a directory** removes everything inside. `/memories` itself can't be
  deleted or renamed.
- **Paths are validated:** anything outside `/memories`, `..` segments, encoded traversal
  (`%2e`) and backslashes are rejected.
- **Approval policies apply.** If the agent's grant requires approval, the result says
  the change is waiting for a human.
- **File views are capped at 16,000 characters.** Claude can page through longer files with `view_range`.
