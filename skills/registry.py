# -*- coding: utf-8 -*-
"""技能注册表：注册 → 转 Schema → 执行（唯一执行路径）。

M1：代码内注册（内置技能）。
M3：加 目录扫描（SKILL.md）/ 原子热加载 / MCP 工具；执行走「校验 → 鉴权 → 执行 → 截断 → 结构化」。

来源（source）约定：
    builtin           内核自带
    file:<目录>        SKILL.md 技能包
    mcp:<server>       MCP server 暴露的工具
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional

MAX_RESULT_CHARS = 8000


class SkillMeta:
    """一个技能的全部元数据（规格书 A.2 SkillMeta）。"""

    def __init__(self, name: str, description: str = "", permission: str = "read",
                 parameters: Optional[Dict[str, Any]] = None,
                 handler: Optional[Callable[..., Any]] = None,
                 timeout_ms: int = 30000, is_async: bool = False,
                 triggers: Optional[List[str]] = None,
                 source: str = "builtin", path: Optional[str] = None,
                 entry: Optional[str] = None, references: Optional[List[str]] = None,
                 body: str = "", tags: Optional[List[str]] = None,
                 depends_on: Optional[List[str]] = None):
        self.name = name
        self.description = description
        self.permission = permission          # read / write / danger
        self.parameters = parameters or {"type": "object", "properties": {}, "required": []}
        self.handler = handler
        self.timeout_ms = timeout_ms
        self.is_async = is_async
        self.triggers = triggers or []
        self.source = source
        self.path = path
        self.entry = entry
        self.references = references or []
        self.body = body
        self.tags = tags or []
        self.depends_on = list(depends_on or [])   # M5：同轮里必须排在这些工具之后跑
        self.wants_ctx = bool(getattr(handler, "wants_ctx", False)) if handler else False

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description, "permission": self.permission,
                "parameters": self.parameters, "timeout_ms": self.timeout_ms,
                "is_async": self.is_async, "triggers": self.triggers, "source": self.source,
                "path": self.path, "entry": self.entry, "references": self.references,
                "tags": self.tags, "depends_on": self.depends_on}

    def to_schema(self) -> Dict[str, Any]:
        """模型能懂的 JSON Schema（OpenAI tools 格式）。"""
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


class ToolRegistry:
    def __init__(self, gate=None, audit=None, logger=None):
        self._skills: Dict[str, SkillMeta] = {}
        self.gate = gate          # 可选：带 gate 时 run() 会做鉴权
        self.audit = audit
        self.logger = logger

    # ---- 注册 ----
    def register(self, meta: SkillMeta) -> SkillMeta:
        self._skills[meta.name] = meta
        return meta

    def register_many(self, metas: List[SkillMeta]) -> int:
        for m in metas:
            self.register(m)
        return len(metas)

    def replace_source(self, prefix: str, metas: List[SkillMeta]) -> Dict[str, Any]:
        """**原子替换**某来源的全部技能：先构造完整的新表，成功才切换（不中途半残）。"""
        kept = {k: v for k, v in self._skills.items() if not str(v.source).startswith(prefix)}
        new = {m.name for m in metas}
        for m in metas:
            kept[m.name] = m
        old = {k for k, v in self._skills.items() if str(v.source).startswith(prefix)}
        self._skills = kept
        return {"added": sorted(new - old), "removed": sorted(old - new), "total": len(kept)}

    def remove_source(self, prefix: str) -> List[str]:
        gone = [k for k, v in self._skills.items() if str(v.source).startswith(prefix)]
        for k in gone:
            self._skills.pop(k, None)
        return sorted(gone)

    # ---- 查询 ----
    def get(self, name: str) -> Optional[Dict[str, Any]]:
        m = self._skills.get(name)
        return m.to_dict() if m else None

    def meta(self, name: str) -> Optional[SkillMeta]:
        return self._skills.get(name)

    def list(self, flt: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        flt = flt or {}
        out = []
        for m in self._skills.values():
            if flt.get("permission") and m.permission != flt["permission"]:
                continue
            if flt.get("source") and not str(m.source).startswith(flt["source"]):
                continue
            if flt.get("tag") and flt["tag"] not in m.tags:
                continue
            import re
            if flt.get("name_pattern") and not re.search(flt["name_pattern"], m.name):
                continue
            out.append(m.to_dict())
        return sorted(out, key=lambda d: d["name"])

    def names(self) -> List[str]:
        return sorted(self._skills)

    def to_schema(self) -> List[Dict[str, Any]]:
        return [m.to_schema() for m in sorted(self._skills.values(), key=lambda x: x.name)]

    # ---- 执行（唯一路径；出错也返回结构化结果，不抛） ----
    def run(self, name: str, args: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        ctx = dict(ctx or {})
        m = self._skills.get(name)
        if m is None:
            return _err("TOOL_NOT_FOUND", "技能不存在：%s" % name)
        if m.handler is None:
            return _err("TOOL_FAILED", "技能没有实现：%s" % name)

        # 1) 校验
        missing = [k for k in (m.parameters.get("required") or []) if k not in (args or {})]
        if missing:
            return _err("TOOL_BAD_ARGS", "缺参数：%s" % ", ".join(missing))

        # 2) 鉴权（唯一入口也被闸围住；不绕过）
        gate = ctx.get("gate") or self.gate
        gate_ctx = {"session_id": ctx.get("session_id"), "trace_id": ctx.get("trace_id"),
                    "issuer": ctx.get("issuer")}
        decision = {"decision": "allow", "reason": "no_gate"}
        if gate is not None:
            decision = gate.check(m.to_dict(), args or {}, gate_ctx)
            if decision.get("decision") == "deny":
                return _err("TOOL_DENIED", "权限被拒：%s" % decision.get("reason"),
                            extra={"decision": "deny", "tier": decision.get("tier")})
            if decision.get("decision") == "ask":
                if not ctx.get("approval"):
                    return _err("TOOL_DENIED", "需要确认但当前没有审批通道",
                                extra={"decision": "ask", "tier": decision.get("tier")})
                approved = bool(ctx["approval"]({"name": name, "args": args or {},
                                                 "reason": decision.get("reason"),
                                                 "tier": decision.get("tier"),
                                                 "second_confirm": decision.get("second_confirm")}))
                if not approved:
                    if gate is not None and hasattr(gate, "record_decision"):
                        gate.record_decision(name, args, "ask", False, None, False, "未获批准")
                    return _err("TOOL_DENIED", "未获批准，已跳过（用户拒绝，或当前是非交互模式、没有审批通道）",
                                approved=False, extra={"decision": "ask", "tier": decision.get("tier")})
                decision = dict(decision, decision="allowed_by_user")

        # 3) 执行
        t0 = time.time()
        ok = True
        try:
            out = (m.handler(args or {}, ctx) if m.wants_ctx else m.handler(args or {}))
            if isinstance(out, dict) and "content" in out and "ok" in out:
                ok = bool(out.get("ok"))
                content = out.get("content")
                content = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                extra_meta = out.get("meta") or {}
            else:
                content = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                extra_meta = {}
        except TimeoutError as e:
            ok, content, extra_meta = False, "技能超时：%s" % e, {"error": "TOOL_TIMEOUT"}
        except Exception as e:  # 工具报错也回填给模型，让它自己修
            ok, content, extra_meta = False, "执行失败：%s: %s" % (e.__class__.__name__, e), {}

        # 4) 截断
        truncated = len(content) > MAX_RESULT_CHARS
        content = content[:MAX_RESULT_CHARS]
        latency = int((time.time() - t0) * 1000)
        meta = {"latency_ms": latency, "truncated": truncated, "source": m.source,
                "decision": decision.get("decision", "allow"), "tier": decision.get("tier")}
        if not ok:
            meta.setdefault("error", extra_meta.get("error") or "TOOL_FAILED")
        meta.update({k: v for k, v in extra_meta.items() if k not in ("error",)})
        result = {"ok": ok, "content": content, "meta": meta}

        # 5) 审计回写（谁在何时调了什么工具、参数、结果、是否获批）
        if gate is not None and hasattr(gate, "record_decision"):
            gate.record_decision(name, args, meta["decision"], decision.get("decision") != "deny",
                                 latency, ok, decision.get("reason", ""), issuer=ctx.get("issuer"))
        return result


def _err(code: str, msg: str, approved: Any = None, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    meta = {"error": code, "approved": approved}
    if extra:
        meta.update(extra)
    return {"ok": False, "content": msg, "meta": meta}
