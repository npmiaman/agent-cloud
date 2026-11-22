"""One-call provisioning: an agent, its own isolated workspace with a quota, permissions and a token."""
import pytest

from agentcloud import Forbidden, Pending


def test_provision_gives_an_isolated_working_workspace(server):
    dev = server.signup()
    out = dev.provision_agent("researcher", storage_mb=5, access="full")
    assert out["workspace"]["quota_bytes"] == 5 * 1024 * 1024
    assert out["token"]["token"].startswith("aca_")
    agent = server.client(out["token"]["token"])
    ws = agent.workspace(out["workspace"]["id"])
    ws.write("notes/plan.md", "# plan\n")
    assert ws.read_text("notes/plan.md") == "# plan\n"
    ws.delete("notes/plan.md")  # full access includes delete

    # a second agent can't see the first one's workspace
    other = dev.provision_agent("analyst")
    them = server.client(other["token"]["token"])
    with pytest.raises(Exception):
        them.workspace(out["workspace"]["id"]).list()


def test_access_levels_and_approval(server):
    dev = server.signup()
    ro = dev.provision_agent("reader", access="read_only")
    with pytest.raises(Forbidden):
        server.client(ro["token"]["token"]).workspace(ro["workspace"]["id"]).write("a.txt", "x")

    rw = dev.provision_agent("writer", access="read_write", approval="overwrite")
    ws = server.client(rw["token"]["token"]).workspace(rw["workspace"]["id"])
    assert ws.write("a.txt", "one").version == 1  # creating needs no approval under "overwrite"
    pending = ws.write("a.txt", "two")
    assert isinstance(pending, Pending)  # changing an existing file does
    with pytest.raises(Forbidden):
        ws.delete("a.txt")  # read_write can't delete
    dev.approve(pending.proposal_id)
    assert ws.read_text("a.txt") == "two"


def test_provision_validates_input(server):
    dev = server.signup()
    r = dev.http.post("/v1/agents/provision", json={"name": "x", "access": "root"})
    assert r.status_code == 422
    agent = dev.provision_agent("a")
    r = server.client(agent["token"]["token"]).http.post("/v1/agents/provision", json={"name": "b"})
    assert r.status_code == 403  # agents can't create agents
