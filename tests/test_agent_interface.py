"""The agent interface: MCP over HTTP, environment discovery, search, share links, CLI."""
import base64
import io
import json
import time
from contextlib import redirect_stdout

import pytest

from agentcloud import AgentCloudError, Forbidden


class MCP:
    """Minimal MCP client speaking Streamable HTTP JSON-RPC to /mcp."""

    def __init__(self, server, token):
        self.http = server.client().http
        self.token, self.n = token, 0

    def rpc(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.n += 1
            msg["id"] = self.n
        r = self.http.post("/mcp", json=msg, headers={"Authorization": f"Bearer {self.token}",
                                                      "Accept": "application/json, text/event-stream"})
        return r

    def init(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "test", "version": "0"}})
        assert r.status_code == 200, r.text
        assert self.rpc("notifications/initialized", notify=True).status_code == 202
        return r.json()["result"]

    def tools(self):
        return [t["name"] for t in self.rpc("tools/list").json()["result"]["tools"]]

    def call(self, name, **args):
        res = self.rpc("tools/call", {"name": name, "arguments": args}).json()["result"]
        return res, res["content"][0]["text"]


@pytest.fixture
def env(server):
    dev = server.signup()
    research = dev.provision_agent("research-agent", storage_mb=50, access="full")
    analyst = dev.provision_agent("analysis-agent", access="read_write")
    reader = dev.provision_agent("reader-agent", access="read_only")
    return dev, research, analyst, reader


def test_mcp_handshake_and_one_credential_flow(server, env):
    dev, research, *_ = env
    m = MCP(server, research["token"]["token"])
    info = m.init()
    assert info["protocolVersion"] == "2025-06-18" and info["capabilities"]["tools"]
    assert "research-agent" in info["instructions"] and "can read, write, delete, share" in info["instructions"]
    assert set(m.tools()) >= {"cloud_whoami", "cloud_list", "cloud_read", "cloud_write", "cloud_search", "cloud_mkdir",
                              "cloud_share", "cloud_history", "cloud_restore"}
    assert m.rpc("ping").json()["result"] == {}
    # Discovery: identity, home workspace, storage, capabilities
    res, text = m.call("cloud_whoami")
    e = res["structuredContent"]
    assert e["identity"]["name"] == "research-agent"
    home = e["workspaces"][0]
    assert home["role"] == "home" and home["storage_limit_bytes"] == 50 * 1024 * 1024
    assert home["capabilities"] == {"read": True, "write": True, "delete": True, "share": True, "restore": True,
                                    "paths": ["/"], "approval": "none", "expires_at": None}
    assert "research-agent" in text
    # Filesystem-like paths, no workspace id needed
    _, t = m.call("cloud_mkdir", path="/research")
    _, t = m.call("cloud_write", path="/research/market-analysis.md", content="# Competitor analysis\nAcme leads SMB.\n")
    assert "v1" in t
    res, t = m.call("cloud_read", path="/research/market-analysis.md")
    assert t == "# Competitor analysis\nAcme leads SMB.\n" and not res["isError"]
    _, t = m.call("cloud_list", path="/research/")
    assert "/research/market-analysis.md" in t
    # Same file through REST/SDK: one store, many interfaces
    ws = dev.workspace(research["workspace"]["id"])
    assert ws.read_text("research/market-analysis.md").startswith("# Competitor analysis")


def test_history_restore_and_conflicts_via_mcp(server, env):
    dev, research, *_ = env
    m = MCP(server, research["token"]["token"]); m.init()
    m.call("cloud_write", path="/notes.md", content="good")
    m.call("cloud_write", path="/notes.md", content="bad", message="oops")
    _, t = m.call("cloud_history", path="/notes.md")
    assert "v2" in t and "v1" in t and '"oops"' in t and "by you" in t
    _, t = m.call("cloud_restore", path="/notes.md", version=1)
    assert "now v3" in t
    assert m.call("cloud_read", path="/notes.md")[1] == "good"
    res, t = m.call("cloud_write", path="/notes.md", content="x", if_version=1)
    assert res["isError"] and "version 3" in t


