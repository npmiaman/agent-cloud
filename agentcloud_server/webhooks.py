"""Outbound webhooks.

Audit events (approval.created, file.deleted, credential.revoked, …) are queued as
deliveries in the same transaction that records them, then POSTed by a dispatcher:

    POST <url>
    X-AgentCloud-Event: approval.created
    X-AgentCloud-Delivery: whd_…
    X-AgentCloud-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>.<body>")>

Verify by recomputing the HMAC and rejecting timestamps older than ~5 minutes.
Non-2xx or network errors retry with backoff (10s, 1m, 5m, 30m, 2h), then fail.
Deliveries are claimed atomically, so several server processes never double-send.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx

from .db import Database, new_id, now

BACKOFF = [10, 60, 300, 1800, 7200]
TIMEOUT = 5.0


def matches(patterns: list[str], event: str) -> bool:
    for p in patterns:
        if p == "*" or p == event or (p.endswith(".*") and event.startswith(p[:-1])):
            return True
    return False


def enqueue(db: Database, owner_id: str, event: str, data: dict) -> int:
    """Queue a delivery for every matching webhook of the organisation. Called inside the
    caller's transaction, so an event is queued if and only if it was recorded."""
    hooks = db.all("SELECT id, events FROM webhooks WHERE owner_id=? AND disabled_at IS NULL", owner_id)
    n = 0
    for h in hooks:
        if not matches(json.loads(h["events"]), event):
            continue
        payload = json.dumps({"id": new_id("evt"), "type": event, "created_at": now(), "organisation_id": owner_id, "data": data})
        db.exec("""INSERT INTO webhook_deliveries(id, webhook_id, event, payload, next_attempt_at, created_at)
                   VALUES (?,?,?,?,?,?)""", new_id("whd"), h["id"], event, payload, now(), now())
        n += 1
    return n


def sign(secret: str, body: bytes, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify(secret: str, body: bytes, header: str, tolerance: int = 300) -> bool:
    """Reference verifier (also handy for tests and SDK users)."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except Exception:
        return False
    if abs(time.time() - ts) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, body, ts), header)


def deliver_due(db: Database, client: httpx.Client | None = None, limit: int = 50) -> dict:
    stats = {"delivered": 0, "retrying": 0, "failed": 0}
    due = db.all("""SELECT d.*, w.url, w.secret FROM webhook_deliveries d JOIN webhooks w ON w.id=d.webhook_id
                    WHERE d.status='pending' AND d.next_attempt_at <= ? AND w.disabled_at IS NULL
                    ORDER BY d.next_attempt_at LIMIT ?""", now(), limit)
    if not due:
        return stats
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
    try:
        for d in due:
            claimed = db.exec("UPDATE webhook_deliveries SET status='sending' WHERE id=? AND status='pending'", d["id"]).rowcount
            if not claimed:
                continue  # another process took it
            body = d["payload"].encode()
            headers = {"Content-Type": "application/json", "User-Agent": "agentcloud-webhooks/1",
                       "X-AgentCloud-Event": d["event"], "X-AgentCloud-Delivery": d["id"],
                       "X-AgentCloud-Signature": sign(d["secret"], body)}
            code, err = None, None
            try:
                r = client.post(d["url"], content=body, headers=headers)
                code = r.status_code
                ok = 200 <= code < 300
                if not ok:
                    err = f"HTTP {code}"
            except httpx.HTTPError as e:
                ok, err = False, f"{type(e).__name__}: {e}"[:300]
            attempts = d["attempts"] + 1
            if ok:
                db.exec("UPDATE webhook_deliveries SET status='delivered', attempts=?, response_code=?, error=NULL, delivered_at=? WHERE id=?",
                        attempts, code, now(), d["id"])
                stats["delivered"] += 1
            elif attempts > len(BACKOFF):
                db.exec("UPDATE webhook_deliveries SET status='failed', attempts=?, response_code=?, error=? WHERE id=?",
                        attempts, code, err, d["id"])
                stats["failed"] += 1
            else:
                db.exec("UPDATE webhook_deliveries SET status='pending', attempts=?, response_code=?, error=?, next_attempt_at=? WHERE id=?",
                        attempts, code, err, now() + BACKOFF[attempts - 1], d["id"])
                stats["retrying"] += 1
    finally:
        if own:
            client.close()
    return stats


def run_forever(db: Database, interval: float = 1.0) -> None:
    client = httpx.Client(timeout=TIMEOUT, follow_redirects=False)
    while True:
        try:
            deliver_due(db, client)
        except Exception as e:
            print(f"[webhooks] dispatch failed: {type(e).__name__}: {e}", flush=True)
        time.sleep(interval)
