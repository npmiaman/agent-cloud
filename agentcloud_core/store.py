"""Object stores with the two primitives the format needs (SPEC §10):

    put_if_absent(key, data) -> bool             create-only  (If-None-Match: *)
    put_if_match(key, data, etag) -> etag|None    compare-and-swap (If-Match)

plus get (data and ETag), list and delete. Backends: MemoryStore (tests), LocalStore (a
directory; safe across processes), S3Store (S3, R2, MinIO, or GCS via its S3 API).
"""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Protocol


class ObjectStore(Protocol):
    def get(self, key: str) -> tuple[bytes, str] | None: ...
    def put_if_absent(self, key: str, data: bytes) -> bool: ...
    def put_if_match(self, key: str, data: bytes, etag: str) -> str | None: ...
    def list(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...


def _etag(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


class MemoryStore:
    def __init__(self):
        self._d: dict[str, bytes] = {}
        self._lock = threading.Lock()
        self.ops = {"get": 0, "put": 0, "list": 0}

    def get(self, key):
        self.ops["get"] += 1
        with self._lock:
            v = self._d.get(key)
        return None if v is None else (v, _etag(v))

    def put_if_absent(self, key, data):
        self.ops["put"] += 1
        with self._lock:
            if key in self._d:
                return False
            self._d[key] = bytes(data)
            return True

    def put_if_match(self, key, data, etag):
        self.ops["put"] += 1
        with self._lock:
            cur = self._d.get(key)
            if cur is None or _etag(cur) != etag:
                return None
            self._d[key] = bytes(data)
            return _etag(data)

    def list(self, prefix):
        self.ops["list"] += 1
        with self._lock:
            return sorted(k for k in self._d if k.startswith(prefix))

    def delete(self, key):
        with self._lock:
            self._d.pop(key, None)


class LocalStore:
    """A directory. Create-only uses O_EXCL on a temp-file link; compare-and-swap takes a
    per-key lock file (O_CREAT|O_EXCL), checks the ETag, then renames atomically. Safe
    across threads and processes on one machine (and on filesystems with atomic link/rename)."""

    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ops = {"get": 0, "put": 0, "list": 0}

    def _p(self, key: str) -> Path:
        if ".." in key.split("/") or key.startswith("/"):
            raise ValueError("bad key")
        return self.root / key

    def get(self, key):
        self.ops["get"] += 1
        try:
            data = self._p(key).read_bytes()
        except FileNotFoundError:
            return None
        return data, _etag(data)

    def _tmp(self, path: Path, data: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        return tmp

    def put_if_absent(self, key, data):
        self.ops["put"] += 1
        path = self._p(key)
        tmp = self._tmp(path, data)
        try:
            os.link(tmp, path)          # atomic create-only
            return True
        except FileExistsError:
            return False
        finally:
            tmp.unlink(missing_ok=True)

    def put_if_match(self, key, data, etag):
        self.ops["put"] += 1
        path = self._p(key)
        lock = path.with_name(f".{path.name}.lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 10
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                try:                     # break a lock abandoned by a crashed writer
                    if time.time() - lock.stat().st_mtime > 30:
                        lock.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    return None
                time.sleep(0.0005)
        try:
            try:
                cur = path.read_bytes()
            except FileNotFoundError:
                return None
            if _etag(cur) != etag:
                return None
            tmp = self._tmp(path, data)
            os.replace(tmp, path)
            return _etag(data)
        finally:
            lock.unlink(missing_ok=True)

    def list(self, prefix):
        self.ops["list"] += 1
        base = self.root
        out = []
        start = self._p(prefix.rsplit("/", 1)[0]) if "/" in prefix else base
        if not start.exists():
            return []
        for p in start.rglob("*"):
            if p.is_file() and not p.name.startswith("."):
                k = p.relative_to(base).as_posix()
                if k.startswith(prefix):
                    out.append(k)
        return sorted(out)

    def delete(self, key):
        self._p(key).unlink(missing_ok=True)


class S3Store:
    """Amazon S3 (and S3-compatible stores) using conditional PUTs: If-None-Match: * and
    If-Match: <etag>. Requires boto3."""

    def __init__(self, bucket: str, client=None, prefix: str = ""):
        if client is None:
            import boto3
            client = boto3.client("s3")
        self.bucket, self.s3, self.prefix = bucket, client, prefix
        self.ops = {"get": 0, "put": 0, "list": 0}

    def _k(self, key):
        return self.prefix + key

    @staticmethod
    def _code(e) -> str:
        return e.response.get("Error", {}).get("Code", "")

    def get(self, key):
        from botocore.exceptions import ClientError
        self.ops["get"] += 1
        try:
            r = self.s3.get_object(Bucket=self.bucket, Key=self._k(key))
        except ClientError as e:
            if self._code(e) in ("NoSuchKey", "404"):
                return None
            raise
        return r["Body"].read(), r["ETag"]

    def put_if_absent(self, key, data):
        from botocore.exceptions import ClientError
        self.ops["put"] += 1
        try:
            self.s3.put_object(Bucket=self.bucket, Key=self._k(key), Body=data, IfNoneMatch="*")
            return True
        except ClientError as e:
            if self._code(e) in ("PreconditionFailed", "412", "ConditionalRequestConflict"):
                return False
            raise

    def put_if_match(self, key, data, etag):
        from botocore.exceptions import ClientError
        self.ops["put"] += 1
        try:
            r = self.s3.put_object(Bucket=self.bucket, Key=self._k(key), Body=data, IfMatch=etag)
            return r["ETag"]
        except ClientError as e:
            if self._code(e) in ("PreconditionFailed", "412", "ConditionalRequestConflict", "NoSuchKey", "404"):
                return None
            raise

    def list(self, prefix):
        self.ops["list"] += 1
        out, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": self._k(prefix)}
            if token:
                kw["ContinuationToken"] = token
            r = self.s3.list_objects_v2(**kw)
            out += [o["Key"][len(self.prefix):] for o in r.get("Contents", [])]
            if not r.get("IsTruncated"):
                return sorted(out)
            token = r["NextContinuationToken"]

    def delete(self, key):
        self.s3.delete_object(Bucket=self.bucket, Key=self._k(key))
