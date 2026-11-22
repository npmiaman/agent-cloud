"""Regression: a write must see reads that the background flusher is committing right now.

The flusher used to take the buffered events, then open a transaction to insert them. A read
set computed in between saw an empty buffer and uncommitted rows, missed the read, and
labelled content derived from untrusted input as trusted.
"""
import threading
import time
from contextlib import contextmanager

from agentcloud_server import provenance as P
from agentcloud_server import service as S


def test_read_set_waits_for_an_in_flight_flush(server, monkeypatch):
    dev = server.signup()
    a = dev.provision_agent("racer", access="owner")
    ctx = server.app.state.ctx
    db = ctx.db
    ws = dict(db.one("SELECT * FROM workspaces WHERE id=?", a["workspace"]["id"]))
    actor = f"agent:{a['agent']['id']}"
    S.flush_activity(db)
    S.log(db, ws["owner_id"], ws["id"], actor, "read", "web.md", "fil_racefile", buffered=True, version=1)
    orig_tx = db.tx
    in_flight = threading.Event()

    @contextmanager
    def slow_tx():
        with orig_tx() as conn:
            if threading.current_thread().name == "slow-flusher":
                in_flight.set()
                time.sleep(0.3)               # rows taken from the buffer, not yet committed
            yield conn
    monkeypatch.setattr(db, "tx", slow_tx)
    t = threading.Thread(target=S.flush_activity, args=(db,), name="slow-flusher")
    t.start()
    assert in_flight.wait(2)
    inputs = P.read_set(db, actor, [ws["id"]], None)
    t.join()
    assert ("fil_racefile", 1) in inputs
