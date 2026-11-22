"""Pluggable text embedders for vector search.

Configure with AGENTCLOUD_EMBEDDER:

    none (default)                              vector search off; search is keyword-only
    hashing                                     built-in, no download. Feature hashing of words and
                                                character trigrams: LEXICAL similarity (shared words
                                                and spellings), not semantic. Good for tests and as a
                                                fallback, not a substitute for a model.
    sentence-transformers:<model>               a local model, e.g. sentence-transformers:all-MiniLM-L6-v2.
                                                Loads offline only (local_files_only) unless
                                                AGENTCLOUD_EMBEDDER_ALLOW_DOWNLOAD=1, so nothing is
                                                downloaded behind your back. Needs `pip install sentence-transformers`.
    http:<url>[|<model>]                        any OpenAI-compatible embeddings endpoint (OpenAI, Voyage
                                                via a proxy, Ollama, vLLM, TEI...). Key from
                                                AGENTCLOUD_EMBEDDER_API_KEY. Content leaves this server:
                                                only use an endpoint you trust with it.

Every embedder returns L2-normalised vectors, so cosine similarity is a dot product. Model
libraries are imported lazily, on first use.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Protocol

_WORD = re.compile(r"\w+")


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _normalise(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


class HashingEmbedder:
    """Signed feature hashing of words (weight 1) and character trigrams of words (weight 0.5)
    with sublinear term frequency, into `dim` dimensions."""
    name = "hashing-384-v1"
    dim = 384

    def _features(self, text: str) -> dict[str, float]:
        feats: dict[str, float] = {}
        for w in _WORD.findall(text.lower()):
            feats["w:" + w] = feats.get("w:" + w, 0.0) + 1.0
            padded = f"#{w}#"
            for i in range(len(padded) - 2):
                g = "g:" + padded[i:i + 3]
                feats[g] = feats.get(g, 0.0) + 0.5
        return feats

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            v = [0.0] * self.dim
            for f, tf in self._features(text).items():
                h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
                v[h % self.dim] += (1.0 if (h >> 32) & 1 else -1.0) * (1.0 + math.log(tf))
            out.append(_normalise(v))
        return out


class SentenceTransformerEmbedder:
    def __init__(self, model: str, allow_download: bool = False, device: str | None = None):
        self.model_name, self.allow_download, self.device = model, allow_download, device
        self.name = f"st:{model}"
        self._model = None
        self._dim: int | None = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer   # imported on first use only
            kw = {"local_files_only": not self.allow_download}
            if self.device:
                kw["device"] = self.device
            try:
                self._model = SentenceTransformer(self.model_name, **kw)
            except Exception as e:
                if not self.allow_download:
                    raise RuntimeError(f"embedding model '{self.model_name}' isn't available locally and downloads are "
                                       "off (set AGENTCLOUD_EMBEDDER_ALLOW_DOWNLOAD=1 to allow one)") from e
                raise
            self._dim = int(self._model.get_sentence_embedding_dimension())
        return self._model

    @property
    def dim(self) -> int:
        self._load()
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        vecs = self._load().encode(texts, normalize_embeddings=True)
        return [[float(x) for x in v] for v in vecs]


class HTTPEmbedder:
    def __init__(self, url: str, model: str | None = None, api_key: str | None = None, http=None, timeout: float = 60):
        import httpx
        self.url, self.model = url, model
        self.name = f"http:{model or url}"
        self.api_key = api_key if api_key is not None else os.environ.get("AGENTCLOUD_EMBEDDER_API_KEY")
        self.http = http or httpx.Client(timeout=timeout)
        self._dim: int | None = None

    @property
    def dim(self) -> int:
        if self._dim is None:
            self.embed(["dimension probe"])
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        body = {"input": texts}
        if self.model:
            body = {"model": self.model, "input": texts}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        r = self.http.post(self.url, json=body, headers=headers)
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
        vecs = [_normalise([float(x) for x in d["embedding"]]) for d in data]
        if len(vecs) != len(texts):
            raise RuntimeError(f"embedding endpoint returned {len(vecs)} vectors for {len(texts)} inputs")
        self._dim = len(vecs[0]) if vecs else self._dim
        return vecs


def from_config(spec: str | None, allow_download: bool | None = None) -> Embedder | None:
    if allow_download is None:
        allow_download = os.environ.get("AGENTCLOUD_EMBEDDER_ALLOW_DOWNLOAD") == "1"
    spec = (spec or "none").strip()
    if spec in ("", "none", "off"):
        return None
    if spec == "hashing":
        return HashingEmbedder()
    kind, _, rest = spec.partition(":")
    if kind in ("sentence-transformers", "st") and rest:
        return SentenceTransformerEmbedder(rest, allow_download=allow_download)
    if kind == "http" and rest:
        url, _, model = rest.partition("|")
        return HTTPEmbedder(url, model or None)
    raise ValueError(f"unknown embedder '{spec}' (use none, hashing, sentence-transformers:<model> or http:<url>|<model>)")
