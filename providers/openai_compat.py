# -*- coding: utf-8 -*-
"""OpenAI 兼容协议的公共实现（DeepSeek / vLLM / 多数国产 API 都是这套）。

只做格式翻译，不写业务逻辑。重试与降级都在这一层。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

from providers.base import (MODEL_AUTH, MODEL_BAD_REQUEST, MODEL_INTERNAL,
                            MODEL_RATE_LIMIT, MODEL_TIMEOUT, MODEL_UNAVAILABLE,
                            ModelProvider, envelope, err, estimate_tokens, norm_tool_call)


class OpenAICompatProvider(ModelProvider):
    default_base_url = "https://api.openai.com/v1"
    default_model = "gpt-4o-mini"

    def __init__(self, name: str, cfg: Optional[Dict[str, Any]] = None, api_key: Optional[str] = None):
        cfg = dict(cfg or {})
        cfg.setdefault("base_url", self.default_base_url)
        cfg.setdefault("model", self.default_model)
        super().__init__(name, cfg)
        self.api_key = api_key

    # ---------- HTTP ----------
    def _endpoint(self) -> str:
        base = self.base_url
        if base.endswith("/chat/completions"):
            return base
        if not base.endswith("/v1"):
            base = base + "/v1"
        return base + "/chat/completions"

    @staticmethod
    def _to_wire(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """内部消息 → 厂商线格式。归一化差异只在这一处抹平。"""
        import json as _json
        out: List[Dict[str, Any]] = []
        for m in messages:
            msg: Dict[str, Any] = {"role": m.get("role"), "content": m.get("content")}
            if m.get("tool_calls"):
                wire = []
                for tc in m["tool_calls"]:
                    if "function" in tc and isinstance(tc.get("function"), dict):
                        wire.append(tc)          # 已经是线格式
                        continue
                    args = tc.get("arguments")
                    if not isinstance(args, str):
                        args = _json.dumps(args or {}, ensure_ascii=False)
                    wire.append({"id": tc.get("id"), "type": "function",
                                 "function": {"name": tc.get("name"), "arguments": args}})
                msg["tool_calls"] = wire
            if m.get("role") == "tool":
                if m.get("tool_call_id"):
                    msg["tool_call_id"] = m["tool_call_id"]
                elif m.get("name"):
                    msg["name"] = m["name"]
            out.append(msg)
        return out

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = "Bearer " + self.api_key
        return h

    def _payload(self, messages, tools, cfg) -> Dict[str, Any]:
        cfg = dict(cfg or {})
        p: Dict[str, Any] = {
            "model": cfg.get("model") or self.model,
            "messages": self._to_wire(messages),
            "temperature": cfg.get("temperature", self.default_temperature),
            "stream": False,
        }
        if cfg.get("max_tokens"):
            p["max_tokens"] = cfg["max_tokens"]
        if tools:
            p["tools"] = tools
            p["tool_choice"] = cfg.get("tool_choice", "auto")
        return p

    def chat(self, messages, tools=None, cfg=None) -> Dict[str, Any]:
        url = self._endpoint()
        body = self._payload(messages, tools, cfg)
        timeout = int((cfg or {}).get("timeout_ms", self.timeout_ms)) / 1000.0
        t0 = time.time()
        last = err(MODEL_INTERNAL, "no attempt", retryable=False, detail=None)
        for attempt in range(max(1, self.retries + 1)):
            try:
                r = requests.post(url, headers=self._headers(), json=body, timeout=timeout)
                if r.status_code == 200:
                    return self._parse(r.json(), t0, body["model"])
                if r.status_code in (401, 403):
                    return envelope(False, error=err(MODEL_AUTH, "鉴权失败 HTTP %s" % r.status_code,
                                                     detail=r.text[:300]),
                                    meta={"latency_ms": int((time.time() - t0) * 1000)})
                if r.status_code in (400, 422):
                    return envelope(False, error=err(MODEL_BAD_REQUEST, "请求非法 HTTP %s" % r.status_code,
                                                     detail=r.text[:300]),
                                    meta={"latency_ms": int((time.time() - t0) * 1000)})
                if r.status_code == 429:
                    last = err(MODEL_RATE_LIMIT, "限流 HTTP 429", detail=r.text[:200])
                elif r.status_code >= 500:
                    last = err(MODEL_UNAVAILABLE, "服务端错误 HTTP %s" % r.status_code, detail=r.text[:200])
                else:
                    last = err(MODEL_INTERNAL, "意外状态码 %s" % r.status_code, retryable=False,
                               detail=r.text[:200])
            except requests.Timeout:
                last = err(MODEL_TIMEOUT, "请求超时 %.1fs" % timeout)
            except requests.RequestException as e:
                last = err(MODEL_UNAVAILABLE, "网络错误 %s" % e.__class__.__name__, detail=str(e)[:200])

            if not last.get("retryable") or attempt >= self.retries:
                break
            time.sleep(min(8.0, 0.8 * (2 ** attempt)))
        return envelope(False, error=last,
                        meta={"latency_ms": int((time.time() - t0) * 1000), "provider": self.name})

    def _parse(self, raw: Dict[str, Any], t0: float, model: str) -> Dict[str, Any]:
        choice = (raw.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        message: Dict[str, Any] = {"role": "assistant", "content": msg.get("content")}
        if msg.get("reasoning_content"):
            message["reasoning_content"] = msg["reasoning_content"]
        tcs = msg.get("tool_calls") or []
        if tcs:
            message["tool_calls"] = [norm_tool_call(t) for t in tcs]
        u = raw.get("usage") or {}
        return envelope(True, data={
            "message": message,
            "usage": {"prompt_tokens": u.get("prompt_tokens"),
                      "completion_tokens": u.get("completion_tokens"),
                      "total_tokens": u.get("total_tokens")},
            "finish_reason": choice.get("finish_reason"),
            "model": raw.get("model") or model,
        }, meta={"latency_ms": int((time.time() - t0) * 1000), "provider": self.name,
                 "ts": int(time.time() * 1000)})

    def count_tokens(self, text: Any) -> Dict[str, Any]:
        return envelope(True, {"tokens": estimate_tokens(text), "exact": False})
