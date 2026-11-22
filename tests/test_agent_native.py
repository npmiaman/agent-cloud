"""Agent-native features: targeted edits, appends, line windows, session continuity,
proposals, diffs, MCP resources, ranked search with line ranges."""
import pytest

from test_agent_interface import MCP


@pytest.fixture
def two(server):
    dev = server.signup()
    a = dev.provision_agent("writer", access="full")
    b = dev.provision_agent("reviewer", access="full")
    return dev, a, b


def test_edit_applies_exact_replacements(server, two):
    dev, a, _ = two
    m = MCP(server, a["token"]["token"]); m.init()
    m.call("cloud_write", path="/doc.md", content="# Plan\nstatus: draft\nowner: ada\nstatus: draft\n")
    res, t = m.call("cloud_edit", path="/doc.md", edits=[{"old": "owner: ada", "new": "owner: alan"}])
    assert "Applied 1 edit(s)" in t and not res["isError"]
    res, t = m.call("cloud_edit", path="/doc.md", edits=[{"old": "status: draft", "new": "status: final"}])
    assert res["isError"] and "appears 2 times" in t                         # ambiguous: nothing changed
    res, t = m.call("cloud_edit", path="/doc.md", edits=[{"old": "status: draft", "new": "status: final", "replace_all": True},
                                                          {"old": "# Plan", "new": "# Plan v2"}])
    assert not res["isError"]
    assert m.call("cloud_read", path="/doc.md")[1] == "# Plan v2\nstatus: final\nowner: alan\nstatus: final\n"
    res, t = m.call("cloud_edit", path="/doc.md", edits=[{"old": "nope", "new": "x"}])
    assert res["isError"] and "not found" in t
    res, t = m.call("cloud_edit", path="/doc.md", if_version=1, edits=[{"old": "# Plan v2", "new": "x"}])
    assert res["isError"] and "version 3" in t                                # compare-and-swap
    assert [v["op"] for v in dev.workspace(a["workspace"]["id"]).versions("doc.md")] == ["update", "update", "create"]


def test_edit_respects_approval_policy(server):
    dev = server.signup()
    ag = dev.provision_agent("careful", access="read_write", approval="overwrite")
    m = MCP(server, ag["token"]["token"]); m.init()
    m.call("cloud_write", path="/c.txt", content="price: 10\n")
    res, t = m.call("cloud_edit", path="/c.txt", edits=[{"old": "10", "new": "12"}])
    assert "waiting for human approval" in t
    assert m.call("cloud_read", path="/c.txt")[1] == "price: 10\n"
    res, t = m.call("cloud_proposals", status="pending")
    assert "pending" in t and "/c.txt" in t


def test_append_and_line_windows(server, two):
    dev, a, _ = two
    m = MCP(server, a["token"]["token"]); m.init()
    m.call("cloud_append", path="/log.txt", content="line 1\n")                  # creates
    for i in range(2, 101):
        m.call("cloud_append", path="/log.txt", content=f"line {i}\n")
    res, t = m.call("cloud_read", path="/log.txt", offset=50, limit=3, line_numbers=True)
    assert t.splitlines()[0] == "[/log.txt v100: lines 50-52 of 100]"
    assert t.splitlines()[1:] == ["50\tline 50", "51\tline 51", "52\tline 52"]
    assert res["structuredContent"]["total_lines"] == 100
    assert m.call("cloud_read", path="/log.txt", offset=99)[1].endswith("line 99\nline 100\n")


def test_session_briefing_and_changes(server, two):
    dev, a, b = two
    ma = MCP(server, a["token"]["token"])
    assert "This is your first session." in ma.init()["instructions"]
    ma.call("cloud_write", path="/shared/spec.md", content="v1")
    ma.call("cloud_share", agent="reviewer", path="/shared/", access="read_write", note="please review the spec")
    mb = MCP(server, b["token"]["token"])
    brief = mb.init()["instructions"]
    assert 'writer gave you read/write access to /shared/' in brief and '"please review the spec"' in brief
    mb.call("cloud_edit", path="/shared/spec.md", workspace="writer", edits=[{"old": "v1", "new": "v1 + review notes"}])
    brief2 = MCP(server, a["token"]["token"]).init()["instructions"]          # writer's next session
    assert "Since your last session" in brief2 and "reviewer edited /shared/spec.md" in brief2
    res, t = MCP(server, a["token"]["token"]).call("cloud_changes", hours=1)
    assert "reviewer edited /shared/spec.md" in t and res["structuredContent"]["changes_by_others"]


