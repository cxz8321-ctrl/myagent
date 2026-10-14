# -*- coding: utf-8 -*-
"""装配：按配置实例化 provider / 安全层 / 技能层（含 MCP）/ 记忆层 / 循环。

换实现只改这一处（规格书 3.2：装配集中）。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from agent.events import EventBus
from agent.loop import AgentLoop
from context.budget import Budget
from context.builder import ContextBuilder
from context.prompts import PromptStore
from mcpclient import MCPManager
from memory import (Embedder, MemoryStore, MemoryWriter, ProceduralStore, Retriever,
                    VectorIndex)
from providers.registry import build_all
from runtime.config import load_config, project_root
from runtime.log import get_logger
from runtime.trace import TraceStore
from security import (ApprovalStore, AuditLog, PermissionGate, PermissionPolicy, Sandbox,
                       SubjectPolicy)
from skills import builtin as builtin_tools
from skills.loader import SkillLoader
from skills.registry import ToolRegistry


def _path(p: Optional[str], base: Optional[str] = None) -> Optional[str]:
    if not p:
        return None
    p = os.path.expanduser(str(p))
    if not os.path.isabs(p):
        p = os.path.join(base or project_root(), p)
    return p


def build_agent(cfg: Optional[Dict[str, Any]] = None, provider_name: Optional[str] = None,
                logger=None, bus: Optional[EventBus] = None, ask_callback=None,
                max_steps: int = 8, session_id: Optional[str] = None) -> Dict[str, Any]:
    cfg = cfg or load_config()
    logger = logger or get_logger("myagent")
    providers = build_all(cfg)
    name = provider_name or cfg.get("default_provider")
    if name not in providers:
        raise KeyError("配置里没有 provider：%s（有：%s）" % (name, ", ".join(providers)))

    # ---------- 存储（先建，审计与审批都要用） ----------
    st_cfg = cfg.get("storage") or {}
    data_dir = _path(st_cfg.get("data_dir") or (cfg.get("paths") or {}).get("data_dir") or "data")
    store = MemoryStore(data_dir=data_dir, db_file=st_cfg.get("db_file", "myagent.db"),
                        inline_limit=int(st_cfg.get("inline_limit", 8192)), logger=logger)

    # ---------- 安全层：分级表 + 沙盒 + 审批 + 审计 + 权限闸 ----------
    sec_cfg = dict(cfg.get("security") or {})
    policy = PermissionPolicy(sec_cfg.get("policy") or {})
    sandbox = Sandbox(sec_cfg.get("sandbox") or {}, base_dir=project_root())
    ap_cfg = dict(sec_cfg.get("approvals") or {})
    approvals = ApprovalStore(path=_path(ap_cfg.get("path") or "data/approvals.json", data_dir),
                             cfg=ap_cfg, logger=logger)
    audit = AuditLog(store=store, logger=logger)
    subjects = SubjectPolicy(sec_cfg.get("subjects") or {})
    gate = PermissionGate(sec_cfg, policy=policy, sandbox=sandbox, approvals=approvals,
                          audit=audit, subjects=subjects, logger=logger, base_dir=project_root())

    # ---------- 技能层：内置 + SKILL.md 技能包 + MCP ----------
    tools = ToolRegistry(gate=gate, audit=audit, logger=logger)
    tools.register_many(builtin_tools.BUILTIN)
    skill_dirs = [d for d in (
        [_path(d) for d in (cfg.get("paths") or {}).get("skill_dirs", [])]
        + [_path((cfg.get("paths") or {}).get("skill_dir"))]
        + [_path("~/.myagent/skills")]
    ) if d]
    loader = SkillLoader(skill_dirs, logger=logger)
    skills_report = loader.load()
    tools.replace_source("file:", skills_report["skills"])
    mcp = MCPManager(cfg.get("mcp") or {}, registry=tools, logger=logger)
    mcp_report = mcp.start_all()

    # ---------- 上下文工程 ----------
    ctx_cfg = cfg.get("context") or {}
    budget = Budget(total_tokens=int(ctx_cfg.get("total_tokens", 32768)),
                    reserve_output=int(ctx_cfg.get("reserve_output", 2048)),
                    weights=ctx_cfg.get("weights"))
    prompts = PromptStore(_path(ctx_cfg.get("prompt_dir") or "config/prompts"))
    context = ContextBuilder(budget=budget, prompts=prompts,
                             prompt_name=ctx_cfg.get("prompt_name", "system"),
                             keep_last=int(ctx_cfg.get("keep_last", 6)),
                             max_tool_result_chars=int(ctx_cfg.get("max_tool_result_chars", 6000)),
                             logger=logger)

    # ---------- 记忆层 L2/L4 ----------
    mem_cfg = cfg.get("memory") or {}
    embedder = index = retriever = writer = procedural = None
    if mem_cfg.get("enabled", True):
        e_cfg = mem_cfg.get("embed") or {}
        if e_cfg.get("enabled", True):
            embedder = Embedder(
                model_path=e_cfg.get("model_path", "~/.local/models/Qwen3-Embedding-0.6B-Q8_0.gguf"),
                model_name=e_cfg.get("model_name"), n_ctx=int(e_cfg.get("n_ctx", 512)),
                n_threads=int(e_cfg.get("n_threads", 0)),
                query_prefix=e_cfg.get("query_prefix", ""), logger=logger)
        if embedder is not None:
            index = VectorIndex(store, embedder, logger=logger)
        r_cfg = mem_cfg.get("retrieve") or {}
        retriever = Retriever(store, embedder=embedder, index=index,
                              weights=r_cfg.get("weights"),
                              min_score=float(r_cfg.get("min_score", 0.15)), logger=logger)
        store.retriever = retriever
        w_cfg = mem_cfg.get("write") or {}
        writer = MemoryWriter(store, index=index, embedder=embedder, retriever=retriever,
                              logger=logger, max_len=int(w_cfg.get("max_len", 200)),
                              model_judge=bool(w_cfg.get("model_judge", True)),
                              judge_provider=providers[name],
                              user_id=mem_cfg.get("user_id", "default"),
                              scope=mem_cfg.get("scope", "global"))
        p_cfg = mem_cfg.get("procedural") or {}
        if p_cfg.get("enabled", True):
            procedural = ProceduralStore(_path(p_cfg.get("dir", "memory/procedural")) or "memory/procedural",
                                         store=store, logger=logger)
        if (mem_cfg.get("summarize") or {}).get("enabled", True):
            store.summarizer = _make_summarizer(providers[name], logger)

    # ---------- 循环 ----------
    rt_cfg = cfg.get("runtime") or {}
    tools_rt = rt_cfg.get("tools") or {}
    trace_cfg = rt_cfg.get("trace") or {}
    sm_cfg = rt_cfg.get("state_machine") or {}
    trace_dir = _path(trace_cfg.get("dir") or "data/traces")
    loop = AgentLoop(
        provider=providers[name],
        tools=tools,
        gate=gate,
        bus=bus,
        context=context,
        prompt_vars=cfg.get("prompt") or {},
        context_budget=budget.total,
        reserve_output=budget.reserve_output,
        max_steps=max_steps,
        store=store,
        session_id=session_id,
        history_load=int(st_cfg.get("history_load", 12)),
        retriever=retriever,
        writer=writer,
        memory_cfg=mem_cfg,
        procedural=procedural,
        user_id=mem_cfg.get("user_id", "default"),
        scope=mem_cfg.get("scope", "global"),
        logger=logger,
        ask_callback=ask_callback,
        approvals=approvals,
        pricing=cfg.get("pricing") or {},
        trace_enabled=bool(trace_cfg.get("enabled", True)),
        trace_dir=trace_dir,
        parallel_tools=bool(tools_rt.get("parallel", True)),
        max_workers=int(tools_rt.get("max_workers", 4)),
        state_strict=bool(sm_cfg.get("strict", False)),
    )
    return {"cfg": cfg, "providers": providers, "provider_name": name, "context": context,
            "budget": budget, "prompts": prompts, "tools": tools, "gate": gate,
            "policy": policy, "sandbox": sandbox, "approvals": approvals, "audit": audit,
            "subjects": subjects,
            "loader": loader, "skills_report": skills_report, "mcp": mcp, "mcp_report": mcp_report,
            "store": store, "embedder": embedder, "index": index, "retriever": retriever,
            "writer": writer, "procedural": procedural, "memory_cfg": mem_cfg,
            "loop": loop, "bus": loop.bus, "logger": logger,
            "cost": loop.cost_calc, "trace_store": TraceStore(trace_dir)}


def reload_skills(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """热重载：SKILL.md 技能包原子替换 + MCP 工具重连。返回变更摘要。"""
    out: Dict[str, Any] = {}
    loader: SkillLoader = ctx.get("loader")
    if loader is not None:
        rep = loader.load()
        out["skills"] = ctx["tools"].replace_source("file:", rep["skills"])
        out["errors"] = rep["errors"]
    mcp = ctx.get("mcp")
    if mcp is not None and mcp.enabled:
        out["mcp"] = mcp.reload()
    return out


def _make_summarizer(provider, logger=None):
    """把一段对话压成摘要（保留偏好/口径/决定/待办/关键事实）。"""
    SYSTEM = "你是一个对话摘要器。只输出摘要正文，不要客套、不要解释。"

    def summarizer(prev: str, new_text: str) -> str:
        prompt = ("把下面的对话压缩成一段简明摘要。必须保留：用户的偏好与口径、已定的决定、"
                  "未完成的待办、关键事实。丢掉寒暄与过程细节。\n\n")
        if prev:
            prompt += "已有摘要：\n%s\n\n" % prev[:2000]
        prompt += "新增对话：\n%s" % str(new_text)[:8000]
        try:
            r = provider.chat([{"role": "system", "content": SYSTEM},
                               {"role": "user", "content": prompt}],
                              cfg={"temperature": 0, "max_tokens": 500})
            if r.get("ok"):
                return (r["data"]["message"].get("content") or "").strip()
        except Exception as e:
            if logger:
                try:
                    logger.warning("summarize_failed %s" % e)
                except Exception:
                    pass
        return ""
    return summarizer
