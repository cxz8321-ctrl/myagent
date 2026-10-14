# -*- coding: utf-8 -*-
"""审批钩子：本次允许 / 永久允许 / 拒绝（规格书 3.7 · 审批钩子）。

gate 只出 decision（纯函数、不弹窗）；"永久允许/永久拒绝"的口径落在这里，
存 JSON 文件，跨进程生效。策略可在配置里预置（免每次问）。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

ALLOW_ONCE = "allow_once"
ALLOW_ALWAYS = "allow_always"
DENY_ONCE = "deny_once"
DENY_ALWAYS = "deny_always"
CHOICES = (ALLOW_ONCE, ALLOW_ALWAYS, DENY_ONCE, DENY_ALWAYS)


class ApprovalStore:
    def __init__(self, path: Optional[str] = None, cfg: Dict[str, Any] = None, logger=None):
        cfg = dict(cfg or {})
        self.path = os.path.abspath(os.path.expanduser(
            path or (cfg.get("path") or os.path.join("data", "approvals.json"))))
        self.logger = logger
        self.allow: Dict[str, Any] = {}
        self.deny: Dict[str, Any] = {}
        # 配置预置：security.approvals.allow / deny（列表或映射）
        self.allow.update(_as_map(cfg.get("allow")))
        self.deny.update(_as_map(cfg.get("deny")))
        self._load()

    # ---- 查询 ----
    def lookup(self, name: str) -> Optional[str]:
        """返回 'always_allow' / 'always_deny' / None。"""
        if name in self.deny:
            return "always_deny"
        if name in self.allow:
            return "always_allow"
        return None

    # ---- 记录 ----
    def remember(self, name: str, decision: str) -> None:
        if decision == ALLOW_ALWAYS:
            self.allow[name] = _stamp()
            self.deny.pop(name, None)
        elif decision == DENY_ALWAYS:
            self.deny[name] = _stamp()
            self.allow.pop(name, None)
        else:
            return  # 一次性决定不落盘
        self._save()

    def revoke(self, name: str) -> bool:
        hit = name in self.allow or name in self.deny
        self.allow.pop(name, None)
        self.deny.pop(name, None)
        if hit:
            self._save()
        return hit

    def list(self) -> Dict[str, Any]:
        return {"allow": dict(self.allow), "deny": dict(self.deny), "path": self.path}

    # ---- 持久化 ----
    def _load(self) -> None:
        if not (self.path and os.path.isfile(self.path)):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            self.allow.update(data.get("allow") or {})
            self.deny.update(data.get("deny") or {})
        except Exception as e:  # noqa: BLE001
            if self.logger:
                try:
                    self.logger.warning("approval_load_failed %s" % e)
                except Exception:
                    pass

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"allow": self.allow, "deny": self.deny}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)   # 原子替换
        except Exception as e:  # noqa: BLE001
            if self.logger:
                try:
                    self.logger.warning("approval_save_failed %s" % e)
                except Exception:
                    pass


def _stamp() -> Dict[str, Any]:
    return {"at": int(time.time() * 1000)}


def _as_map(v: Any) -> Dict[str, Any]:
    if not v:
        return {}
    if isinstance(v, dict):
        return {k: (val if isinstance(val, dict) else _stamp()) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return {str(k): _stamp() for k in v}
    return {}
