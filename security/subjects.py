# -*- coding: utf-8 -*-
"""派发者（issuer）与权限上限：让 myagent 成为"下位"。

调用者权限口径：
    调用者(owner) > 上层调用方(assistant) > 外部助手(external) > 本程序自身(self) > 未识别(unknown)

规则：
  1. **按派发者封顶**：谁派的活，就只能用到那一档权限。
       owner   → danger（危险操作仍要二次确认）
       assistant/external  → write（只读 + 写入需确认；**危险类一律不放行**，要干危险活回到 owner）
       self    → read（它自己发起的一切，只读）
       unknown → read（认不出来就当最严格）
  2. **长期授权只有 owner 能给**：「永久允许 / 永久拒绝」这类落盘口径，assistant/external/self 都无权授予，
     它们只能给"本次允许"。—— 防止上层调用方把它养成一把万能钥匙。
  3. 判定是纯函数：只比级别、不改状态。

时间：所有判定都是"上限封顶"，不会把低权限抬成高权限。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from security.policy import DANGER, READ, WRITE

OWNER, ASSISTANT, EXTERNAL, SELF, UNKNOWN = "owner", "assistant", "external", "self", "unknown"

# 级别高低（只用于封顶比较）
RANK: Dict[str, int] = {READ: 1, WRITE: 2, DANGER: 3}

DEFAULT_SUBJECTS: Dict[str, Dict[str, Any]] = {
    OWNER: {"max_tier": DANGER, "can_grant_persistent": True, "note": "本地管理员"},
    ASSISTANT: {"max_tier": WRITE, "can_grant_persistent": False, "note": "上层调用者"},
    EXTERNAL: {"max_tier": WRITE, "can_grant_persistent": False, "note": "外部编码工具"},
    SELF: {"max_tier": READ, "can_grant_persistent": False, "note": "它自己发起的"},
    UNKNOWN: {"max_tier": READ, "can_grant_persistent": False, "note": "未识别来源（按最严）"},
}


class SubjectPolicy:
    def __init__(self, cfg: Dict[str, Any] = None):
        cfg = dict(cfg or {})
        self.subjects: Dict[str, Dict[str, Any]] = {k: dict(v) for k, v in DEFAULT_SUBJECTS.items()}
        for k, v in (cfg.get("subjects") or {}).items():
            self.subjects.setdefault(str(k), {}).update(v or {})
        self.default_issuer = str(cfg.get("default_issuer") or OWNER)

    # ---- 查询 ----
    def known(self) -> Dict[str, Any]:
        return {k: dict(v) for k, v in self.subjects.items()}

    def cap(self, issuer: Optional[str]) -> Dict[str, Any]:
        key = str(issuer or "").strip().lower() or self.default_issuer
        return dict(self.subjects.get(key) or self.subjects[UNKNOWN] | {"id": UNKNOWN}), key

    def max_rank(self, issuer: Optional[str]) -> int:
        cap, _k = self.cap(issuer)
        return RANK.get(str(cap.get("max_tier", READ)).lower(), 1)

    def can_grant_persistent(self, issuer: Optional[str]) -> bool:
        cap, _k = self.cap(issuer)
        return bool(cap.get("can_grant_persistent"))

    # ---- 封顶 ----
    def clamp(self, tier: str, issuer: Optional[str]) -> Tuple[str, bool, str]:
        """返回 (实际上限内的级别, 是否被削, 说明)。"""
        t = str(tier or READ).lower()
        mx = self.max_rank(issuer)
        if RANK.get(t, 3) <= mx:
            return t, False, ""
        allowed = [k for k, v in RANK.items() if v == mx]
        return (allowed[0] if allowed else READ), True, \
            "派发者 %s 的权限上限是 %s，够不到 %s" % (issuer, allowed[0] if allowed else READ, t)

    def describe(self) -> str:
        lines = []
        for k, v in self.subjects.items():
            lines.append("  %-8s 上限=%-7s 可给永久授权=%s  %s"
                         % (k, v.get("max_tier"), "是" if v.get("can_grant_persistent") else "否",
                            v.get("note") or ""))
        return "\n".join(lines)
