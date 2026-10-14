# -*- coding: utf-8 -*-
"""文本清洗：把"无法编码的脏字符"挡在系统外。

背景（2026-09-28 踩到）：Windows 终端 / 输入法 / 剪贴板有时会送进来**半个 emoji**
（孤立的 UTF-16 代理项，如 `\\ud83d`）。后果是连环的：
  - sqlite 写不进（`surrogates not allowed`）→ 消息落不了盘；
  - embedding/向量检索编码失败 → 退化；
  - trace 落盘失败 → 复盘没证据；
  - 最要命：发给模型的 JSON 被对方拒收 → HTTP 400，整轮直接死。

对策：所有"外来文本"在进入系统前**统一清洗一遍**（丢掉孤立代理项）。
清洗是无损的——正常字符（含完整 emoji）原样保留。
"""
from __future__ import annotations

from typing import Any


def sanitize(text: Any) -> Any:
    """去掉无法用 UTF-8 编码的字符（主要是孤立代理项）。非字符串原样返回。"""
    if not isinstance(text, str):
        return text
    if not text:
        return text
    # 快路径：能编码就说明干净
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", "ignore").decode("utf-8", "ignore")


def sanitize_deep(obj: Any) -> Any:
    """递归清洗 dict / list / tuple / str（用于整个消息或参数结构）。"""
    if isinstance(obj, str):
        return sanitize(obj)
    if isinstance(obj, dict):
        return {sanitize(k) if isinstance(k, str) else k: sanitize_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_deep(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(sanitize_deep(v) for v in obj)
    return obj


def sanitize_messages(messages: Any) -> Any:
    """清洗消息序列里每条的 content / tool_calls / name（其它字段不动）。"""
    if not isinstance(messages, list):
        return messages
    out = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        m2 = dict(m)
        if isinstance(m2.get("content"), str):
            m2["content"] = sanitize(m2["content"])
        if m2.get("tool_calls") is not None:
            m2["tool_calls"] = sanitize_deep(m2["tool_calls"])
        if isinstance(m2.get("name"), str):
            m2["name"] = sanitize(m2["name"])
        out.append(m2)
    return out


def safe_dumps(obj: Any, **kw: Any) -> str:
    """json.dumps 的保险版：结果保证可 UTF-8 编码（写日志/trace 用）。"""
    import json
    kw.setdefault("ensure_ascii", False)
    kw.setdefault("default", str)
    return sanitize(json.dumps(obj, **kw))
