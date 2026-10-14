"""Offline vectors: hash vectors measure vocabulary overlap, not semantics.

An optional sentence-transformers model loads only from an explicit local
directory. Dependencies and models are never automatically downloaded.
"""
import hashlib
import math
import os
import re
from functools import lru_cache
from pathlib import Path


def tokens(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text.casefold(), re.UNICODE)


@lru_cache(maxsize=2)
def _local_model(path: str):
    if not Path(path).is_dir():
        raise ValueError("MAKMA_EMBEDDING_MODEL requires an existing local directory")
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(path, local_files_only=True)


def embed(text: str) -> tuple[str, list[float]]:
    model_path = os.getenv("MAKMA_EMBEDDING_MODEL", "").strip()
    if model_path:
        try:
            vector = _local_model(model_path).encode(text, normalize_embeddings=True).tolist()
            return "semantic-local:" + str(Path(model_path).resolve()), vector
        except (ImportError, OSError, ValueError, RuntimeError):
            pass
    vector = [0.0] * 256
    for token in tokens(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        vector[int.from_bytes(digest[:4], "big") % len(vector)] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return "lexical-hash-v1", [value / norm for value in vector]


def similarity(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right)) if len(left) == len(right) else 0.0
