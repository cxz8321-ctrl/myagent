# -*- coding: utf-8 -*-
"""审计：一张表横切（规格书 B.6）。

优先落 sqlite 的 audit_log（M2 已建表）；没有 store 时退化到 JSONL 追加文件，
保证任何环境下"敏感操作可回溯"。写入只追加，不修改、不删除。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional


class AuditLog:
    def __init__(self, store=None, path: Optional[str] = None, logger=None):
        self.store = store
        self.logger = logger
        # M5：并发工具会同时回写审计，串行化写，避免 sqlite/文件交叉踩踏
        self._lock = threading.Lock()
        if store is None:
            base = os.path.expanduser(path or os.path.join("data", "audit.jsonl"))
        else:
            base = path
        self.path = os.path.abspath(os.path.expanduser(base)) if base else None
        if self.path:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)

    def write(self, actor: str, target_type: str, action: str, target_id: str = "",
              detail: Any = None, approved: Any = None) -> bool:
        """追加一条审计。失败不抛（审计不能拖垮主流程），返回是否落盘成功。"""
        d = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False, default=str)
        try:
            with self._lock:
                if self.store is not None:
                    self.store.audit(actor, target_type, action, target_id, d[:2000], approved)
                    return True
                row = {"ts": int(time.time() * 1000), "actor": actor, "target_type": target_type,
                       "action": action, "target_id": target_id, "detail": d[:2000], "approved": approved}
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                return True
        except Exception as e:  # noqa: BLE001
            if self.logger:
                try:
                    self.logger.warning("audit_write_failed %s" % e)
                except Exception:
                    pass
            return False

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        if self.store is not None:
            return self.store.audit_recent(limit=limit)
        out: List[Dict[str, Any]] = []
        if self.path and os.path.isfile(self.path):
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-limit:]
            for ln in lines:
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    continue
        return out

    # 便捷别名：gate / executor 直接调
    def tool_call(self, name: str, args: Any, decision: str, approved: Any, latency_ms: Any = None,
                  result_ok: Any = None, reason: str = "") -> bool:
        return self.write("agent", "tool", "call", name,
                          {"args": args, "decision": decision, "latency_ms": latency_ms,
                           "result_ok": result_ok, "reason": reason}, approved)
