"""agentcloud_core: the reference library for the Agent Cloud repository format (SPEC.md).

Store agent work directly on S3, GCS, R2, MinIO or a local directory, with branches,
merges and lineage, and no server in the write path.
"""
from .repo import Branch, Codec, Conflict, LocalKEK, MergeConflict, Repo, Transaction
from .store import LocalStore, MemoryStore, ObjectStore, S3Store

__version__ = "0.1.0"
__all__ = ["Repo", "Branch", "Transaction", "Conflict", "MergeConflict", "LocalKEK", "Codec",
           "ObjectStore", "MemoryStore", "LocalStore", "S3Store"]
