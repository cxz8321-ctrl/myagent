# -*- coding: utf-8 -*-
"""示例渠道适配器：通用 webhook 风格。

入参：{"user_id": "...", "text": "..."}（兼容 message/content 字段）
出参：{"type": "reply", "text": "...", ...}

只做字段搬运与形状转换，不做任何判断/检索/生成。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .base import ChannelAdapter


class WebhookAdapter(ChannelAdapter):
    name = "webhook"

    def translate_in(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        raw = raw or {}
        text = raw.get("text")
        if text is None:
            text = raw.get("message")
        if text is None:
            text = raw.get("content")
        user_id = raw.get("user_id") or raw.get("user") or "anonymous"
        return {
            "type": "message",
            "channel": self.name,
            "user_id": str(user_id),
            "text": "" if text is None else str(text),
            "session_id": raw.get("session_id"),
            "meta": {"raw_keys": sorted(raw.keys())},
        }

    def translate_out(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        ev = event or {}
        t = ev.get("type")
        p = ev.get("payload") or {}
        if t == "final":
            return {"type": "reply", "channel": self.name,
                    "text": p.get("answer") or "", "status": p.get("status"),
                    "session_id": p.get("session_id")}
        if t == "error":
            e = p.get("error") or {}
            return {"type": "error", "channel": self.name,
                    "text": e.get("message") or "出错了", "code": e.get("code")}
        if t == "thought":
            return {"type": "thought", "text": p.get("text") or ""}
        if t == "tool_call":
            return {"type": "tool_call", "name": p.get("name"),
                    "arguments": p.get("arguments")}
        return None
