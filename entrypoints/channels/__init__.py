# -*- coding: utf-8 -*-
"""渠道适配器注册表：加一个渠道 = 加一个 ChannelAdapter 子类并登记在这里，内核零改动。"""
from __future__ import annotations

from typing import List, Optional

from .base import ChannelAdapter
from .webhook import WebhookAdapter

ADAPTERS = {a.name: a for a in (WebhookAdapter,)}


def get_adapter(name: str) -> Optional[ChannelAdapter]:
    cls = ADAPTERS.get(str(name or "").strip().lower())
    return cls() if cls else None


def list_adapters() -> List[str]:
    return sorted(ADAPTERS)


__all__ = ["ChannelAdapter", "WebhookAdapter", "ADAPTERS", "get_adapter", "list_adapters"]
