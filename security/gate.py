# -*- coding: utf-8 -*-
"""权限闸：把「分级表 + 沙盒 + 审批口径 + 审计」四件事合成一次判定。

契约（规格书 A.5 PermissionGate.check）：
  - 入：skill_meta（含 name/permission）、args、ctx（可选：session_id/trace_id）
  - 出：{decision: allow|ask|deny, reason, policy, tier, second_confirm}
  - **纯函数、无副作用**（不弹窗）；弹窗由上层依 decision 发起
  - deny 必写审计（这里只写这一条审计，不影响纯净性）

判定顺序：
  1) 永久拒绝 → deny
  1.5) **派发者封顶**：issuer 的权限上限够不到本工具级别 → deny（"我是下位"就落在这里）
  2) 沙盒预检（路径/命令越界直接 deny，与级别无关）
  3) 永久允许 → allow
  4) 分级表 → 级别默认动作（read=allow / write=ask / danger=ask）
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from security.policy import DANGER, READ, WRITE, PermissionPolicy
from security.sandbox import Sandbox
from security.subjects import SubjectPolicy

_PATH_KEYS = ("path", "file", "filename", "dir", "directory")
_CMD_KEYS = ("cmd", "command", "argv")


class PermissionGate:
    def __init__(self, cfg: Dict[str, Any] = None, policy: Optional[PermissionPolicy] = None,
                 sandbox: Optional[Sandbox] = None, approvals=None, audit=None,
                 subjects: Optional[SubjectPolicy] = None,
                 logger=None, base_dir: Optional[str] = None):
        cfg = dict(cfg or {})
        self.policy = policy or PermissionPolicy(cfg.get("policy") or cfg)
        self.sandbox = sandbox if sandbox is not None else Sandbox(cfg.get("sandbox") or {}, base_dir=base_dir)
        self.approvals = approvals
        self.audit = audit
        self.subjects = subjects or SubjectPolicy(cfg.get("subjects") or {})
        self.logger = logger
        self.log: list = []          # 内存留痕（无 store 时可查）

    # ---------------- 主判定 ----------------
    def check(self, skill_meta: Dict[str, Any], args: Dict[str, Any],
              ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        name = (skill_meta or {}).get("name") or "?"
        args = args or {}
        ctx = dict(ctx or {})
        issuer = ctx.get("issuer")
        cls = self.policy.classify(name, skill_meta)
        tier = cls["tier"]

        # 1) 永久拒绝
        if self.approvals is not None and self.approvals.lookup(name) == "always_deny":
            return self._deny(name, "审批口径里永久拒绝", "approval_deny", tier, args, ctx)

        # 1.5) 派发者封顶：够不到就拒（不是"降级放行"，是明确拒绝）
        _eff, capped, why = self.subjects.clamp(tier, issuer)
        if capped:
            return self._deny(name, why, "subject_cap:%s" % (issuer or self.subjects.default_issuer),
                              tier, args, ctx)

        # 2) 沙盒预检（越界一律拒，跟级别无关）
        ok, why = self._sandbox_precheck(skill_meta, args, tier)
        if not ok:
            return self._deny(name, why, "sandbox", tier, args, ctx)

        # 3) 永久允许
        if self.approvals is not None and self.approvals.lookup(name) == "always_allow":
            return self._out(name, "allow", "审批口径里永久允许", "approval_allow", tier, args, ctx)

        # 4) 分级表
        action = {"allow": "allow", "ask": "ask", "deny": "deny"}.get(
            self.policy.action_for(tier), "ask")
        reason = {"allow": "只读操作，直接放行", "ask": "需确认",
                  "deny": "策略拒绝"}.get(action, "需确认")
        res = self._out(name, action, reason, "tier=%s" % tier, tier, args, ctx)
        if action == "deny":
            self._audit(name, args, "deny", False, reason, ctx)
        return res

    # ---------------- 内部 ----------------
    def _sandbox_precheck(self, meta: Dict[str, Any], args: Dict[str, Any], tier: str):
        if not self.sandbox.enabled:
            return True, "sandbox_off"
        mode = "write" if tier in (WRITE, DANGER) else "read"
        for k in _PATH_KEYS:
            v = args.get(k)
            if isinstance(v, (list, tuple)):
                for x in v:
                    ok, why = self.sandbox.check_path(str(x), mode)
                    if not ok:
                        return False, why
            elif isinstance(v, str) and v:
                ok, why = self.sandbox.check_path(v, mode)
                if not ok:
                    return False, why
        for k in _CMD_KEYS:
            if args.get(k):
                ok, why = self.sandbox.check_command(args[k])
                if not ok:
                    return False, why
        return True, "ok"

    def _out(self, name, decision, reason, policy, tier, args, ctx):
        ctx = dict(ctx or {})
        rec = {"decision": decision, "reason": reason, "policy": policy, "tier": tier,
               "issuer": ctx.get("issuer") or self.subjects.default_issuer,
               "second_confirm": self.policy.second_confirm_for(tier) if decision == "ask" else False}
        self.log.append({"skill": name, "args": _safe(args), **rec})
        return rec

    def _deny(self, name, reason, policy, tier, args, ctx):
        rec = self._out(name, "deny", reason, policy, tier, args, ctx)
        self._audit(name, args, "deny", False, reason, ctx)
        return rec

    def _audit(self, name, args, decision, approved, reason, ctx):
        if self.audit is None:
            return
        ctx = dict(ctx or {})
        self.audit.write("gate", "permission", decision, name,
                         {"args": _safe(args), "reason": reason,
                          "session_id": ctx.get("session_id"),
                          "issuer": ctx.get("issuer") or self.subjects.default_issuer},
                         approved)

    def record_decision(self, name: str, args: Any, decision: str, approved: Any,
                        latency_ms: Any = None, result_ok: Any = None, reason: str = "",
                        issuer: Any = None) -> None:
        """执行器干完活后回写一条工具审计（谁/何时/参数/结果/是否获批）。"""
        if self.audit is None:
            return
        self.audit.tool_call(name, _safe(args), decision, approved, latency_ms, result_ok,
                             "%s | issuer=%s" % (reason or "", issuer or self.subjects.default_issuer))


def _safe(args: Any) -> Any:
    s = str(args)
    return s[:200]
