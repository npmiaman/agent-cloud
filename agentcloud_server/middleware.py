"""Cross-cutting API standards (spec §11.10, §16.4, §19): request IDs, rate limits,
idempotency keys, security headers and request metrics.

Written as raw ASGI so request bodies are only buffered where needed (idempotent
control-plane calls), never for data-plane uploads.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections import Counter, defaultdict

from .db import Database, now
from .service import request_id_var
from . import provenance as P

IDEMPOTENT_METHODS = {b"POST", b"PUT", b"PATCH", b"DELETE"}
MAX_FINGERPRINT_BODY = 1024 * 1024
SECURITY_HEADERS = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY")]


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.counts: Counter = Counter()
        self.started = time.time()

    def inc(self, key: str, n: int = 1):
        with self.lock:
            self.counts[key] += n

    def snapshot(self) -> dict:
        with self.lock:
            return {"uptime_seconds": round(time.time() - self.started), **dict(self.counts)}


class RateLimiter:
    """Token bucket per key. In-process; a multi-instance deployment would back this with Redis."""

    def __init__(self):
        self.lock = threading.Lock()
        self.buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str, per_minute: int) -> tuple[bool, float]:
        rate = per_minute / 60.0
        with self.lock:
            tokens, last = self.buckets.get(key, (float(per_minute), time.monotonic()))
            t = time.monotonic()
            tokens = min(per_minute, tokens + (t - last) * rate)
            if tokens < 1:
                self.buckets[key] = (tokens, t)
                return False, (1 - tokens) / rate
            self.buckets[key] = (tokens - 1, t)
            if len(self.buckets) > 100_000:  # bound memory
                self.buckets.clear()
            return True, 0.0


def _header(scope, name: bytes) -> bytes | None:
    for k, v in scope.get("headers", []):
        if k == name:
            return v
    return None


def _json_error(status: int, code: str, message: str, request_id: str, extra_headers=()):
    body = json.dumps({"error": {"code": code, "message": message, "request_id": request_id}}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
               (b"x-request-id", request_id.encode()), *SECURITY_HEADERS, *extra_headers]
    return status, headers, body


class ApiStandards:
    def __init__(self, app, db: Database, settings, metrics: Metrics):
        self.app, self.db, self.settings, self.metrics = app, db, settings, metrics
        self.limiter = RateLimiter()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        rid = (_header(scope, b"x-request-id") or b"").decode()[:64] or "req_" + secrets.token_hex(8)
        token = request_id_var.set(rid)
        # REST callers can label their writes for provenance: X-Agent-Task, X-Agent-Model, traceparent
        hdr = {k.decode().lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
               if k.lower() in (b"x-agent-task", b"x-agent-model", b"traceparent", b"user-agent", b"x-agent-inputs")}
        actx = P.agent_ctx.set(P.from_meta(None, hdr))
        path: str = scope["path"]
        method: bytes = scope["method"].encode()
        try:
            # ---- rate limiting
            auth = _header(scope, b"authorization")
            client = (scope.get("client") or ("?", 0))[0]
            if path.startswith("/v1/auth/") and path != "/v1/auth/agent/token":
                key, limit = f"auth:{client}", self.settings.auth_rate_limit_per_minute
            elif auth:
                key, limit = "cred:" + hashlib.sha256(auth).hexdigest(), self.settings.rate_limit_per_minute
            else:
                key, limit = f"ip:{client}", self.settings.rate_limit_per_minute
            ok, retry = self.limiter.allow(key, limit)
            if not ok:
                self.metrics.inc("rate_limited")
                return await self._send(send, *_json_error(429, "RATE_LIMITED", "too many requests; slow down", rid,
                                                           [(b"retry-after", str(max(1, int(retry + 0.999))).encode())]))
            # ---- idempotency
            idem = _header(scope, b"idempotency-key")
            if idem and method in IDEMPOTENT_METHODS and not path.startswith("/v1/data/"):
                return await self._idempotent(scope, receive, send, rid, idem.decode()[:200], auth or b"")
            return await self._passthrough(scope, receive, send, rid)
        finally:
            request_id_var.reset(token)
            P.agent_ctx.reset(actx)

    async def _passthrough(self, scope, receive, send, rid):
        status_box = {}

        async def send_wrapper(msg):
            if msg["type"] == "http.response.start":
                status_box["s"] = msg["status"]
                headers = list(msg.get("headers", []))
                names = {k for k, _ in headers}
                headers.append((b"x-request-id", rid.encode()))
                headers += [h for h in SECURITY_HEADERS if h[0] not in names]
                msg = {**msg, "headers": headers}
            await send(msg)

        await self.app(scope, receive, send_wrapper)
        self._count(status_box.get("s", 500))

    def _count(self, status: int):
        self.metrics.inc("requests")
        self.metrics.inc(f"status_{status // 100}xx")
        if status == 403:
            self.metrics.inc("permission_denied")

    async def _send(self, send, status, headers, body):
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
        self._count(status)

    def _caller_scope(self, auth: bytes) -> str:
        """Scope keys to *who* is calling, not to the raw token: an agent whose access token
        refreshes between a write and its retry must still hit the same key."""
        from .auth import authenticate
        tok = auth.decode(errors="replace")
        tok = tok[7:].strip() if tok.lower().startswith("bearer ") else tok
        p = authenticate(self.db, tok) if tok else None
        ident = f"{p.owner_id}:{p.actor}" if p else "anon:" + hashlib.sha256(auth).hexdigest()
        return hashlib.sha256(ident.encode()).hexdigest()

    async def _idempotent(self, scope, receive, send, rid, key, auth: bytes):
        # Spool the body (memory up to 1 MiB, then disk) while hashing all of it, so the
        # fingerprint covers every byte and large bodies never sit in memory.
        import tempfile
        spool = tempfile.SpooledTemporaryFile(max_size=MAX_FINGERPRINT_BODY)
        h, size, more = hashlib.sha256(), 0, True
        while more:
            msg = await receive()
            chunk = msg.get("body", b"")
            h.update(chunk)
            size += len(chunk)
            spool.write(chunk)
            more = msg.get("more_body", False)
        spool.seek(0)
        fingerprint = f"{scope['method']} {scope['path']}?{scope.get('query_string', b'').decode()} {size}:{h.hexdigest()}"
        scope_key = self._caller_scope(auth)
        with self.db.tx():
            row = self.db.one("SELECT * FROM idempotency WHERE scope=? AND key=?", scope_key, key)
            if row is None:
                self.db.exec("INSERT INTO idempotency(scope, key, fingerprint, created_at) VALUES (?,?,?,?)",
                             scope_key, key, fingerprint, now())
        if row is not None:
            spool.close()
            if row["fingerprint"] != fingerprint:
                return await self._send(send, *_json_error(422, "IDEMPOTENCY_KEY_REUSED",
                                                           "this Idempotency-Key was already used for a different request", rid))
            if row["status"] is None:
                return await self._send(send, *_json_error(409, "IDEMPOTENCY_IN_PROGRESS",
                                                           "a request with this Idempotency-Key is still being processed", rid))
            self.metrics.inc("idempotent_replays")
            headers = [(b"content-type", (row["content_type"] or "application/json").encode()),
                       (b"content-length", str(len(row["body"] or b"")).encode()),
                       (b"idempotent-replayed", b"true"), (b"x-request-id", rid.encode()), *SECURITY_HEADERS]
            return await self._send(send, row["status"], headers, row["body"] or b"")

        done = False

        async def replay_receive():
            nonlocal done
            if not done:
                chunk = spool.read(256 * 1024)
                last = len(chunk) < 256 * 1024
                if last:
                    done = True
                return {"type": "http.request", "body": chunk, "more_body": not last}
            return await receive()

        captured = {"status": 500, "headers": [], "body": bytearray()}

        async def capture(msg):
            if msg["type"] == "http.response.start":
                captured["status"] = msg["status"]
                captured["headers"] = list(msg.get("headers", []))
            elif msg["type"] == "http.response.body":
                captured["body"] += msg.get("body", b"")

        try:
            await self.app(scope, replay_receive, capture)
        except BaseException:
            self.db.exec("DELETE FROM idempotency WHERE scope=? AND key=?", scope_key, key)
            raise
        finally:
            spool.close()
        status = captured["status"]
        ctype = dict(captured["headers"]).get(b"content-type", b"application/json").decode()
        if status >= 500:
            self.db.exec("DELETE FROM idempotency WHERE scope=? AND key=?", scope_key, key)  # let a retry run
        else:
            self.db.exec("UPDATE idempotency SET status=?, body=?, content_type=? WHERE scope=? AND key=?",
                         status, bytes(captured["body"]), ctype, scope_key, key)
        headers = [h for h in captured["headers"] if h[0] != b"content-length"]
        headers += [(b"content-length", str(len(captured["body"])).encode()), (b"x-request-id", rid.encode())]
        names = {k for k, _ in headers}
        headers += [h for h in SECURITY_HEADERS if h[0] not in names]
        await self._send(send, status, headers, bytes(captured["body"]))


__all__ = ["ApiStandards", "Metrics", "defaultdict"]
