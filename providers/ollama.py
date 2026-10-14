# -*- coding: utf-8 -*-
"""Ollama provider —— 本地推理（/api/chat）。"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

from providers.base import (MODEL_INTERNAL, MODEL_TIMEOUT, MODEL_UNAVAILABLE,
                            ModelProvider, envelope, err, estimate_tokens)


class OllamaProvider(ModelProvider):
    default_base_url = "http://127.0.0.1:11434"
    default_model = "qwen3:4b"

    def __init__(self, name: str, cfg: Optional[Dict[str, Any]] = None):
        cfg = dict(cfg or {})
        cfg.setdefault("base_url", self.default_base_url)
        cfg.setdefault("model", self.default_model)
        super().__init__(name, cfg)

    @staticmethod
    def _to_ollama(m: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {"role": m.get("role"), "content": m.get("content") or ""}
        if m.get("tool_calls"):
            out["tool_calls"] = [
                {"function": {"name": tc.get("name"), "arguments": tc.get("arguments")}}
                for tc in m["tool_calls"]
            ]
        if m.get("name"):
            out["tool_name"] = m["name"]
        return out

    def _payload(self, messages, tools, cfg) -> Dict[str, Any]:
        cfg = dict(cfg or {})
        p: Dict[str, Any] = {
            "model": cfg.get("model") or self.model,
            "messages": [self._to_ollama(m) for m in messages],
            "stream": False,
            "options": {"temperature": cfg.get("temperature", self.default_temperature)},
        }
        if cfg.get("max_tokens"):
            p["options"]["num_predict"] = cfg["max_tokens"]
        if tools:
            p["tools"] = tools
        return p

    def chat(self, messages, tools=None, cfg=None) -> Dict[str, Any]:
        body = self._payload(messages, tools, cfg)
        timeout = int((cfg or {}).get("timeout_ms", self.timeout_ms)) / 1000.0
        t0 = time.time()
        last = err(MODEL_INTERNAL, "no attempt", retryable=False)
        for attempt in range(max(1, self.retries + 1)):
            try:
                r = requests.post(self.base_url + "/api/chat", json=body, timeout=timeout)
                if r.status_code == 200:
                    return self._parse(r.json(), t0, body["model"])
                if r.status_code in (400, 404):
                    return envelope(False, error=err("MODEL_BAD_REQUEST",
                                                     "Ollama HTTP %s（模型名不对？）" % r.status_code,
                                                     detail=r.text[:300]),
                                    meta={"latency_ms": int((time.time() - t0) * 1000)})
                last = err(MODEL_UNAVAILABLE, "Ollama HTTP %s" % r.status_code, detail=r.text[:200])
            except requests.Timeout:
                last = err(MODEL_TIMEOUT, "Ollama 超时 %.1fs" % timeout)
            except requests.RequestException as e:
                last = err(MODEL_UNAVAILABLE, "Ollama 连不上（serve 没起？）%s" % e.__class__.__name__,
                           detail=str(e)[:200])
            if not last.get("retryable") or attempt >= self.retries:
                break
            time.sleep(min(6.0, 0.8 * (2 ** attempt)))
        return envelope(False, error=last,
                        meta={"latency_ms": int((time.time() - t0) * 1000), "provider": self.name})

    def _parse(self, raw: Dict[str, Any], t0: float, model: str) -> Dict[str, Any]:
        m = raw.get("message") or {}
        message: Dict[str, Any] = {"role": "assistant", "content": m.get("content")}
        if m.get("thinking"):
            message["reasoning_content"] = m["thinking"]
        tcs = m.get("tool_calls") or []
        if tcs:
            from providers.base import norm_tool_call
            message["tool_calls"] = [norm_tool_call(t) if t.get("id") else
                                     {"id": None, "name": (t.get("function") or {}).get("name"),
                                      "arguments": (t.get("function") or {}).get("arguments") or {},
                                      "arguments_raw": None} for t in tcs]
        pe = raw.get("prompt_eval_count")
        ev = raw.get("eval_count")
        return envelope(True, data={
            "message": message,
            "usage": {"prompt_tokens": pe, "completion_tokens": ev,
                      "total_tokens": (pe or 0) + (ev or 0)},
            "finish_reason": raw.get("done_reason") or ("tool_calls" if tcs else "stop"),
            "model": raw.get("model") or model,
        }, meta={"latency_ms": int((time.time() - t0) * 1000), "provider": self.name,
                 "ts": int(time.time() * 1000)})

    def count_tokens(self, text: Any) -> Dict[str, Any]:
        return envelope(True, {"tokens": estimate_tokens(text), "exact": False})

    # ---- 本地模型的额外能力：列出本机已拉取的模型 / 拉取状态 ----
    def list_local(self) -> List[Dict[str, Any]]:
        try:
            r = requests.get(self.base_url + "/api/tags", timeout=5)
            if r.status_code != 200:
                return []
            return (r.json().get("models") or [])
        except requests.RequestException:
            return []

    def health(self) -> Dict[str, Any]:
        r = super().health()
        try:
            r["data"]["running"] = len(self.list_local()) >= 0 and requests.get(
                self.base_url + "/api/tags", timeout=3).status_code == 200
        except Exception:
            r["data"]["running"] = False
        r["data"]["models"] = [m.get("name") for m in self.list_local()][:20]
        return r