def test_tools_tailored_to_permissions_and_errors_guide_recovery(server, env):
    dev, research, analyst, reader = env
    rd = MCP(server, reader["token"]["token"]); rd.init()
    tools = rd.tools()
    assert "cloud_read" in tools and "cloud_write" not in tools and "cloud_delete" not in tools and "cloud_share" not in tools
    res = rd.rpc("tools/call", {"name": "cloud_write", "arguments": {"path": "/x", "content": "y"}}).json()["result"]
    assert res["isError"]
    an = MCP(server, analyst["token"]["token"]); an.init()
    assert "cloud_delete" not in an.tools() and "cloud_write" in an.tools()
    # Scoped agent refused outside its folder is told where it *can* work
    ws = dev.workspace(research["workspace"]["id"])
    ws.write("raw/data.csv", "a\n1\n"); ws.write("secret/pay.csv", "x")
    dev.workspace(research["workspace"]["id"]).grant(reader["agent"]["id"], "raw/", read=True)
    res, t = rd.call("cloud_read", path="/secret/pay.csv", workspace="research-agent")
    assert res["isError"] and "/raw/" in t


def test_agent_to_agent_share_via_mcp(server, env):
    dev, research, analyst, reader = env
    r = MCP(server, research["token"]["token"]); r.init()
    a = MCP(server, analyst["token"]["token"]); a.init()
    r.call("cloud_write", path="/research/companies.csv", content="company,arr\nAcme,12\n")
    _, t = r.call("cloud_share", agent="analysis-agent", path="/research/companies.csv")
    assert "read only" in t
    # The analyst now sees two workspaces; whoami says which is home
    e = a.call("cloud_whoami")[0]["structuredContent"]
    roles = {w["name"]: w["role"] for w in e["workspaces"]}
    assert roles == {"analysis-agent": "home", "research-agent": "shared with you"}
    assert a.call("cloud_read", path="/research/companies.csv", workspace="research-agent")[1].startswith("company,arr")
    res, _ = a.call("cloud_write", path="/research/companies.csv", content="x", workspace="research-agent")
    assert res["isError"]
    # Bare paths still go to its own home workspace
    a.call("cloud_write", path="/report.md", content="Acme ARR 12")
    assert dev.workspace(analyst["workspace"]["id"]).read_text("report.md") == "Acme ARR 12"
    # Analyst (no share right) can't share onward
    assert "cloud_share" not in a.tools()


def test_search_finds_content_across_workspaces(server, env):
    dev, research, analyst, reader = env
    r = MCP(server, research["token"]["token"]); r.init()
    r.call("cloud_write", path="/research/q3.md", content="Detailed competitor analysis of the CRM market.\nAcme vs Globex.")
    r.call("cloud_write", path="/research/hiring.md", content="Hiring plan")
    r.call("cloud_write", path="/competitors/list.csv", content="name\nAcme\n")
    r.call("cloud_share", agent="analysis-agent", path="/research/")
    a = MCP(server, analyst["token"]["token"]); a.init()
    a.call("cloud_write", path="/notes/analysis.md", content="Our competitor analysis says Globex is slowing.")
    res, t = a.call("cloud_search", query="competitor analysis")
    paths = [h["path"] for h in res["structuredContent"]["results"]]
    assert set(paths) == {"/research/q3.md", "/notes/analysis.md"}      # content hits in both workspaces
    assert "/competitors/list.csv" not in paths                          # not shared with the analyst
    assert any("competitor analysis" in (h["snippet"] or "").lower() for h in res["structuredContent"]["results"])
    res, _ = r.call("cloud_search", query="competitors")
    assert [h["path"] for h in res["structuredContent"]["results"]] == ["/competitors/list.csv"]  # name hit
    assert "No files match" in a.call("cloud_search", query="zebra")[1]


def test_binary_round_trip_via_mcp(server, env):
    dev, research, *_ = env
    m = MCP(server, research["token"]["token"]); m.init()
    blob = bytes(range(256)) * 4
    m.call("cloud_write", path="/img/raw.bin", content=base64.b64encode(blob).decode(), encoding="base64")
    res, t = m.call("cloud_read", path="/img/raw.bin")
    assert res["structuredContent"]["encoding"] == "base64"
    assert base64.b64decode(t.split("]\n", 1)[1]) == blob


