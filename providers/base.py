# -*- coding: utf-8 -*-
"""模型适配层：统一接口 + 统一数据契约 + 结果信封（规格书附录 A）。

铁律：
  - 上层看到的永远是同一种消息结构，厂商差异只在这层抹平。
  - 层与层之间只传可 JSON 序列化的纯数据。
  - 出口只报最终结果：错就返回 ok=false 的信封，不往上抛异常。
"""
from __future__ import annotations

import abc
import time
from typing import Any, Dict, Iterable, List, Optional

from runtime.text import sanitize_deep

# ---- 错误码（规格书 A.4）----
MODEL_TIMEOUT = "MODEL_TIMEOUT"
MODEL_RATE_LIMIT = "MODEL_RATE_LIMIT"
MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
MODEL_AUTH = "MODEL_AUTH"
MODEL_BAD_REQUEST = "MODEL_BAD_REQUEST"
MODEL_INTERNAL = "MODEL_INTERNAL"

RETRASSISTANTBLE_CODES = {MODEL_TIMEOUT, MODEL_RATE_LIMIT, MODEL_UNAVAILABLE}


def envelope(ok: bool, data: Any = None, error: Optional[Dict[str, Any]] = None,
             meta: Optional[Dict[str, Any]] = None, keep_data: bool = False) -> Dict[str, Any]:
    """统一结果信封 {ok, v, data, error, meta}。

    默认失败时不带 data（provider 既有口径）；`keep_data=True` 时失败也保留 data，
    供内核把"终止原因/费用/trace"留给上层查询（M5）。
    """
    env: Dict[str, Any] = {
        "ok": bool(ok),
        "v": 1,
        "data": data if (ok or keep_data) else None,
        "error": sanitize_deep(error) if error else error,   # 错误里可能夹带脏字符（如图床/API 回显）
        "meta": sanitize_deep(meta) if meta else (meta or {"ts": int(time.time() * 1000)}),
    }
    if not ok and error is None:
        env["error"] = err("SYS_INTERNAL", "unknown")
    return env


def err(code: str, message: str, retryable: Optional[bool] = None,
        detail: Any = None) -> Dict[str, Any]:
    if retryable is None:
        retryable = code in RETRASSISTANTBLE_CODES
    from runtime.text import sanitize
    return {"code": code, "message": sanitize(message), "retryable": bool(retryable),
            "detail": sanitize(detail)}


def estimate_tokens(text: Any) -> int:
    """粗略估算（±10% 量级），只用于上下文预算，不得用于对外计费。"""
    if isinstance(text, (list, tuple)):
        text = "\n".join(str(m.get("content") or "") if isinstance(m, dict) else str(m) for m in text)
    s = str(text or "")
    cjk = sum(1 for c in s if "\u4e00" <= c <= "\u9fff" or "\u3000" <= c <= "\u30ff" or "\uff00" <= c <= "\uffef")
    other = len(s) - cjk
    return int(cjk * 0.6 + other * 0.28) + 4


def norm_tool_call(tc: Dict[str, Any]) -> Dict[str, Any]:
    """把 OpenAI 风格的 tool_call 归一成内部结构。"""
    fn = tc.get("function") or {}
    raw = fn.get("arguments")
    args: Any = raw
    if isinstance(raw, str):
        import json
        try:
            args = json.loads(raw) if raw.strip() else {}
        except ValueError:
            args = {}
    return {
        "id": tc.get("id"),
        "name": fn.get("name"),
        "arguments": args,
        "arguments_raw": raw if isinstance(raw, str) else None,
    }


class ModelProvider(abc.ABC):
    """所有 provider 的统一脸。新增一个 provider 只需实现 2–3 个方法。"""

    name: str = "base"

    def __init__(self, name: str, cfg: Optional[Dict[str, Any]] = None):
        self.name = name
        self.cfg = dict(cfg or {})
        self.model = self.cfg.get("model", "")
        self.base_url = str(self.cfg.get("base_url") or "").rstrip("/")
        self.timeout_ms = int(self.cfg.get("timeout_ms", 120000))
        self.retries = int(self.cfg.get("retries", 2))
        self.default_temperature = self.cfg.get("temperature", 0.3)

    # ---------- 必需 ----------
    @abc.abstractmethod
    def chat(self, messages: List[Dict[str, Any]],
             tools: Optional[List[Dict[str, Any]]] = None,
             cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """data = {message, usage, finish_reason, model}"""

    @abc.abstractmethod
    def count_tokens(self, text: Any) -> Dict[str, Any]:
        """data = {tokens:int, exact:bool}"""

    # ---------- 可选 ----------
    def stream_chat(self, messages, tools=None, cfg=None) -> Iterable[Dict[str, Any]]:
        r = self.chat(messages, tools, cfg)
        if not r["ok"]:
            yield {"type": "error", "error": r["error"]}
            return
        d = r["data"]
        if d["message"].get("content"):
            yield {"type": "delta", "text": d["message"]["content"]}
        yield {"type": "done", **d}

    def health(self) -> Dict[str, Any]:
        return envelope(True, {"provider": self.name, "type": self.__class__.__name__,
                               "model": self.model, "base_url": self.base_url})
