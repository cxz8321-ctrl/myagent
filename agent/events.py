# -*- coding: utf-8 -*-
"""事件总线：思考 / 工具调用 / 工具返回 / 最终答案 / 错误 / 状态变化。

订阅者抛异常必须被吞掉并记录 —— 渲染坏了不能拖垮内核。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List

EVENT_TYPES = {"thought", "tool_call", "tool_result", "final", "error",
               "state_change", "approval_required", "step"}


class EventBus:
    def __init__(self):
        self._subs: List[Callable[[Dict[str, Any]], None]] = []
        self.history: List[Dict[str, Any]] = []

    def subscribe(self, handler: Callable[[Dict[str, Any]], None]) -> Callable[[], None]:
        self._subs.append(handler)

        def unsubscribe() -> None:
            try:
                self._subs.remove(handler)
            except ValueError:
                pass

        return unsubscribe

    def emit(self, type_: str, payload: Dict[str, Any], **extra: Any) -> int:
        ev = {"type": type_, "ts": int(time.time() * 1000), "payload": payload or {}}
        ev.update(extra)
        self.history.append(ev)
        delivered = 0
        for h in list(self._subs):
            try:
                h(ev)
                delivered += 1
            except Exception:
                pass
        return delivered