def test_approval_surfaces_to_agent(server):
    dev = server.signup()
    ag = dev.provision_agent("careful", access="read_write", approval="overwrite")
    m = MCP(server, ag["token"]["token"]); m.init()
    m.call("cloud_write", path="/db.csv", content="a")
    res, t = m.call("cloud_write", path="/db.csv", content="b")
    assert "waiting for human approval" in t and not res["isError"]
    assert m.call("cloud_read", path="/db.csv")[1] == "a"


def test_mcp_auth_and_protocol_errors(server, env):
    m = MCP(server, "aca_not-a-real-token")
    r = m.rpc("initialize", {"protocolVersion": "2025-06-18"})
    assert r.status_code == 401 and "WWW-Authenticate" in r.headers
    dev, research, *_ = env
    m = MCP(server, research["token"]["token"])
    assert m.rpc("no/such").json()["error"]["code"] == -32601
    assert m.rpc("tools/call", {"name": "cloud_nope", "arguments": {}}).json()["error"]["code"] == -32602
    old = m.rpc("initialize", {"protocolVersion": "2024-11-05"}).json()["result"]
    assert old["protocolVersion"] == "2024-11-05"
    bad = m.http.post("/mcp", content=b"{nope", headers={"Authorization": f"Bearer {research['token']['token']}"})
    assert bad.json()["error"]["code"] == -32700
    assert m.http.get("/mcp").status_code == 405
    # Revoked token: MCP stops working immediately
    dev.revoke_agent_token(research["agent"]["id"], research["token"]["id"])
    assert m.rpc("tools/list").status_code == 401


def test_provision_returns_connection_details(server):
    dev = server.signup()
    out = dev.provision_agent("x")
    c = out["connect"]
    assert c["mcp_url"].endswith("/mcp")
    assert c["mcp_config"]["mcpServers"]["agent-cloud"]["headers"]["Authorization"] == f"Bearer {out['token']['token']}"
    assert c["env"]["AGENTCLOUD_TOKEN"] == out["token"]["token"]


def test_share_links_for_humans(server):
    dev = server.signup()
    ws = dev.create_workspace("w")
    ws.write("report.md", "final v1")
    link = ws.link("report.md", ttl_hours=1, max_downloads=2)
    anon = server.client().http  # no credentials at all
    r = anon.get(link["url_path"])
    assert r.status_code == 200 and r.content == b"final v1"
    assert "attachment" in r.headers["content-disposition"] and "sandbox" in r.headers["content-security-policy"]
    ws.write("report.md", "v2 edited later")
    assert anon.get(link["url_path"]).content == b"final v1"   # pinned to the shared version
    assert anon.get(link["url_path"]).status_code == 404        # max_downloads reached
    l2 = ws.link("report.md")
    ws.revoke_link(l2["id"])
    assert anon.get(l2["url_path"]).status_code == 404
    assert anon.get("/s/acl_guess").status_code == 404
    # A link token is not an API credential
    assert server.client(link["token"]).http.get("/v1/me").status_code == 401
    assert [a["action"] for a in ws.activity()].count("link_download") == 2


def test_share_links_owner_only_and_expire(server):
    dev = server.signup()
    ag = dev.provision_agent("a", access="full")
    agent_ws = server.client(ag["token"]["token"]).workspace(ag["workspace"]["id"])
    agent_ws.write("f.txt", "x")
    with pytest.raises(Forbidden):
        agent_ws.link("f.txt")
    ws = dev.workspace(ag["workspace"]["id"])
    short = ws.link("f.txt", ttl_hours=0.0003)  # ~1s
    time.sleep(1.3)
    assert server.client().http.get(short["url_path"]).status_code == 404


