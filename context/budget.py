# -*- coding: utf-8 -*-
"""预算分配器：给系统提示 / 记忆 / 工具结果 / 对话历史 四家分 token 配额。

窗口是有限资源，装得下才用得上。总配额 = total - reserve_output（给模型留的输出余量）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

FAMILIES = ("system", "memory", "tools", "history")

DEFAULT_WEIGHTS = {"system": 0.15, "memory": 0.25, "tools": 0.15, "history": 0.45}


class Budget:
    def __init__(self, total_tokens: int = 32768, reserve_output: int = 2048,
                 weights: Optional[Dict[str, float]] = None):
        self.total = int(total_tokens)
        self.reserve_output = int(reserve_output)
        self.weights = dict(DEFAULT_WEIGHTS)
        if weights:
            for k, v in weights.items():
                if k in FAMILIES:
                    self.weights[k] = float(v)

    @property
    def available(self) -> int:
        return max(0, self.total - self.reserve_output)

    def quota(self, family: str) -> int:
        return int(self.available * self.weights.get(family, 0.0))

    def to_dict(self) -> Dict[str, Any]:
        return {"total": self.total, "reserve_output": self.reserve_output,
                "available": self.available, "weights": dict(self.weights),
                "quota": {f: self.quota(f) for f in FAMILIES}}
