# -*- coding: utf-8 -*-
"""任务集加载：ASSISTANTML / JSON → 统一的 task 结构（M4）。

一条任务 = 输入 + 期望（答案 / 期望的工具调用序列 / 约束）。
字段（至少）：id / input / expect / tags；其余可选：
    turns          多轮输入（跨会话记忆召回用；每轮一个 session，可 same_session 覆盖）
    input_prefix   前缀填充 {text, repeat}（超长输入用）
    input_pad      后缀填充 {text, repeat}
    constraints    {max_steps, max_total_tokens, max_latency_ms, max_context_units, no_error}
    judge          可选模型判分的评分标准文字
    session_id     指定会话 id（默认由 runner 生成，任务间互相隔离）
"""
from __future__ import annotations

import glob
import os
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

TASK_GLOBS = ("*.yaml", "*.yml", "*.json")
REQUIRED_FIELDS = ("id", "input", "expect", "tags")


def load_tasks(path: str) -> List[Dict[str, Any]]:
    """加载一个任务目录（扫 *.yaml/*.json）或单个任务文件。"""
    p = os.path.abspath(os.path.expanduser(str(path)))
    files: List[str] = []
    if os.path.isdir(p):
        for pat in TASK_GLOBS:
            files.extend(sorted(glob.glob(os.path.join(p, pat))))
    elif os.path.isfile(p):
        files = [p]
    else:
        raise FileNotFoundError("任务集路径不存在：%s" % p)
    if not files:
        raise ValueError("任务目录里没有 yaml/json 任务文件：%s" % p)

    tasks: List[Dict[str, Any]] = []
    seen: Dict[str, str] = {}
    for f in files:
        for raw in _read_file(f):
            t = normalize_task(raw, source=f)
            if t["id"] in seen:
                raise ValueError("任务 id 重复：%s（%s 与 %s）" % (t["id"], seen[t["id"]], f))
            seen[t["id"]] = f
            tasks.append(t)
    if not tasks:
        raise ValueError("任务集为空：%s" % p)
    return tasks


def _read_file(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    if yaml is not None:
        data = yaml.safe_load(text)
    else:  # pragma: no cover
        import json
        data = json.loads(text)
    if data is None:
        return []
    if isinstance(data, dict):
        data = data.get("tasks") or []
    if not isinstance(data, list):
        raise ValueError("任务文件顶层必须是列表或 {tasks: [...]}：%s" % path)
    return data


def normalize_task(raw: Dict[str, Any], source: Optional[str] = None) -> Dict[str, Any]:
    """把原始 dict 归一成 runner 用的 task。缺字段直接报错（早点炸，别带病跑）。"""
    if not isinstance(raw, dict):
        raise ValueError("任务必须是映射（收到 %s）：%s" % (type(raw).__name__, source))
    missing = [k for k in REQUIRED_FIELDS if raw.get(k) in (None, "", [], {})]
    if missing:
        raise ValueError("任务缺必填字段 %s：%r（%s）" % (", ".join(missing), raw.get("id"), source))

    tid = str(raw["id"]).strip()
    prefix = raw.get("input_prefix")
    pad = raw.get("input_pad")

    turns_raw = raw.get("turns") or []
    if turns_raw:
        turns = [_decorate(str(x), prefix, pad) for x in turns_raw]
    else:
        turns = [_decorate(str(raw["input"]), prefix, pad)]

    expect = raw.get("expect") or {}
    if not isinstance(expect, dict):
        raise ValueError("expect 必须是映射：%s" % tid)
    constraints = raw.get("constraints") or {}
    if not isinstance(constraints, dict):
        raise ValueError("constraints 必须是映射：%s" % tid)

    return {
        "id": tid,
        "input": turns[0],
        "turns": turns,
        "expect": expect,
        "constraints": constraints,
        "tags": [str(x) for x in (raw.get("tags") or [])],
        "judge": raw.get("judge"),
        "same_session": bool(raw.get("same_session", False)),
        "session_id": raw.get("session_id"),
        "source": source,
    }


def _decorate(text: str, prefix: Any, pad: Any) -> str:
    return _block(prefix) + text + _block(pad)


def _block(spec: Any) -> str:
    """把 {text, repeat} 或字符串展开成填充块；没给就空串。"""
    if not spec:
        return ""
    if isinstance(spec, str):
        return spec
    if isinstance(spec, dict):
        filler = str(spec.get("text") or "")
        try:
            repeat = int(spec.get("repeat") or 0)
        except (TypeError, ValueError):
            repeat = 0
        return filler * max(0, repeat)
    return ""


def tags_of(task: Dict[str, Any]) -> List[str]:
    return list(task.get("tags") or [])
