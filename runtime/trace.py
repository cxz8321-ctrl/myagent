# -*- coding: utf-8 -*-
"""每步 trace 落盘（M5）：`data/traces/<trace_id>.jsonl`，一行一条，可复盘。

一条 trace 至少包含：
    run_start  任务输入、会话、provider/model
    step       本步耗时、token 用量、本步费用、相位
    tool       每次工具调用：名字/参数/耗时/结果大小/错误
    state      相位转移（from -> to）
    final      终止原因、总耗时、总 token、总费用

只追加、不修改；写失败不拖垮内核（trace 是观测，不是主流程）。
"""
from __future__ import annotations

import glob
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional


def _now_ms() -> int:
    return int(time.time() * 1000)


def safe_id(trace_id: str) -> str:
    """trace_id 用作文件名：只保留安全字符，空则回退。"""
    s = "".join(c for c in str(trace_id or "") if c.isalnum() or c in "-_.")
    return s or "unknown"


class TraceWriter:
    """一次任务一个 writer；线程安全（并发工具可能从别的线程补记）。"""

    def __init__(self, trace_id: str, dir_path: Optional[str] = None,
                 enabled: bool = True, logger=None):
        self.trace_id = trace_id
        self.enabled = bool(enabled and dir_path)
        self.dir = os.path.abspath(os.path.expanduser(dir_path)) if dir_path else None
        self.logger = logger
        self._lock = threading.Lock()
        self._seq = 0
        self.path: Optional[str] = None
        if self.enabled:
            try:
                os.makedirs(self.dir, exist_ok=True)
                self.path = os.path.join(self.dir, "%s.jsonl" % safe_id(trace_id))
            except Exception as e:  # noqa: BLE001
                self.enabled = False
                self._warn("trace_init_failed %s" % e)

    def write(self, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        with self._lock:
            self._seq += 1
            rec = dict(record or {})
            rec.setdefault("type", "event")
            rec["trace_id"] = self.trace_id
            rec["seq"] = self._seq
            rec["ts"] = _now_ms()
            try:
                from runtime.text import safe_dumps
                with open(self.path, "a", encoding="utf-8", errors="replace") as f:
                    f.write(safe_dumps(rec) + "\n")
                return rec
            except Exception as e:  # noqa: BLE001
                self._warn("trace_write_failed %s" % e)
                return None

    # ---- 语义化便捷方法 ----
    def run_start(self, **fields: Any) -> Optional[Dict[str, Any]]:
        return self.write(dict({"type": "run_start"}, **fields))

    def step(self, **fields: Any) -> Optional[Dict[str, Any]]:
        return self.write(dict({"type": "step"}, **fields))

    def tool(self, **fields: Any) -> Optional[Dict[str, Any]]:
        return self.write(dict({"type": "tool"}, **fields))

    def state(self, **fields: Any) -> Optional[Dict[str, Any]]:
        return self.write(dict({"type": "state"}, **fields))

    def final(self, **fields: Any) -> Optional[Dict[str, Any]]:
        return self.write(dict({"type": "final"}, **fields))

    def error(self, **fields: Any) -> Optional[Dict[str, Any]]:
        return self.write(dict({"type": "error"}, **fields))

    def close(self) -> None:
        return None

    def _warn(self, msg: str) -> None:
        if self.logger is None:
            return
        try:
            self.logger.warning(msg)
        except Exception:  # noqa: BLE001
            pass


class TraceStore:
    """读 trace：复盘、查最近一条、为 /resume 出方案。"""

    def __init__(self, dir_path: Optional[str] = None):
        self.dir = os.path.abspath(os.path.expanduser(dir_path)) if dir_path else None

    def path_for(self, trace_id: str) -> Optional[str]:
        if not self.dir:
            return None
        exact = os.path.join(self.dir, "%s.jsonl" % safe_id(trace_id))
        if os.path.isfile(exact):
            return exact
        hits = sorted(glob.glob(os.path.join(self.dir, "%s*.jsonl" % safe_id(trace_id))))
        return hits[0] if len(hits) == 1 else None

    def load(self, trace_id: str) -> List[Dict[str, Any]]:
        p = self.path_for(trace_id)
        if not p:
            return []
        out: List[Dict[str, Any]] = []
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out

    def list_traces(self, limit: int = 20) -> List[Dict[str, Any]]:
        if not self.dir or not os.path.isdir(self.dir):
            return []
        files = sorted(glob.glob(os.path.join(self.dir, "*.jsonl")),
                       key=lambda p: os.path.getmtime(p), reverse=True)[:limit]
        out = []
        for p in files:
            out.append({"trace_id": os.path.basename(p)[:-6], "path": p,
                        "mtime": int(os.path.getmtime(p) * 1000)})
        return out

    def latest_trace_id(self) -> Optional[str]:
        lst = self.list_traces(limit=1)
        return lst[0]["trace_id"] if lst else None

    # ---- 复盘 / 恢复 ----
    @staticmethod
    def summarize(records: List[Dict[str, Any]]) -> Dict[str, Any]:
        run = {}
        final = {}
        steps = []
        tools = []
        for r in records or []:
            t = r.get("type")
            if t == "run_start":
                run = r
            elif t == "step":
                steps.append(r)
            elif t == "tool":
                tools.append(r)
            elif t == "final":
                final = r
        return {
            "trace_id": (records[0].get("trace_id") if records else None),
            "input": run.get("input"),
            "session_id": run.get("session_id"),
            "provider": run.get("provider"),
            "model": run.get("model"),
            "status": final.get("status"),
            "termination": final.get("termination") or final.get("status"),
            "steps": len(steps),
            "tools": len(tools),
            "elapsed_ms": final.get("elapsed_ms"),
            "usage": final.get("usage"),
            "cost": final.get("cost"),
            "step_details": [{"step": s.get("step"), "duration_ms": s.get("duration_ms"),
                              "usage": s.get("usage"), "cost": s.get("cost")} for s in steps],
            "tool_details": [{"name": x.get("name"), "latency_ms": x.get("latency_ms"),
                              "ok": x.get("ok"), "error": x.get("error")} for x in tools],
        }

    def replay(self, trace_id: str) -> Dict[str, Any]:
        return self.summarize(self.load(trace_id))

    def plan_resume(self, trace_id: str) -> Optional[Dict[str, Any]]:
        """按 trace_id 出一份"接着跑"的方案（会话 + 原输入 + 是否可续）。"""
        recs = self.load(trace_id)
        if not recs:
            return None
        s = self.summarize(recs)
        status = s.get("termination") or s.get("status")
        resumable = status in ("cancelled", "max_steps", "budget_exhausted", "error", "loop_guard")
        return {
            "trace_id": trace_id,
            "session_id": s.get("session_id"),
            "input": s.get("input"),
            "status": status,
            "provider": s.get("provider"),
            "model": s.get("model"),
            "resumable": bool(resumable),
            "resume_prompt": "请接着上面未完成的任务继续，不要重复已经做完的部分。",
        }


def plan_resume(trace_id: str, dir_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    return TraceStore(dir_path).plan_resume(trace_id)


def last_trace_id(dir_path: Optional[str] = None) -> Optional[str]:
    return TraceStore(dir_path).latest_trace_id()
