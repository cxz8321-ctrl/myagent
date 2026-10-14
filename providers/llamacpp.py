# -*- coding: utf-8 -*-
"""llama.cpp provider —— 本地推理（GGUF 量化，CPU 也能跑）。

为什么不用 Ollama：本机没装运行时，且 GitHub/ollama.com 的下载通道当前不可达；
llama.cpp 走 ModelScope 拿模型 + PyPI 装运行时，全程不依赖代理。
以后代理通了、或换 Windows 侧用核显（Ollama），只需在配置里加一个 provider，
内核与上层零改动 —— 这正是模型适配层存在的意义。
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from providers.base import (MODEL_INTERNAL, MODEL_TIMEOUT, MODEL_UNAVAILABLE,
                            ModelProvider, envelope, err, estimate_tokens, norm_tool_call)


class LlamaCppProvider(ModelProvider):
    def __init__(self, name: str, cfg: Optional[Dict[str, Any]] = None):
        cfg = dict(cfg or {})
        cfg.setdefault("model_path", "~/.local/models/Qwen3-4B-Q4_K_M.gguf")
        cfg.setdefault("n_ctx", 8192)
        cfg.setdefault("n_threads", 0)
        cfg.setdefault("n_batch", 512)
        cfg.setdefault("no_think", True)
        super().__init__(name, cfg)
        self.model_path = os.path.expanduser(str(self.cfg["model_path"]))
        self._llm = None

    # ---------- 懒加载：第一次调用才把模型读进内存 ----------
    def _load(self) -> None:
        if self._llm is not None:
            return
        from llama_cpp import Llama  # 延迟导入：没装也不影响其他 provider
        if not os.path.isfile(self.model_path):
            raise FileNotFoundError("模型文件不存在：%s" % self.model_path)
        kw: Dict[str, Any] = {
            "model_path": self.model_path,
            "n_ctx": int(self.cfg["n_ctx"]),
            "n_batch": int(self.cfg["n_batch"]),
            "verbose": False,
            "logits_all": False,
        }
        nt = int(self.cfg.get("n_threads") or 0)
        if nt > 0:
            kw["n_threads"] = nt
        if self.cfg.get("chat_format"):
            kw["chat_format"] = self.cfg["chat_format"]
        self._llm = Llama(**kw)

    # ---------- 线格式转换 ----------
    @staticmethod
    def _to_llama(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for m in messages:
            msg: Dict[str, Any] = {"role": m.get("role"), "content": m.get("content") or ""}
            if m.get("tool_calls"):
                msg["tool_calls"] = [
                    {"type": "function", "function": {"name": tc.get("name"),
                                                      "arguments": tc.get("arguments")}}
                    for tc in m["tool_calls"]]
            if m.get("role") == "tool" and m.get("name"):
                msg["name"] = m["name"]
            out.append(msg)
        return out

    def _kwargs(self, messages, tools, cfg) -> Dict[str, Any]:
        cfg = dict(cfg or {})
        msgs = self._to_llama(messages)
        if self.cfg.get("no_think"):
            # Qwen3 软开关：/no_think 写在 system 里；有些模板认 chat_template_kwargs，两个都上
            hit = False
            for m in msgs:
                if m.get("role") == "system":
                    m["content"] = "/no_think\n" + (m.get("content") or "")
                    hit = True
                    break
            if not hit:
                msgs.insert(0, {"role": "system", "content": "/no_think"})
        kw: Dict[str, Any] = {
            "messages": msgs,
            "temperature": cfg.get("temperature", self.default_temperature),
            "max_tokens": int(cfg.get("max_tokens") or self.cfg.get("max_tokens") or 1024),
            "stream": False,
        }
        if tools:
            kw["tools"] = tools
        if self.cfg.get("no_think"):
            kw["chat_template_kwargs"] = {"enable_thinking": False}
        return kw

    def chat(self, messages, tools=None, cfg=None) -> Dict[str, Any]:
        t0 = time.time()
        try:
            self._load()
        except FileNotFoundError as e:
            return envelope(False, error=err("MODEL_UNAVAILABLE", str(e), retryable=False),
                            meta={"latency_ms": int((time.time() - t0) * 1000)})
        except ImportError as e:
            return envelope(False, error=err(MODEL_UNAVAILABLE,
                                             "没装 llama-cpp-python：%s" % e, retryable=False),
                            meta={"latency_ms": int((time.time() - t0) * 1000)})
        except Exception as e:  # 模型加载失败
            return envelope(False, error=err(MODEL_INTERNAL, "模型加载失败：%s" % e, retryable=False),
                            meta={"latency_ms": int((time.time() - t0) * 1000)})

        kw = self._kwargs(messages, tools, cfg)
        try:
            try:
                raw = self._llm.create_chat_completion(**kw)
            except TypeError:
                kw.pop("chat_template_kwargs", None)     # 老版本不支持就退回
                raw = self._llm.create_chat_completion(**kw)
        except Exception as e:
            return envelope(False, error=err(MODEL_INTERNAL, "推理失败：%s" % e, retryable=False),
                            meta={"latency_ms": int((time.time() - t0) * 1000)})
        return self._parse(raw, t0)

    def _parse(self, raw: Dict[str, Any], t0: float) -> Dict[str, Any]:
        choice = (raw.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = msg.get("content")
        reasoning = msg.get("reasoning_content")
        if content and "<think" in content:
            # 兜底：模型还是吐了思考链，剥出来单独放（不污染正文）
            import re
            for m in re.finditer(r"<think[^>]*>(.*?)</think[^>]*>", content, re.S):
                reasoning = (reasoning or "") + m.group(1)
            content = re.sub(r"<think[^>]*>.*?</think[^>]*>", "", content, flags=re.S)
            content = re.sub(r"</?think[^>]*>", "", content).strip()
        message: Dict[str, Any] = {"role": "assistant", "content": content}
        if reasoning:
            message["reasoning_content"] = reasoning
        tcs = msg.get("tool_calls") or []
        if tcs:
            message["tool_calls"] = [norm_tool_call(t) for t in tcs]
        elif content and "<tool_call>" in content:
            # 有些模板（Qwen3 GGUF 走 llama.cpp 时）把工具调用以文本吐出，这里翻译成结构化调用
            import json as _json
            import re as _re
            pat = _re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", _re.S)
            calls = []
            for i, m in enumerate(pat.finditer(content)):
                raw_txt = m.group(1).strip()
                try:
                    obj = _json.loads(raw_txt)
                except ValueError:
                    continue
                args = obj.get("arguments")
                if isinstance(args, str):
                    try:
                        args = _json.loads(args)
                    except ValueError:
                        pass
                calls.append({"id": "call_%d" % i, "name": obj.get("name"),
                              "arguments": args or {}, "arguments_raw": raw_txt})
            if calls:
                message["tool_calls"] = calls
                message["content"] = pat.sub("", content).strip() or None
        u = raw.get("usage") or {}
        return envelope(True, data={
            "message": message,
            "usage": {"prompt_tokens": u.get("prompt_tokens"),
                      "completion_tokens": u.get("completion_tokens"),
                      "total_tokens": u.get("total_tokens")},
            "finish_reason": choice.get("finish_reason"),
            "model": self.cfg.get("model") or os.path.basename(self.model_path),
        }, meta={"latency_ms": int((time.time() - t0) * 1000), "provider": self.name,
                 "ts": int(time.time() * 1000)})

    def count_tokens(self, text: Any) -> Dict[str, Any]:
        return envelope(True, {"tokens": estimate_tokens(text), "exact": False})

    def health(self) -> Dict[str, Any]:
        r = super().health()
        r["data"].update({
            "model_path": self.model_path,
            "exists": os.path.isfile(self.model_path),
            "size_mb": round(os.path.getsize(self.model_path) / 1e6, 1) if os.path.isfile(self.model_path) else 0,
            "loaded": self._llm is not None,
            "n_ctx": self.cfg.get("n_ctx"),
        })
        try:
            import llama_cpp
            r["data"]["runtime"] = "llama-cpp-python " + getattr(llama_cpp, "__version__", "?")
        except Exception:
            r["data"]["runtime"] = None
        return r
