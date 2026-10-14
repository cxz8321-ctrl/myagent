# -*- coding: utf-8 -*-
"""权限分级表：工具 → 权限级别，集中一处、可审计（规格书 3.7）。

铁律：**没有"未分类"的漏网工具**。
  - 表里没登记、元数据也没声明的工具，一律按最严（danger）处理。
  - 表可被配置覆盖（config.security.tiers / overrides），便于按环境收紧。

级别三级（规格书表 15）：
  read   只读：直接放行，仍记审计
  write  写入：需确认（本次允许 / 永久允许 / 拒绝）
  danger 危险：二次确认 + 沙盒约束 + 重点审计
"""
from __future__ import annotations

from typing import Any, Dict, List

READ = "read"
WRITE = "write"
DANGER = "danger"
TIERS = (READ, WRITE, DANGER)

# 内置工具 → 级别（新增内置工具必须在此登记，否则按 danger 兜底）
DEFAULT_TIERS: Dict[str, str] = {
    # 只读
    "read_file": READ,
    "list_dir": READ,
    "calc": READ,
    "now": READ,
    "http_get": READ,
    # 写入
    "write_file": WRITE,
    "append_note": WRITE,
    "http_post": WRITE,
    # 危险
    "run_command": DANGER,
    "delete_path": DANGER,
    "send_message": DANGER,
}


class PermissionPolicy:
    """纯数据：把工具判到某一级。不弹窗、不落盘（那是 gate / audit 的事）。"""

    def __init__(self, cfg: Dict[str, Any] = None):
        cfg = dict(cfg or {})
        self.tiers: Dict[str, str] = dict(DEFAULT_TIERS)
        self.tiers.update(cfg.get("tiers") or {})
        self.overrides: Dict[str, str] = dict(cfg.get("overrides") or {})
        # 漏网兜底级别：默认最严
        self.default_tier: str = cfg.get("default_tier") or DANGER
        # 级别 → 默认动作（allow / ask / deny），可配置
        self.actions: Dict[str, str] = dict(cfg.get("actions") or {READ: "allow", WRITE: "ask", DANGER: "ask"})
        # 危险级是否要求二次确认（上层渲染更强提示）
        self.second_confirm: bool = bool(cfg.get("second_confirm", True))

    # ---- 判定 ----
    def classify(self, name: str, meta: Dict[str, Any] = None) -> Dict[str, Any]:
        """返回 {tier, source}。source 说明这个级别是哪来的，便于审计与排查。"""
        if not name:
            return {"tier": DANGER, "source": "empty"}
        if name in self.overrides:
            return {"tier": _norm(self.overrides[name]), "source": "override"}
        if name in self.tiers:
            return {"tier": _norm(self.tiers[name]), "source": "table"}
        m = meta or {}
        declared = m.get("permission")
        if declared:
            return {"tier": _norm(declared), "source": "declared"}
        return {"tier": _norm(self.default_tier), "source": "fallback"}

    def action_for(self, tier: str) -> str:
        return self.actions.get(_norm(tier), "ask")

    def second_confirm_for(self, tier: str) -> bool:
        return _norm(tier) == DANGER and self.second_confirm

    # ---- 自检用：揪出漏网 ----
    def audit_tools(self, metas: List[Dict[str, Any]]) -> Dict[str, Any]:
        out: Dict[str, Any] = {"total": len(metas), "fallback": [], "by_tier": {t: 0 for t in TIERS}}
        for m in metas:
            r = self.classify(m.get("name"), m)
            out["by_tier"][r["tier"]] = out["by_tier"].get(r["tier"], 0) + 1
            if r["source"] == "fallback":
                out["fallback"].append(m.get("name"))
        out["clean"] = not out["fallback"]
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {"tiers": self.tiers, "overrides": self.overrides, "default_tier": self.default_tier,
                "actions": self.actions, "second_confirm": self.second_confirm}


def _norm(t: str) -> str:
    t = str(t or "").strip().lower()
    return t if t in TIERS else DANGER