def test_agent_activity_and_workspace_sharing_view(server, env):
    dev, research, analyst, reader = env
    rws = dev.workspace(research["workspace"]["id"])
    rws.grant(analyst["agent"]["id"], "", read=True)
    a = server.client(analyst["token"]["token"])
    a.workspace(analyst["workspace"]["id"]).write("x.md", "x")
    acts = dev.activity(agent_id=analyst["agent"]["id"])
    kinds = {x["action"] for x in acts}
    assert {"create", "grant", "provision_agent"} <= kinds
    assert all(x["actor"] == f"agent:{analyst['agent']['id']}" or x["detail"].get("agent_id") == analyst["agent"]["id"] for x in acts)
    info = rws.refresh()
    assert info["shared"] and {x["name"] for x in info["agents"]} == {"research-agent", "analysis-agent"}
    # Agents don't get to enumerate other agents through workspace listings
    assert "agents" not in a.workspaces()[0].info


def test_cli(server, env, tmp_path):
    from agentcloud.cli import main
    dev, research, analyst, reader = env
    c = server.client(research["token"]["token"])

    def run(*argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(list(argv), client=c)
        return code, buf.getvalue()

    src = tmp_path / "plan.md"; src.write_text("# plan v1\n")
    assert run("put", str(src), "docs/plan.md")[0] == 0
    src.write_text("# plan v2\n")
    assert "v2" in run("put", str(src), "docs/plan.md", "-m", "update")[1]
    assert "docs/plan.md" in run("ls", "-r")[1]
    assert "update" in run("log", "docs/plan.md")[1]
    assert run("restore", "docs/plan.md", "1")[0] == 0
    code, out = run("--json", "stat", "docs/plan.md")
    assert json.loads(out)["version"] == 3
    (tmp_path / "local").mkdir(); (tmp_path / "local" / "a.txt").write_text("a")
    assert run("push", str(tmp_path / "local"), "sync/")[0] == 0
    assert run("pull", str(tmp_path / "back"), "sync/")[0] == 0
    assert (tmp_path / "back" / "a.txt").read_text() == "a"
    code, out = run("whoami")
    assert "research-agent" in out
    # Pending approval -> exit code 3
    careful = dev.provision_agent("careful", access="read_write", approval="overwrite")
    c2 = server.client(careful["token"]["token"])
    with redirect_stdout(io.StringIO()):
        assert main(["put", str(src), "p.md"], client=c2) == 0
        assert main(["put", str(tmp_path / "local" / "a.txt"), "p.md"], client=c2) == 3


def test_agent_names_are_unique_per_owner(server):
    dev = server.signup()
    a = dev.provision_agent("sales-agent")
    with pytest.raises(AgentCloudError) as e:
        dev.provision_agent("sales-agent")
    assert e.value.status == 409
    with pytest.raises(AgentCloudError):
        dev.create_agent("sales-agent")
    dev.delete_agent(a["agent"]["id"])          # a deleted agent frees its name
    assert dev.provision_agent("sales-agent")["agent"]["name"] == "sales-agent"
    other = server.signup("other@example.com")  # names are per owner
    assert other.provision_agent("sales-agent")


def test_batch_read_write_and_identity_in_handshake(server, env):
    """Fewer model turns: several files per call, and identity known at initialize."""
    dev, research, *_ = env
    m = MCP(server, research["token"]["token"])
    info = m.init()
    assert "You are agent 'research-agent'" in info["instructions"]
    res, t = m.call("cloud_write", files=[{"path": "/p/a.md", "content": "alpha beta"},
                                          {"path": "/p/b.md", "content": "gamma"},
                                          {"path": "../bad", "content": "x"}])
    assert "Saved /p/a.md" in t and "Saved /p/b.md" in t and "failed" in t and not res["isError"]
    res, t = m.call("cloud_read", paths=["/p/a.md", "/p/b.md", "/p/missing.md"])
    assert "=== /p/a.md (v1) ===\nalpha beta" in t and "=== /p/b.md (v1) ===\ngamma" in t and "(error)" in t
    assert [f.get("version") for f in res["structuredContent"]["files"]] == [1, 1, None]
    res, t = m.call("cloud_read")
    assert res["isError"] and "pass `path`" in t
