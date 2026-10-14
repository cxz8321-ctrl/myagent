# -*- coding: utf-8 -*-
"""显式状态机（M5）：一次任务的相位唯一、可查询、转移有据可查。

相位（规格书 M5）：
    idle -> thinking -> acting -> waiting_approval -> ... -> done / error

铁律：
  - **合法转移表集中一处**（LEGAL），改状态只能走 transition()。
  - 非法转移：strict 模式抛 IllegalTransition；默认只**记录**（记进 violations，
    不改变当前状态）——内核不能因为一次意外转移就崩。
  - 转移线程安全（并发工具可能在别的线程里把状态推回 acting）。

注意：这里的相位（phase）是"运行到哪一步"；`AgentState.status` 仍保留既有对外
语义（四类终止原因：done / max_steps / budget_exhausted / cancelled / error /
loop_guard），两者不冲突。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional


class RunState:
    """相位常量。"""

    IDLE = "idle"
    THINKING = "thinking"
    ACTING = "acting"
    WAITING_APPROVAL = "waiting_approval"
    DONE = "done"
    ERROR = "error"


ALL_STATES = (RunState.IDLE, RunState.THINKING, RunState.ACTING,
              RunState.WAITING_APPROVAL, RunState.DONE, RunState.ERROR)
TERMINAL_STATES = (RunState.DONE, RunState.ERROR)


def _legal_table() -> Dict[str, set]:
    """合法转移表：key 只能去 value 里的相位。"""
    return {
        RunState.IDLE: {RunState.THINKING, RunState.DONE, RunState.ERROR},
        RunState.THINKING: {RunState.ACTING, RunState.DONE, RunState.ERROR},
        RunState.ACTING: {RunState.THINKING, RunState.WAITING_APPROVAL,
                          RunState.DONE, RunState.ERROR},
        RunState.WAITING_APPROVAL: {RunState.ACTING, RunState.ERROR},
        RunState.DONE: set(),
        RunState.ERROR: set(),
    }


LEGAL_TRANSITIONS: Dict[str, set] = _legal_table()


class IllegalTransition(Exception):
    """非法转移：从 src 到 dst 不在合法表里。"""

    def __init__(self, src: str, dst: str):
        self.src, self.dst = src, dst
        super().__init__("非法状态转移：%s -> %s（合法去向：%s）"
                         % (src, dst, sorted(LEGAL_TRANSITIONS.get(src, set())) or "无（终态）"))


def is_legal(src: str, dst: str) -> bool:
    return dst in LEGAL_TRANSITIONS.get(src, set())


class StateMachine:
    """单实例 = 一次任务的相位机。"""

    def __init__(self, state: str = RunState.IDLE, strict: bool = False,
                 on_change: Optional[Callable[[Dict[str, Any]], None]] = None,
                 logger=None):
        if state not in ALL_STATES:
            raise ValueError("未知状态：%s（可选：%s）" % (state, ", ".join(ALL_STATES)))
        self._state = state
        self._lock = threading.RLock()
        self.strict = bool(strict)
        self.on_change = on_change
        self.logger = logger
        self.violations: List[Dict[str, Any]] = []
        self.history: List[Dict[str, Any]] = [
            {"from": None, "to": state, "reason": "init", "ts": int(time.time() * 1000)}
        ]

    # ---------- 查询 ----------
    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def can(self, to: str) -> bool:
        with self._lock:
            return is_legal(self._state, to)

    def legal_next(self) -> List[str]:
        with self._lock:
            return sorted(LEGAL_TRANSITIONS.get(self._state, set()))

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {"state": self._state, "terminal": self._state in TERMINAL_STATES,
                    "legal_next": sorted(LEGAL_TRANSITIONS.get(self._state, set())),
                    "history": list(self.history), "violations": list(self.violations)}

    # ---------- 转移 ----------
    def transition(self, to: str, reason: str = "", meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """尝试转移到 to。

        返回 {ok, from, to, reason}。非法时：strict 抛 IllegalTransition；
        否则记录一条 violations 并保持原状态（返回 ok=False）。
        """
        if to not in ALL_STATES:
            raise ValueError("未知状态：%s" % to)
        with self._lock:
            src = self._state
            if to == src:
                # 原地重复不算转移：幂等通过，不记历史（避免并发里刷屏）
                return {"ok": True, "from": src, "to": to, "reason": reason, "noop": True}
            if not is_legal(src, to):
                rec = {"from": src, "to": to, "reason": reason, "ts": int(time.time() * 1000)}
                self.violations.append(rec)
                self._log("illegal_transition", **rec)
                if self.strict:
                    raise IllegalTransition(src, to)
                return {"ok": False, "from": src, "to": src, "reason": reason, "illegal": True}
            self._state = to
            rec = {"from": src, "to": to, "reason": reason, "ts": int(time.time() * 1000)}
            self.history.append(rec)
            if meta:
                rec_meta = dict(rec, meta=meta)
            else:
                rec_meta = rec
            if self.on_change:
                try:
                    self.on_change(rec_meta)
                except Exception:  # noqa: BLE001 —— 观察者坏了不影响状态推进
                    pass
            return {"ok": True, "from": src, "to": to, "reason": reason}

    def reset(self, state: str = RunState.IDLE) -> None:
        with self._lock:
            self._state = state
            self.violations = []
            self.history = [{"from": None, "to": state, "reason": "reset",
                             "ts": int(time.time() * 1000)}]

    # ---------- 内部 ----------
    def _log(self, msg: str, **fields: Any) -> None:
        if self.logger is None:
            return
        try:
            from runtime.log import log_event
            log_event(self.logger, msg, **fields)
        except Exception:  # noqa: BLE001
            pass
