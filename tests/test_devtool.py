"""Dev-tool surface: local bootstrap, one-time login links, webhooks, live log tail, system info."""
import json
import time

import httpx

from agentcloud_server import local, webhooks


def _ctx(server):
    return server.app.state.ctx


def test_bootstrap_is_once_and_writes_private_admin_file(server):
    db = _ctx(server).db
    rec = local.bootstrap(db, server.data_dir)
    assert rec["email"] == "admin@localhost" and rec["api_key"].startswith("acu_")
    f = local.admin_file(server.data_dir)
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    assert local.bootstrap(db, server.data_dir) is None              # second run: no new admin
    admin = server.client(rec["api_key"])
    assert admin.me()["organisation"]["name"] == "Local"


def test_magic_links_are_single_use_and_expire(server):
    db = _ctx(server).db
    rec = local.bootstrap(db, server.data_dir)
    http = server.client().http
    code = local.magic_link(db, rec["user_id"], "http://x").split("login=")[1]
    r = http.post("/v1/auth/magic", json={"code": code})
    assert r.status_code == 200 and r.json()["token"].startswith("acs_")
    assert http.post("/v1/auth/magic", json={"code": code}).json()["error"]["code"] == "BAD_LINK"
    old = local.magic_link(db, rec["user_id"], "http://x").split("login=")[1]
    db.exec("UPDATE magic_links SET expires_at=?", time.time() - 1)
    assert http.post("/v1/auth/magic", json={"code": old}).status_code == 401


def test_webhooks_signed_filtered_retried(server):
    db = _ctx(server).db
    dev = server.signup()
    hook = dev._json("POST", "/v1/webhooks", json={"url": "https://example.test/hook", "events": ["approval.*", "file.deleted"]})
    assert hook["secret"].startswith("whsec_")
    ag = dev.provision_agent("careful", access="full", approval="overwrite")
    A = server.client(ag["token"]["token"]).workspace(ag["workspace"]["id"])
    A.write("a.md", "v1")                                            # file.created: not subscribed
    p = A.write("a.md", "v2")                                        # approval.created: subscribed
    dev.approve(p.proposal_id)                                       # approval.approved
    events = [r["event"] for r in db.all("SELECT event FROM webhook_deliveries ORDER BY created_at")]
    assert events == ["approval.created", "approval.approved"]

    seen, status = [], {"code": 500}

    def handler(req: httpx.Request):
        seen.append(req)
        return httpx.Response(status["code"])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = webhooks.deliver_due(db, client)
    assert stats == {"delivered": 0, "retrying": 2, "failed": 0}
    req = seen[0]
    assert webhooks.verify(hook["secret"], req.content, req.headers["x-agentcloud-signature"])
    assert json.loads(req.content)["type"] == "approval.created"
    assert webhooks.deliver_due(db, client) == {"delivered": 0, "retrying": 0, "failed": 0}   # backoff: not due yet
    db.exec("UPDATE webhook_deliveries SET next_attempt_at=?", time.time() - 1)
    status["code"] = 204
    assert webhooks.deliver_due(db, client)["delivered"] == 2
    log = dev._json("GET", f"/v1/webhooks/{hook['id']}/deliveries")
    assert {d["status"] for d in log} == {"delivered"} and all(d["attempts"] == 2 for d in log)


def test_webhook_claims_prevent_double_send(server):
    db = _ctx(server).db
    dev = server.signup()
    hook = dev._json("POST", "/v1/webhooks", json={"url": "https://example.test/h"})
    dev._json("POST", f"/v1/webhooks/{hook['id']}/test")
    sent = []
    c = httpx.Client(transport=httpx.MockTransport(lambda r: (sent.append(1), httpx.Response(200))[1]))
    webhooks.deliver_due(db, c)
    webhooks.deliver_due(db, c)                                      # a second dispatcher finds nothing
    assert len(sent) == 1
    assert webhooks.matches(["approval.*"], "approval.created") and not webhooks.matches(["approval.*"], "file.created")


def test_live_log_tail_and_filters(server):
    dev = server.signup()
    ag = dev.provision_agent("a", access="full")
    A = server.client(ag["token"]["token"]).workspace(ag["workspace"]["id"])
    A.write("notes/x.md", "x")
    last = dev._json("GET", "/v1/activity", params={"limit": 1})[0]["id"]
    A.write("notes/y.md", "y")
    A.read("notes/y.md")                                            # buffered read event
    tail = dev._json("GET", "/v1/activity", params={"after": last})
    assert [t["event"] for t in tail] == ["file.created", "file.read"]   # oldest first, buffered read included
    only_reads = dev._json("GET", "/v1/activity", params={"event": "file.read"})
    assert only_reads and all(r["action"] == "read" for r in only_reads)
    by_path = dev._json("GET", "/v1/activity", params={"q": "y.md"})
    assert by_path and all("y.md" in r["path"] for r in by_path)
    prefix = dev._json("GET", "/v1/activity", params={"event": "file."})
    assert {r["event"] for r in prefix} <= {"file.created", "file.read", "file.modified"}


def test_system_info_is_admin_only(server):
    dev = server.signup()
    info = dev._json("GET", "/v1/system")
    assert info["version"] and info["counts"]["workspaces"] >= 0 and "data_dir" in info
    ag = dev.provision_agent("a")
    r = server.client(ag["token"]["token"]).http.get("/v1/system")
    assert r.status_code == 403


def test_dashboard_is_compressed_and_cacheable(server):
    http = server.client().http
    r = http.get("/", headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip" and r.headers["etag"]
    assert http.get("/", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
