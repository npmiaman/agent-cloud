"""PRD §16, the MVP definition of success, as one runnable scenario."""
import pytest

from agentcloud import NotFound


def test_prd_section_16(server):
    # 1. Create an account.
    dev = server.signup()
    assert dev.me()["kind"] == "user"

    # 2. Create a workspace for an AI agent.
    ws = dev.create_workspace("research", "research agent's files")
    researcher = dev.create_agent("research-agent")
    ws.grant(researcher["id"], "", read=True, write=True, delete=True)

    # 3. Connect the agent through an API (its own scoped credential).
    token = dev.create_agent_token(researcher["id"])
    agent = server.client(token)
    assert agent.me()["kind"] == "agent"

    # 4. Upload and retrieve files.
    aws = agent.workspace(ws.id)
    v1 = aws.write("leads.csv", "name,company,email,score\nAda,Analytical,ada@x.io,9\nAlan,Bletchley,alan@x.io,8\n")
    assert v1.version == 1 and v1.op == "create"
    assert aws.read_text("leads.csv").startswith("name,company")

    # 5. Shut down the agent environment (client and server process gone).
    del agent, aws
    server.restart()

    # 6. Restart the agent and retrieve its previous files.
    agent = server.client(token)
    aws = agent.workspace(ws.id)
    assert [e["path"] for e in aws.list()] == ["leads.csv"]
    assert "Ada,Analytical" in aws.read_text("leads.csv")

    # 7. Modify a file and see its previous version. (The agent drops two columns by mistake.)
    v2 = aws.write("leads.csv", "name,score\nAda,9\nAlan,8\n", message="cleanup")
    assert v2.version == 2
    hist = aws.versions("leads.csv")
    assert [h["n"] for h in hist] == [2, 1]
    assert hist[0]["actor"] == f"agent:{researcher['id']}"
    assert "company" in aws.read_text("leads.csv", version=1)
    d = dev.workspace(ws.id).diff("leads.csv", 1, 2)
    assert d["csv"]["columns_removed"] == ["company", "email"]

    # 8. Restore the original file.
    r = dev.workspace(ws.id).restore("leads.csv", version=1)
    assert r.version == 3 and r.op == "restore"
    assert "ada@x.io" in aws.read_text("leads.csv")
    assert [h["n"] for h in aws.versions("leads.csv")] == [3, 2, 1]  # history kept, nothing rewritten

    # 9. Restrict another agent from accessing that workspace.
    other = dev.create_agent("unrelated-agent")
    other_ws = server.client(dev.create_agent_token(other["id"])).workspace(ws.id)
    with pytest.raises(NotFound):  # not even told it exists
        other_ws.read("leads.csv")
    with pytest.raises(NotFound):
        other_ws.write("evil.txt", "x")

    # And the activity log recorded who did what.
    actions = [(a["actor"].split(":")[0], a["action"]) for a in dev.workspace(ws.id).activity()]
    assert ("agent", "create") in actions and ("agent", "update") in actions and ("user", "restore") in actions
