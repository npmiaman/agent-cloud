"""Runtime configuration, read from environment variables."""
from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("AGENTCLOUD_DATA", "~/.agentcloud")).expanduser().resolve())
    # Hard cap on a single upload, bytes.
    max_upload_bytes: int = int(os.environ.get("AGENTCLOUD_MAX_UPLOAD", str(100 * 1024 * 1024)))
    # Default storage quota for a new workspace, bytes (counts every stored version).
    default_quota_bytes: int = int(os.environ.get("AGENTCLOUD_DEFAULT_QUOTA", str(1024 * 1024 * 1024)))
    # User session lifetime, seconds.
    session_ttl: int = int(os.environ.get("AGENTCLOUD_SESSION_TTL", str(7 * 24 * 3600)))
    # Requests per minute per credential (and per IP for unauthenticated calls).
    rate_limit_per_minute: int = int(os.environ.get("AGENTCLOUD_RATE_LIMIT", "1200"))
    # Login/signup attempts per minute per IP.
    auth_rate_limit_per_minute: int = int(os.environ.get("AGENTCLOUD_AUTH_RATE_LIMIT", "20"))
    # Lifetime of access tokens exchanged for an agent credential, seconds.
    access_token_ttl: int = int(os.environ.get("AGENTCLOUD_ACCESS_TOKEN_TTL", "900"))
    # Optional second directory that receives a copy of every blob (another disk / NAS).
    replica_dir: Path | None = field(default_factory=lambda: Path(os.environ["AGENTCLOUD_REPLICA_DIR"]).expanduser().resolve()
                                     if os.environ.get("AGENTCLOUD_REPLICA_DIR") else None)
    # Run the background worker inside the server process (single-process deployments).
    inline_worker: bool = os.environ.get("AGENTCLOUD_INLINE_WORKER", "1") == "1"
    # 32-byte key for AES-256-GCM encryption of blobs at rest, base64. Generated and
    # persisted to data_dir/master.key on first run if unset.
    master_key_b64: str | None = os.environ.get("AGENTCLOUD_MASTER_KEY")
    # Vector search: none | hashing | sentence-transformers:<model> | http:<url>|<model> (see embeddings.py)
    embedder: str = os.environ.get("AGENTCLOUD_EMBEDDER", "none")
    # A local model is only ever loaded from the local cache unless this is set
    embedder_allow_download: bool = os.environ.get("AGENTCLOUD_EMBEDDER_ALLOW_DOWNLOAD") == "1"

    def master_key(self) -> bytes:
        if self.master_key_b64:
            key = base64.b64decode(self.master_key_b64)
        else:
            path = self.data_dir / "master.key"
            if path.exists():
                key = base64.b64decode(path.read_text().strip())
            else:
                key = secrets.token_bytes(32)
                self.data_dir.mkdir(parents=True, exist_ok=True)
                path.write_text(base64.b64encode(key).decode())
                os.chmod(path, 0o600)
        if len(key) != 32:
            raise ValueError("AGENTCLOUD_MASTER_KEY must decode to 32 bytes")
        return key
