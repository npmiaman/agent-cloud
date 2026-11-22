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

    def replicate(self, ns: str, sha256: str) -> bool:
        """Ensure the replica has this blob (used on write and to backfill an existing store)."""
        rp = self._rpath(ns, sha256)
        if rp is None or rp.exists():
            return False
        self._atomic_copy(self._path(ns, sha256), rp)
        return True

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
                    if f.read(4) not in (MAGIC1, MAGIC2):
                        raise ValueError("bad header")
            except (FileNotFoundError, ValueError):
                self._atomic_copy(rp, path)
                self.healed += 1
        return self._read(path, sha256)

    def verify(self, ns: str, sha256: str) -> bool:
        try:
            for _ in self._read(self._path(ns, sha256), sha256):
                pass
            return True
        except Exception:
            return False

    def repair(self, ns: str, sha256: str) -> bool:
        """Replace a damaged primary blob from a valid replica copy."""
        rp = self._rpath(ns, sha256)
        if rp is None or not rp.exists():
            return False
        try:
            for _ in self._read(rp, sha256):
                pass
        except Exception:
            return False
        self._atomic_copy(rp, self._path(ns, sha256))
        self.healed += 1
        return True

    def _read(self, path: Path, sha256: str) -> Iterator[bytes]:
        with open(path, "rb") as f:
            magic = f.read(4)
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
                return b"".join(self._read(self._path(ns, sha256), sha256))
            raise
