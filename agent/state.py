# -*- coding: utf-8 -*-
"""一次任务的全部状态。所有层共享它，不用全局变量。

可序列化落盘、可恢复（M5 起用得上）。
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AgentState:
    session_id: str
    trace_id: str
    messages: List[Dict[str, Any]] = field(default_factory=list)
    step_count: int = 0
    budget_left: int = 32768
    artifacts: List[Any] = field(default_factory=list)
    status: str = "idle"          # 终止口径（对外契约）：done/max_steps/budget_exhausted/cancelled/error/loop_guard
    phase: str = "idle"           # M5 显式状态机相位：idle/thinking/acting/waiting_approval/done/error
    termination: Optional[str] = None   # M5：与 status 同值，显式记录终止原因
    error: Optional[Dict[str, Any]] = None
    usage_total: Dict[str, int] = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0})
    cost_total: Dict[str, Any] = field(default_factory=dict)   # M5：本次任务费用
    trace_path: Optional[str] = None                            # M5：trace 落盘路径
    context_stats: Dict[str, Any] = field(default_factory=dict)
    memory_block: str = ""
    memory_ids: List[str] = field(default_factory=list)
    memory_written: List[Dict[str, Any]] = field(default_factory=list)
    summary_block: str = ""
    procedural_block: str = ""
    issuer: str = "owner"          # 谁派的活：owner / assistant / external / self（决定权限上限）
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AgentState":
        known = {k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def add_usage(self, usage: Optional[Dict[str, Any]]) -> None:
        if not usage:
            return
        for k in ("prompt_tokens", "completion_tokens"):
            v = usage.get(k) or 0
            self.usage_total[k] = self.usage_total.get(k, 0) + int(v)
        used = int((usage.get("total_tokens") or 0) or
                   ((usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)))
        self.budget_left = max(0, self.budget_left - used)
