# -*- coding: utf-8 -*-
"""provider 装配：配置驱动。换实现只改 BUILDERS 一处。"""
from __future__ import annotations

from typing import Any, Dict

from providers.deepseek import DeepSeekProvider
from providers.llamacpp import LlamaCppProvider
from providers.ollama import OllamaProvider
from providers.openai_compat import OpenAICompatProvider

BUILDERS = {
    "deepseek": DeepSeekProvider,
    "ollama": OllamaProvider,
    "llamacpp": LlamaCppProvider,
    "local": LlamaCppProvider,          # 配置里 type: local 也给本地推理
    "openai": OpenAICompatProvider,
    "openai_compat": OpenAICompatProvider,
    "vllm": OpenAICompatProvider,      # vLLM 走 OpenAI 兼容协议，先用同一个薄实现
}


def build_provider(name: str, pcfg: Dict[str, Any]):
    pcfg = dict(pcfg or {})
    ptype = pcfg.get("type", name)
    cls = BUILDERS.get(ptype)
    if cls is None:
        raise ValueError("未知 provider 类型：%s（可选：%s）" % (ptype, ", ".join(sorted(BUILDERS))))
    if cls is OpenAICompatProvider:
        from runtime.secrets import get_secret
        key = pcfg.get("api_key") or get_secret(pcfg.get("api_key_ref") or name)
        return cls(name, pcfg, api_key=key)
    return cls(name, pcfg)


def build_all(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {n: build_provider(n, p) for n, p in (cfg.get("providers") or {}).items()}
