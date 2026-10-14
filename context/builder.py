# -*- coding: utf-8 -*-
"""组装器：按优先级拼装消息，超出配额时低优先级先出局。

铁律：系统提示与最近 N 轮永不被裁；注入的记忆必须记录 id 以便回查。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from context.budget import Budget
from context.prompts import PromptStore
from context.truncate import group_units, truncate_text, unit_family
from providers.base import estimate_tokens
from runtime.text import sanitize_messages


class ContextBuilder:
    def __init__(self, budget: Optional[Budget] = None, prompts: Optional[PromptStore] = None,
                 prompt_name: str = "system", keep_last: int = 6,
                 max_tool_result_chars: int = 6000, counter: Optional[Callable[[Any], int]] = None,
                 logger=None):
        self.budget = budget or Budget()
        self.prompts = prompts or PromptStore()
        self.prompt_name = prompt_name
        self.keep_last = int(keep_last)
        self.max_tool_result_chars = int(max_tool_result_chars)
        self.counter = counter or estimate_tokens
        self.logger = logger

    # ---------- 主入口 ----------
    def build(self, state, memory_block: Optional[str] = None,
              memory_ids: Optional[List[str]] = None,
              summary_block: str = "", procedural_block: str = "",
              extra_vars: Optional[Dict[str, Any]] = None,
              system_override: Optional[str] = None) -> Dict[str, Any]:
        """state.messages 只放对话（不含 system）；system 与 memory 由这里注入。"""
        dropped: Dict[str, int] = {"system": 0, "memory": 0, "tools": 0, "history": 0}
        used: Dict[str, int] = {"system": 0, "memory": 0, "tools": 0, "history": 0}

        # 没显式给就从状态对象上取（少一层手工传参的坑）
        if not memory_block:
            memory_block = getattr(state, "memory_block", "") or None
        if not memory_ids:
            memory_ids = getattr(state, "memory_ids", None) or []
        if not summary_block:
            summary_block = getattr(state, "summary_block", "") or ""
        if not procedural_block:
            procedural_block = getattr(state, "procedural_block", "") or ""

        # 1) 系统提示（按配额截断，但绝不整块丢）
        if system_override is not None:
            rendered = {"text": system_override, "version": "override", "path": None, "missing": []}
        else:
            rendered = self.prompts.render(self.prompt_name, variables=extra_vars)
        sys_budget = self.budget.quota("system")
        sys_text = rendered["text"]
        max_chars = max(500, int(sys_budget * 2.2))
        sys_text, cut = truncate_text(sys_text, max_chars)
        if cut:
            dropped["system"] = 1
        system_msg = {"role": "system", "content": sys_text}
        used["system"] = self.counter(sys_text)

        # 2) 记忆块（M2 起真注入；没有就跳过）
        msgs: List[Dict[str, Any]] = [system_msg]
        if memory_block:
            m_budget = self.budget.quota("memory")
            mem_text, cut = truncate_text(memory_block, max(300, int(m_budget * 2.2)))
            if cut:
                dropped["memory"] = 1
            msgs.append({"role": "system", "content": "以下是与你相关的记忆片段（按需参考，不必复述）：\n" + mem_text})
            used["memory"] = self.counter(mem_text)

        # 2.5) 更早对话的摘要（原文不再进上下文）
        if summary_block:
            sm_text, _ = truncate_text(summary_block, max(300, int(self.budget.quota("memory") * 2.2)))
            msgs.append({"role": "system", "content": "【更早对话的摘要】\n" + sm_text})
            used["memory"] += self.counter(sm_text)

        # 2.6) L4 程序性记忆：经验文件的标题+摘要（正文按需自取）
        if procedural_block:
            pr_text, _ = truncate_text(procedural_block, max(200, int(self.budget.quota("system") * 2.2)))
            msgs.append({"role": "system",
                         "content": "【你的经验文件（需要细节就用 read_file 自己读）】\n" + pr_text})
            used["system"] += self.counter(pr_text)

        # 3) 历史单元：从最新往旧装，超配额就丢；最近 keep_last 个单元强制保留
        units = group_units(state.messages)
        kept: List[List[Dict[str, Any]]] = []
        for idx, unit in enumerate(reversed(units)):
            family = unit_family(unit)
            cost = self.counter(_flatten(unit))
            must = idx < self.keep_last
            over_system_hard = False  # 系统已单独算
            if family == "tools" and not must:
                unit = _shrink_tool_unit(unit, self.max_tool_result_chars)
                cost = self.counter(_flatten(unit))
            if must or used[family] + cost <= self.budget.quota(family) or over_system_hard:
                kept.append(unit)
                used[family] += cost
            else:
                dropped[family] += 1
        kept.reverse()

        # 4) 工具结果再兜一层：单条超长仍要削
        out_msgs: List[Dict[str, Any]] = list(msgs)
        for unit in kept:
            for m in unit:
                if m.get("role") == "tool":
                    content, cut = truncate_text(m.get("content"), self.max_tool_result_chars)
                    if cut:
                        m = dict(m, content=content)
                        dropped["tools"] += 1
                out_msgs.append(m)

        stats = {
            "used_tokens": sum(used.values()),
            "by_family": used,
            "dropped": dropped,
            "units_kept": len(kept),
            "units_total": len(units),
            "injected_memory_ids": list(memory_ids or []),
            "prompt_version": rendered["version"],
            "prompt_path": rendered["path"],
            "prompt_missing_vars": rendered["missing"],
            "budget": self.budget.to_dict(),
        }
        return {"messages": sanitize_messages(_drop_orphan_tools(out_msgs)), "stats": stats}


def _drop_orphan_tools(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """丢掉"孤儿 tool 消息"（没有前置 assistant.tool_calls 认领它）。

    为什么必须丢：历史是从 L1 截最近 N 条捞的，截断点可能落在某个工具往返中间，
    于是开头留下一条 tool 消息。厂商 API 会直接 400：
      "Messages with role 'tool' must be a response to a preceding message with 'tool_calls'"
    （2026-09-28 实际踩到）
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for m in msgs or []:
        role = m.get("role")
        if role == "assistant":
            for tc in (m.get("tool_calls") or []):
                if isinstance(tc, dict) and tc.get("id"):
                    seen.add(tc["id"])
            out.append(m)
        elif role == "tool":
            cid = m.get("tool_call_id")
            if cid and cid in seen:
                out.append(m)
            # 孤儿：丢
        else:
            out.append(m)
    return out


def _flatten(unit: List[Dict[str, Any]]) -> str:
    return "\n".join(str(m.get("content") or "") for m in unit)


def _shrink_tool_unit(unit: List[Dict[str, Any]], max_chars: int) -> List[Dict[str, Any]]:
    out = []
    for m in unit:
        if m.get("role") == "tool" and isinstance(m.get("content"), str) and len(m["content"]) > max_chars:
            out.append(dict(m, content=truncate_text(m["content"], max_chars)[0]))
        else:
            out.append(m)
    return out
