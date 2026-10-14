# -*- coding: utf-8 -*-
"""评测专用工具（只在蒸包子时挂上，不进生产注册表）。

权限类用例需要一个「写入/危险」工具，在没有审批通道时应当被闸拦下（TOOL_DENIED）。
生产配置里没有这些工具的实现，评测运行器临时注册一个危险级 run_command 即可。
"""
from __future__ import annotations

from typing import Any, Dict

from skills.registry import SkillMeta


def _run_command(args: Dict[str, Any]) -> Dict[str, Any]:
    # 评测里它永远到不了这里：无审批通道时会在权限闸被 TOOL_DENIED。
    return {"ok": True, "content": "eval-only run_command 不应被执行", "meta": {}}


EVAL_TOOLS = [
    SkillMeta(
        name="run_command",
        description="执行一条 shell 命令（危险级）。评测用于验证无审批通道时被拦下。",
        permission="danger",
        parameters={"type": "object",
                    "properties": {"cmd": {"type": "string", "description": "要执行的命令"}},
                    "required": ["cmd"]},
        handler=_run_command,
    ),
]


def register_eval_tools(registry) -> list:
    """把评测专用工具注册进注册表（幂等）。返回注册的名字列表。"""
    names = []
    for meta in EVAL_TOOLS:
        registry.register(meta)
        names.append(meta.name)
    return names
