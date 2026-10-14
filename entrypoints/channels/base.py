# -*- coding: utf-8 -*-
"""渠道适配器：只做「外部消息 <-> 内部事件」的翻译，不含任何业务逻辑。

契约（规格书 3.8-2）：
  - translate_in(raw)  外部渠道的一条消息 -> 内部标准消息 dict
       {"type": "message", "channel", "user_id", "text", "session_id", "meta"}
  - translate_out(event) 内部事件（EventBus 事件）-> 渠道出参 dict（或 None 表示不推）
  适配器不得调用内核 / 模型 / 记忆；换渠道只加一个适配器，内核零改动。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class ChannelAdapter:
    """极薄适配器基类。子类只实现两个纯翻译函数。"""

    name = "base"

    def translate_in(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def translate_out(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        raise NotImplementedError
