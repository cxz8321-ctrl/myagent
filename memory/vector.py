# -*- coding: utf-8 -*-
"""向量索引：把 memory_vectors 里的向量拿来暴力算余弦（点积）。

口径（规格书 B.10-9）：1 万条以内单文件 sqlite + 暴力算完全够用，不必上向量库扩展；
真慢了再换 sqlite-vec，接口不变。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None


class VectorIndex:
    def __init__(self, store, embedder, logger=None):
        self.store = store
        self.embedder = embedder
        self.logger = logger
        self._cache: Optional[Tuple[List[str], "np.ndarray"]] = None

    # ---------- 建索引 ----------
    def index_memory(self, memory_id: str, text: str) -> bool:
        if not self.embedder or not self.embedder.available():
            return False
        vec = self.embedder.embed_one(text)
        self.store.conn.execute(
            "INSERT OR REPLACE INTO memory_vectors(memory_id,chunk_index,embedding,model,dim,created_at) "
            "VALUES(?,0,?,?,?,?)",
            (memory_id, _pack(vec), self.embedder.model_name, len(vec), int(time.time() * 1000)))
        self.store.conn.commit()
        self._cache = None
        return True

    def rebuild(self, memory_ids: Optional[List[str]] = None) -> int:
        """重建：换 embedding 模型时只重建这张表，主表不动。"""
        q = "SELECT id, content FROM memories WHERE status='active'"
        if memory_ids:
            q += " AND id IN (%s)" % ",".join("?" * len(memory_ids))
        rows = self.store.conn.execute(q, tuple(memory_ids or [])).fetchall()
        n = 0
        for r in rows:
            if self.index_memory(r["id"], r["content"] or ""):
                n += 1
        return n

    # ---------- 检索 ----------
    def _load_cache(self) -> Tuple[List[str], Any]:
        if self._cache is not None:
            return self._cache
        rows = self.store.conn.execute(
            "SELECT memory_id, embedding FROM memory_vectors").fetchall()
        ids, mats = [], []
        for r in rows:
            ids.append(r["memory_id"])
            mats.append(_unpack(r["embedding"]))
        mat = np.asarray(mats, dtype="float32") if mats else np.zeros((0, 1), dtype="float32")
        self._cache = (ids, mat)
        return self._cache

    def search(self, query_vec: List[float], k: int = 10,
               allowed_ids: Optional[set] = None) -> List[Tuple[str, float]]:
        """暴力算点积（向量都已归一化）→ 取 top-k。"""
        ids, mat = self._load_cache()
        if not ids:
            return []
        q = np.asarray(query_vec, dtype="float32")
        sims = mat @ q
        order = np.argsort(-sims)
        out: List[Tuple[str, float]] = []
        for idx in order:
            mid = ids[int(idx)]
            if allowed_ids is not None and mid not in allowed_ids:
                continue
            out.append((mid, float(sims[int(idx)])))
            if len(out) >= k:
                break
        return out

    def invalidate(self) -> None:
        self._cache = None


def _pack(vec: List[float]) -> bytes:
    return np.asarray(vec, dtype="float32").tobytes() if np is not None else b""


def _unpack(blob: bytes):
    return np.frombuffer(blob, dtype="float32") if np is not None else []
