"""Text embeddings for family matching.

Voyage AI when VOYAGE_API_KEY is set; otherwise a deterministic hashing
embedding (word unigrams + bigrams) that needs no network. The local one is
good enough to separate task families by vocabulary, not semantics.
"""
from __future__ import annotations

import hashlib
import math
import re

import httpx

from . import config

_WORD = re.compile(r"[a-z]+")


def _local(text: str, dims: int) -> list[float]:
    words = _WORD.findall(text.lower())
    grams = words + [f"{a} {b}" for a, b in zip(words, words[1:])]
    vec = [0.0] * dims
    for g in grams:
        h = int.from_bytes(hashlib.blake2b(g.encode(), digest_size=8).digest(), "big")
        vec[h % dims] += 1.0 if (h >> 32) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def embed(texts: list[str]) -> list[list[float]]:
    if config.EMBEDDING_PROVIDER == "voyage":
        resp = httpx.post(
            "https://api.voyageai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {config.VOYAGE_API_KEY}"},
            json={"input": texts, "model": config.EMBEDDING_MODEL,
                  "output_dimension": config.EMBEDDING_DIMENSIONS},
            timeout=30,
        )
        resp.raise_for_status()
        return [d["embedding"] for d in resp.json()["data"]]
    return [_local(t, config.EMBEDDING_DIMENSIONS) for t in texts]


def embed_one(text: str) -> list[float]:
    return embed([text])[0]


def embedding_cost(texts: list[str]) -> float:
    if config.EMBEDDING_PROVIDER == "voyage":
        tokens = sum(len(t.split()) * 1.3 for t in texts)
        return tokens * config.VOYAGE_PRICE_PER_MTOK / 1e6
    return config.SIM_COST_EMBEDDING * len(texts)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def centroid(vectors: list[list[float]]) -> list[float]:
    dims = len(vectors[0])
    c = [sum(v[i] for v in vectors) / len(vectors) for i in range(dims)]
    norm = math.sqrt(sum(x * x for x in c)) or 1.0
    return [x / norm for x in c]