def test_decisions_reach_the_agent(server):
    dev = server.signup()
    ag = dev.provision_agent("careful", access="read_write", approval="overwrite")
    m = MCP(server, ag["token"]["token"]); m.init()
    m.call("cloud_write", path="/x.md", content="a")
    m.call("cloud_write", path="/x.md", content="b")
    pid = dev.pending()[0]["id"]
    dev.reject(pid, note="keep the original wording")
    brief = MCP(server, ag["token"]["token"]).init()["instructions"]
    assert 'Your change to /x.md was rejected: "keep the original wording"' in brief


def test_diff_tool(server, two):
    dev, a, _ = two
    m = MCP(server, a["token"]["token"]); m.init()
    m.call("cloud_write", path="/t.csv", content="name,company,email\nada,acme,a@x\n")
    m.call("cloud_write", path="/t.csv", content="name\nada\n")
    res, t = m.call("cloud_diff", path="/t.csv")
    assert "columns removed ['company', 'email']" in t and "-name,company,email" in t


def test_files_are_mcp_resources(server, two):
    dev, a, _ = two
    m = MCP(server, a["token"]["token"])
    caps = m.init()["capabilities"]
    assert "resources" in caps
    m.call("cloud_write", path="/notes/today.md", content="hello resources")
    lst = m.rpc("resources/list").json()["result"]["resources"]
    uri = next(r["uri"] for r in lst if r["uri"].endswith("notes/today.md"))
    assert uri == "agentcloud://writer/notes/today.md"
    got = m.rpc("resources/read", {"uri": uri}).json()["result"]["contents"][0]
    assert got["text"] == "hello resources" and got["mimeType"] == "text/markdown"
    err = m.rpc("resources/read", {"uri": "agentcloud://reviewer/secret.md"}).json()
    assert "error" in err                                                     # other agent's workspace: no access
    assert m.rpc("resources/templates/list").json()["result"]["resourceTemplates"]


def test_search_ranks_and_points_at_lines(server, two):
    dev, a, _ = two
    m = MCP(server, a["token"]["token"]); m.init()
    filler = "\n".join(f"unrelated line {i}" for i in range(120))
    m.call("cloud_write", files=[
        {"path": "/deep.md", "content": filler + "\nthe churn analysis shows churn rising; churn churn\n" + filler},
        {"path": "/brief.md", "content": "one mention of churn analysis"},
        {"path": "/other.md", "content": "nothing relevant here"}])
    res, t = m.call("cloud_search", query="churn analysis")
    hits = res["structuredContent"]["results"]
    assert {h["path"] for h in hits} == {"/deep.md", "/brief.md"}             # other.md excluded
    deep = next(h for h in hits if h["path"] == "/deep.md")
    assert deep["lines"] == "121-160" and "lines 121-160" in t                # points at the matching chunk
    window = m.call("cloud_read", path="/deep.md", offset=121, limit=1)[1]
    assert "churn analysis" in window


def test_rest_and_sdk_parity(server, two):
    dev, a, _ = two
    ws = server.client(a["token"]["token"]).workspace(a["workspace"]["id"])
    ws.write("notes.md", "alpha\nbeta\ngamma\n")
    assert ws.edit("notes.md", [{"old": "beta", "new": "BETA"}]).version == 2
    assert ws.append("notes.md", "delta\n").version == 3
    assert ws.read_lines("notes.md", offset=2, limit=2) == "BETA\ngamma\n"
    assert ws.read_lines("notes.md", offset=4, line_numbers=True) == "4\tdelta\n"
    assert dev.changes(hours=1)["changes_by_others"]                           # the owner sees the agent's edits
    txt = server.client().http.get("/llms.txt").text
    assert "cloud_edit" in txt and "/mcp" in txt and "{BASE}" not in txt


def test_home_workspace_survives_full_shares(server, two):
    """Regression (found with a real agent): a full-access share made the agent's home ambiguous."""
    dev, a, b = two
    ma, mb = MCP(server, a["token"]["token"]), MCP(server, b["token"]["token"])
    ma.init(); mb.init()
    ma.call("cloud_share", agent="reviewer", path="/", access="read_write")
    res, t = mb.call("cloud_write", path="/mine.md", content="in my own home")
    assert "Saved /mine.md" in t and "in 'reviewer'" in t
