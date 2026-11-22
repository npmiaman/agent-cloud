"""Blob storage: the data plane's object store.

Blobs are content-addressed (sha256 of plaintext), immutable, deduplicated, and
encrypted at rest with AES-256-GCM. Large files stream through in 1 MiB chunks, so
memory use is constant regardless of file size.

Blobs live under a per-organisation namespace (<root>/<org>/<sha[:2]>/<sha>), so
storage keys enforce tenant isolation as well as the database does (spec §5.2, §17.1).
Identical content in two organisations is stored twice, on purpose.

`BlobStore` is the seam for S3/R2/GCS: implement put_file / open_stream / exists / delete.

On-disk formats
  ACB1 (legacy, single shot): MAGIC | 12-byte nonce | GCM(ciphertext+tag)
  ACB2 (chunked):             MAGIC | 8-byte random prefix | chunks...
      chunk = 4-byte big-endian ciphertext length | GCM(chunk plaintext)
      nonce = prefix | 4-byte chunk index; AAD = sha256 | index | final flag
  The final flag in the AAD stops truncation attacks: a stream that ends without a
  chunk marked final fails to decrypt.
  ACM1 (chunked manifest):    MAGIC | 12-byte nonce | GCM(JSON {"size", "chunks": [[sha, len], ...]})
      with AAD = "manifest:" + file sha. Big files are rewritten into this form by the
      worker's background dedup: content-defined chunks (cdc.py) stored once per
      organisation at <root>/<org>/chunks/<sha[:2]>/<sha>, each an ACB2 blob of its own.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import struct
import tempfile
from pathlib import Path
from typing import Iterator, Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CHUNK = 1024 * 1024
MAGIC1 = b"ACB1"
MAGIC2 = b"ACB2"
MAGICM = b"ACM1"
CDC_MIN_FILE = 64 * 1024          # files below this stay whole
_NS = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class TooLarge(Exception):
    pass


class BlobStore(Protocol):
    def put_file(self, ns: str, src: Path, sha256: str) -> None: ...
    def open_stream(self, ns: str, sha256: str) -> Iterator[bytes]: ...
    def exists(self, ns: str, sha256: str) -> bool: ...
    def delete(self, ns: str, sha256: str) -> None: ...


class LocalEncryptedBlobStore:
    def __init__(self, root: Path, key: bytes, replica: Path | None = None):
        """`replica`: an optional second directory (ideally another disk or a network mount)
        that receives a copy of every blob. Reads fall back to it and repair the primary."""
        self.root = Path(os.path.realpath(root))
        self.root.mkdir(parents=True, exist_ok=True)
        self.tmp = root / ".tmp"
        self.tmp.mkdir(exist_ok=True)
        self.aead = AESGCM(key)
        self.replica = Path(os.path.realpath(replica)) if replica else None
        if self.replica:
            self.replica.mkdir(parents=True, exist_ok=True)
        self.healed = 0

    # ------------------------------------------------------------ paths

    def _path(self, ns: str, sha256: str) -> Path:
        if not _NS.match(ns or ""):
            raise ValueError("bad namespace")
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise ValueError("bad blob id")
        return self._contained(self.root, os.path.join(self.root, ns, sha256[:2], sha256))

    @staticmethod
    def _contained(base: Path, candidate: str) -> Path:
        """Defence in depth: the inputs are already validated above, but never let a
        resolved path leave the store's root."""
        resolved = os.path.realpath(candidate)
        if not resolved.startswith(str(base) + os.sep):
            raise ValueError("blob path escapes the store")
        return Path(resolved)

    def _rpath(self, ns: str, sha256: str) -> Path | None:
        if not self.replica:
            return None
        self._path(ns, sha256)  # validates ns and sha256
        return self._contained(self.replica, os.path.join(self.replica, ns, sha256[:2], sha256))

    def _chunk_path(self, ns: str, csha: str, base: Path | None = None) -> Path:
        self._path(ns, csha)    # validates ns and the hash
        base = base or self.root
        return self._contained(base, os.path.join(base, ns, "chunks", csha[:2], csha))

    def iter_chunks(self, ns: str) -> Iterator[tuple[str, Path]]:
        base = self.root / ns / "chunks"
        if base.exists():
            for p in base.glob("*/*"):
                if p.is_file() and len(p.name) == 64:
                    yield p.name, p

    def exists(self, ns: str, sha256: str) -> bool:
        return self._path(ns, sha256).exists() or bool(self.replica and self._rpath(ns, sha256).exists())

    def delete(self, ns: str, sha256: str) -> None:
        for path in (self._path(ns, sha256), self._rpath(ns, sha256)):
            if path is None:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _atomic_copy(src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".{dst.name}.{secrets.token_hex(4)}")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)

    def replicate(self, ns: str, sha256: str, force: bool = False) -> bool:
        """Ensure the replica has this blob, and its chunks if it's a manifest (used on write
        and to backfill an existing store)."""
        rp = self._rpath(ns, sha256)
        if rp is None:
            return False
        done = False
        for csha, _ in self.chunks_of(ns, sha256) or []:
            rc = self._chunk_path(ns, csha, self.replica)
            if not rc.exists():
                self._atomic_copy(self._chunk_path(ns, csha), rc)
                done = True
        if force or not rp.exists():
            self._atomic_copy(self._path(ns, sha256), rp)
            done = True
        return done

    def iter_namespace(self, ns: str) -> Iterator[tuple[str, Path]]:
        base = self.root / ns
        if not base.exists():
            return
        for p in base.glob("*/*"):
            if p.is_file() and len(p.name) == 64:
                yield p.name, p

    # ------------------------------------------------------------ staging (plaintext temp files)

    def stage_stream(self, chunks: Iterator[bytes], max_bytes: int) -> tuple[Path, str, int]:
        """Write an incoming stream to a private temp file while hashing it.
        Returns (temp path, sha256, size). Caller must commit or unlink it."""
        h = hashlib.sha256()
        size = 0
        fd, tmp = tempfile.mkstemp(dir=self.tmp, prefix="stage-")
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise TooLarge(f"upload exceeds {max_bytes} bytes")
                    h.update(chunk)
                    f.write(chunk)
            os.chmod(tmp, 0o600)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return Path(tmp), h.hexdigest(), size

    def stage_bytes(self, data: bytes) -> tuple[Path, str, int]:
        return self.stage_stream(iter([data]), len(data))

    # ------------------------------------------------------------ write

    def put_file(self, ns: str, src: Path, sha256: str) -> None:
        """Encrypt a staged plaintext file into the store. Idempotent (content-addressed)."""
        dest = self._path(ns, sha256)
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        prefix = secrets.token_bytes(8)
        size = src.stat().st_size
        fd, tmp = tempfile.mkstemp(dir=self.tmp, prefix="enc-")
        try:
            with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
                out.write(MAGIC2 + prefix)
                idx, done = 0, 0
                while True:
                    pt = inp.read(CHUNK)
                    done += len(pt)
                    final = done >= size
                    ct = self.aead.encrypt(prefix + struct.pack(">I", idx), pt, self._aad(sha256, idx, final))
                    out.write(struct.pack(">I", len(ct)) + ct)
                    idx += 1
                    if final:
                        break
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, dest)  # atomic: a blob is either whole or absent
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self.replicate(ns, sha256)

    # ------------------------------------------------------------ chunks and manifests

    def _encrypt_small(self, data: bytes, sha256: str) -> bytes:
        """ACB2 bytes for data that fits in one chunk (CDC chunks are at most 64 KiB)."""
        prefix = secrets.token_bytes(8)
        ct = self.aead.encrypt(prefix + struct.pack(">I", 0), data, self._aad(sha256, 0, True))
        return MAGIC2 + prefix + struct.pack(">I", len(ct)) + ct

    def _write_atomic(self, dest: Path, raw: bytes) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.tmp, prefix="w-")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(raw)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, dest)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _put_chunk(self, ns: str, data: bytes) -> str:
        csha = hashlib.sha256(data).hexdigest()
        dest = self._chunk_path(ns, csha)
        if not dest.exists():
            self._write_atomic(dest, self._encrypt_small(data, csha))
        if self.replica:
            rc = self._chunk_path(ns, csha, self.replica)
            if not rc.exists():
                self._atomic_copy(dest, rc)
        return csha

    def _decrypt_manifest(self, raw: bytes, sha256: str) -> dict:
        import json
        body = self.aead.decrypt(raw[4:16], raw[16:], b"manifest:" + sha256.encode())
        return json.loads(body)

    def chunks_of(self, ns: str, sha256: str) -> list[tuple[str, int]] | None:
        """[(chunk sha, length), ...] if the blob is stored chunked, else None."""
        for path in (self._path(ns, sha256), self._rpath(ns, sha256)):
            if path is None or not path.exists():
                continue
            with open(path, "rb") as f:
                if f.read(4) != MAGICM:
                    return None
            try:
                return [tuple(c) for c in self._decrypt_manifest(path.read_bytes(), sha256)["chunks"]]
            except Exception:
                continue    # damaged manifest: try the replica copy
        return None

    def _read_chunk(self, ns: str, csha: str, heal: bool) -> bytes:
        path = self._chunk_path(ns, csha)
        try:
            return b"".join(self._read(path, csha))
        except Exception:
            if not heal or not self.replica:
                raise
            rc = self._chunk_path(ns, csha, self.replica)
            data = b"".join(self._read(rc, csha))          # raises if the replica copy is bad too
            self._atomic_copy(rc, path)
            self.healed += 1
            return data

    def rechunk(self, ns: str, sha256: str) -> bool:
        """Background dedup: rewrite a whole-file blob as content-defined chunks plus a manifest.
        Atomic (os.replace); readers holding the old file keep reading it. False if not applicable."""
        import json

        from .cdc import chunk_stream
        dest = self._path(ns, sha256)
        if not dest.exists() or dest.stat().st_size < CDC_MIN_FILE:
            return False
        with open(dest, "rb") as f:
            if f.read(4) not in (MAGIC1, MAGIC2):
                return False
        fd, tmp = tempfile.mkstemp(dir=self.tmp, prefix="rechunk-")
        try:
            with os.fdopen(fd, "wb") as out:
                for part in self._read(dest, sha256):        # verifies integrity while decrypting
                    out.write(part)
            os.chmod(tmp, 0o600)
            size = Path(tmp).stat().st_size
            chunks = []
            with open(tmp, "rb") as inp:
                for c in chunk_stream(inp):
                    chunks.append([self._put_chunk(ns, c), len(c)])
        finally:
            Path(tmp).unlink(missing_ok=True)
        body = json.dumps({"size": size, "chunks": chunks}, separators=(",", ":")).encode()
        nonce = secrets.token_bytes(12)
        self._write_atomic(dest, MAGICM + nonce + self.aead.encrypt(nonce, body, b"manifest:" + sha256.encode()))
        if self.replica:
            self.replicate(ns, sha256, force=True)
        return True

    def put_bytes(self, ns: str, data: bytes) -> tuple[str, int]:
        src, sha, size = self.stage_bytes(data)
        try:
            self.put_file(ns, src, sha)
        finally:
            src.unlink(missing_ok=True)
        return sha, size

    @staticmethod
    def _aad(sha256: str, idx: int, final: bool) -> bytes:
        return sha256.encode() + struct.pack(">I?", idx, final)

    # ------------------------------------------------------------ read

    def open_stream(self, ns: str, sha256: str) -> Iterator[bytes]:
        """Yield decrypted chunks, verifying integrity. Each chunk is authenticated and the
        whole-file sha256 is checked at the end. If the primary copy is missing or its header
        is damaged, the replica serves the read and repairs the primary. (Damage deeper in a
        file is caught by the worker's scrub, which repairs from the replica.)"""
        path = self._path(ns, sha256)
        rp = self._rpath(ns, sha256)
        if rp is not None and rp.exists():
            try:
                with open(path, "rb") as f:
                    if f.read(4) not in (MAGIC1, MAGIC2, MAGICM):
                        raise ValueError("bad header")
            except (FileNotFoundError, ValueError):
                self._atomic_copy(rp, path)
                self.healed += 1
        return self._read(path, sha256, ns=ns, heal=True)

    def verify(self, ns: str, sha256: str) -> bool:
        try:
            for _ in self._read(self._path(ns, sha256), sha256, ns=ns, heal=False):
                pass
            return True
        except Exception:
            return False

    def repair(self, ns: str, sha256: str) -> bool:
        """Replace a damaged primary blob (and any damaged chunks) from valid replica copies."""
        rp = self._rpath(ns, sha256)
        if rp is None or not rp.exists():
            return False
        try:
            for _ in self._read(rp, sha256, ns=ns, heal=True, chunk_base=self.replica):
                pass
        except Exception:
            return False
        if not self.verify(ns, sha256):
            self._atomic_copy(rp, self._path(ns, sha256))
        for csha, _ in self.chunks_of(ns, sha256) or []:
            try:
                self._read_chunk(ns, csha, heal=True)
            except Exception:
                return False
        self.healed += 1
        return self.verify(ns, sha256)

    def _read(self, path: Path, sha256: str, ns: str | None = None, heal: bool = False,
              chunk_base: Path | None = None) -> Iterator[bytes]:
        with open(path, "rb") as f:
            magic = f.read(4)
            if magic == MAGICM:
                manifest = self._decrypt_manifest(magic + f.read(), sha256)
                ns = ns or path.parent.parent.name
                h = hashlib.sha256()
                for csha, n in manifest["chunks"]:
                    if chunk_base is not None:
                        data = b"".join(self._read(self._chunk_path(ns, csha, chunk_base), csha))
                    else:
                        data = self._read_chunk(ns, csha, heal)
                    if len(data) != n:
                        raise ValueError("chunk length mismatch")
                    h.update(data)
                    yield data
                if h.hexdigest() != sha256:
                    raise ValueError("blob integrity check failed")
                return
            if magic == MAGIC1:
                raw = f.read()
                data = self.aead.decrypt(raw[:12], raw[12:], sha256.encode())
                if hashlib.sha256(data).hexdigest() != sha256:
                    raise ValueError("blob integrity check failed")
                for i in range(0, max(len(data), 1), CHUNK):
                    yield data[i:i + CHUNK]
                return
            if magic != MAGIC2:
                raise ValueError("corrupt blob header")
            prefix = f.read(8)
            h = hashlib.sha256()
            idx = 0
            while True:
                hdr = f.read(4)
                if len(hdr) != 4:
                    raise ValueError("truncated blob")
                (n,) = struct.unpack(">I", hdr)
                ct = f.read(n)
                if len(ct) != n:
                    raise ValueError("truncated blob")
                nonce = prefix + struct.pack(">I", idx)
                is_last = f.peek(1)[:1] == b"" if hasattr(f, "peek") else False
                pt = self.aead.decrypt(nonce, ct, self._aad(sha256, idx, is_last))
                h.update(pt)
                if is_last:
                    if h.hexdigest() != sha256:
                        raise ValueError("blob integrity check failed")
                    yield pt
                    return
                yield pt
                idx += 1

    def open(self, ns: str, sha256: str) -> bytes:
        try:
            return b"".join(self.open_stream(ns, sha256))
        except Exception:
            if self.repair(ns, sha256):         # damaged deeper in the file: retry from the repaired copy
                return b"".join(self._read(self._path(ns, sha256), sha256, ns=ns))
            raise
