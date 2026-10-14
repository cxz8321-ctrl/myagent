# -*- coding: utf-8 -*-
"""截断与分块：超长的东西先削再进上下文，别让小内容也绕路。"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple


def truncate_text(text: Any, max_chars: int, head_ratio: float = 0.6) -> Tuple[str, bool]:
    """掐头留尾：头部 60% + 尾部 40%，中间标出砍了多少。返回 (文本, 是否截断)。"""
    s = "" if text is None else str(text)
    if max_chars <= 0 or len(s) <= max_chars:
        return s, False
    head = max(1, int(max_chars * head_ratio))
    tail = max(1, max_chars - head)
    cut = len(s) - head - tail
    return s[:head] + "\n…[中略 %d 字]…\n" % cut + s[-tail:], True


def group_units(messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """把消息切成"不可拆单元"。

    一个单元 = 一条普通消息，或 assistant(tool_calls) + 紧随其后的 tool 结果们。
    拆开会让 tool 消息失去对应的调用，模型直接报错。
    """
    units: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "tool" and cur and _has_tool_calls(cur[0]):
            cur.append(m)
            continue
        if cur:
            units.append(cur)
        cur = [m]
    if cur:
        units.append(cur)
    return units


def _has_tool_calls(m: Dict[str, Any]) -> bool:
    return bool(m.get("role") == "assistant" and m.get("tool_calls"))


def unit_family(unit: List[Dict[str, Any]]) -> str:
    """这个单元算哪一家：工具结果 / 对话历史。"""
    if any(m.get("role") == "tool" for m in unit) or _has_tool_calls(unit[0]):
        return "tools"
    return "history"
