# -*- coding: utf-8 -*-
"""中断令牌（M5）：优雅退出用。

一次任务一个 token；谁都可以 `cancel()`（CLI 的 /cancel、信号、看门狗），
循环在每个可能的长耗时段落检查 `cancelled`，命中就收尾落盘、不崩。
"""
from __future__ import annotations

import threading
import time
from typing import Optional


class CancelToken:
    def __init__(self):
        self._event = threading.Event()
        self.reason: str = ""
        self.cancelled_at: Optional[int] = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str = "user_cancel") -> bool:
        """置位；重复调用只记第一次原因。返回是否是本次置位。"""
        first = not self._event.is_set()
        if first:
            self.reason = str(reason or "user_cancel")
            self.cancelled_at = int(time.time() * 1000)
        self._event.set()
        return first

    def reset(self) -> None:
        self._event.clear()
        self.reason = ""
        self.cancelled_at = None

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._event.wait(timeout)

    def to_dict(self) -> dict:
        return {"cancelled": self.cancelled, "reason": self.reason,
                "cancelled_at": self.cancelled_at}
