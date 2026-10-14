# -*- coding: utf-8 -*-
"""embedding：用 llama.cpp 直接读 GGUF 出向量（不装 torch，复用已有运行时）。

规格书口径：embedding 模型本地小模型优先 —— 省费用、可离线；量大了再换大的。
向量一律 L2 归一化后再存，相似度就用点积。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None


class Embedder:
    def __init__(self, model_path: str, model_name: Optional[str] = None, n_ctx: int = 512,
                 n_threads: int = 0, query_prefix: str = "", batch: int = 8, logger=None):
        self.model_path = os.path.expanduser(model_path)
        self.model_name = model_name or os.path.basename(self.model_path)
        self.n_ctx = int(n_ctx)
        self.n_threads = int(n_threads)
        self.query_prefix = query_prefix
        self.batch = int(batch)
        self.logger = logger
        self._llm = None
        self._dim = 0

    # ---------- 懒加载 ----------
    def _load(self):
        if self._llm is not None:
            return
        from llama_cpp import Llama
        if not os.path.isfile(self.model_path):
            raise FileNotFoundError("embedding 模型不存在：%s" % self.model_path)
        kw: Dict[str, Any] = {"model_path": self.model_path, "embedding": True,
                              "n_ctx": self.n_ctx, "verbose": False}
        if self.n_threads > 0:
            kw["n_threads"] = self.n_threads
        self._llm = Llama(**kw)

    @property
    def dim(self) -> int:
        if not self._dim:
            v = self.embed(["x"])
            self._dim = int(v.shape[1])
        return self._dim

    def available(self) -> bool:
        return os.path.isfile(self.model_path) and np is not None

    # ---------- 出向量 ----------
    def embed(self, texts: List[str], is_query: bool = False) -> "np.ndarray":
        self._load()
        if np is None:
            raise RuntimeError("需要 numpy")
        if isinstance(texts, str):
            texts = [texts]
        if is_query and self.query_prefix:
            texts = [self.query_prefix + t for t in texts]
        vecs: List[List[float]] = []
        for i in range(0, len(texts), self.batch):
            chunk = [t if t.strip() else " " for t in texts[i:i + self.batch]]
            out = self._llm.create_embedding(chunk)
            for d in (out.get("data") or []):
                vecs.append(d["embedding"])
        arr = np.asarray(vecs, dtype="float32")
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms

    def embed_one(self, text: str, is_query: bool = False) -> List[float]:
        return self.embed([text], is_query=is_query)[0].tolist()

    def health(self) -> Dict[str, Any]:
        ok = self.available()
        return {"ok": ok, "model": self.model_name, "path": self.model_path,
                "exists": os.path.isfile(self.model_path), "loaded": self._llm is not None}
