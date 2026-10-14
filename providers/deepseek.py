# -*- coding: utf-8 -*-
"""DeepSeek provider —— 第一个 provider（云）。

DS 只是第一个 provider，不是唯一的一个；写死 = 以后换模型重写。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from providers.openai_compat import OpenAICompatProvider
from runtime.secrets import get_secret, mask


class DeepSeekProvider(OpenAICompatProvider):
    default_base_url = "https://api.deepseek.com"
    default_model = "deepseek-chat"

    def __init__(self, name: str, cfg: Optional[Dict[str, Any]] = None):
        cfg = dict(cfg or {})
        ref = cfg.get("api_key_ref") or "deepseek"
        key = cfg.get("api_key") or get_secret(ref)
        super().__init__(name, cfg, api_key=key)
        self.key_ref = ref

    def health(self) -> Dict[str, Any]:
        r = super().health()
        r["data"]["has_key"] = bool(self.api_key)
        r["data"]["key_ref"] = self.key_ref
        r["data"]["key_masked"] = mask(self.api_key)
        return r
