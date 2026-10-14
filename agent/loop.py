# -*- coding: utf-8 -*-
"""核心循环：想 → 做 → 看 → 再想。

终止条件四选一（规格书 3.1）：模型给最终答案 / 达到最大步数 / 预算耗尽 / 用户中断。
每步发事件；任何分支都有终态与原因。

M5 增补（不破坏上面这些对外契约）：
  - 相位走显式状态机 `runtime.state_machine`（idle/thinking/acting/waiting_approval/done/error）
  - 同一轮里**无依赖的工具并行执行**（`concurrent.futures`），有依赖的严格串行
  - 每步 trace 落盘（耗时 / token / 费用），费用按 config 里的 provider 单价算
  - cancel token 语义明确：命中即优雅收尾，不崩、可续
  - 出错统一归类（可重试 / 不可重试 / 需人工），出口只报信封不抛异常
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional

from agent.events import EventBus
from agent.state import AgentState
from context.builder import ContextBuilder
from providers.base import envelope
from runtime.text import sanitize, sanitize_messages
from runtime.cost import CostCalculator
from runtime.errors import AgentError, classify, from_exception
from runtime.state_machine import RunState, StateMachine
from runtime.trace import TraceWriter


class Termination:
    DONE = "done"
    MAX_STEPS = "max_steps"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"
    ERROR = "error"
    LOOP_GUARD = "loop_guard"


# 终止原因 -> 终态相位
_TERMINAL_PHASE = {
    Termination.DONE: RunState.DONE,
    Termination.MAX_STEPS: RunState.DONE,
    Termination.BUDGET_EXHAUSTED: RunState.DONE,
    Termination.CANCELLED: RunState.DONE,
    Termination.LOOP_GUARD: RunState.ERROR,
    Termination.ERROR: RunState.ERROR,
}

DEFAULT_SYSTEM = (
    "你是骛（myagent），一个务实、不端不装的助理。"
    "需要动手时就用工具，用完根据结果继续，不要编造工具结果。"
    "回答用中文，简洁直接。"
)


class AgentLoop:
    def __init__(self, provider, tools=None, gate=None, bus: Optional[EventBus] = None,
                 context=None, prompt_vars: Optional[Dict[str, Any]] = None,
                 system_prompt: Optional[str] = None, context_budget: int = 32768,
                 reserve_output: int = 2048, max_steps: int = 8,
                 store=None, session_id: Optional[str] = None, history_load: int = 12,
                 retriever=None, writer=None, memory_cfg: Optional[Dict[str, Any]] = None,
                 procedural=None, user_id: str = "default", scope: str = "global",
                 logger=None, ask_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
                 approvals=None, pricing: Optional[Dict[str, Any]] = None,
                 trace_enabled: bool = False, trace_dir: Optional[str] = None,
                 parallel_tools: bool = True, max_workers: int = 4,
                 state_strict: bool = False, subjects=None):
        self.provider = provider
        self.tools = tools
        self.gate = gate
        self.bus = bus or EventBus()
        self.context = context or ContextBuilder()
        self.prompt_vars = dict(prompt_vars or {})
        self.system_prompt = system_prompt          # 给了就盖掉文件模板
        self.context_budget = int(context_budget)
        self.reserve_output = int(reserve_output)
        self.max_steps = int(max_steps)
        self.store = store                          # L1 会话落盘（可为 None）
        self.session_id = session_id
        self.history_load = int(history_load)
        self.retriever = retriever                  # L2 检索（可为 None）
        self.writer = writer                        # L2 写入（可为 None）
        mc = dict(memory_cfg or {})
        self.user_id = user_id
        self.scope = scope
        self.inject_enabled = bool((mc.get("inject") or {}).get("enabled", True))
        self.inject_max_chars = int((mc.get("inject") or {}).get("max_chars", 2000))
        self.top_k = int((mc.get("retrieve") or {}).get("top_k", 5))
        self.write_enabled = bool((mc.get("write") or {}).get("enabled", True))
        self.procedural = procedural                # L4 经验文件（可为 None）
        sc = mc.get("summarize") or {}
        self.summarize_enabled = bool(sc.get("enabled", True))
        self.summarize_trigger = float(sc.get("trigger_ratio", 0.7))
        self.summarize_keep_last = int(sc.get("keep_last", 8))
        self.procedural_max_chars = int((mc.get("procedural") or {}).get("inject_max_chars", 2000))
        self.logger = logger
        self.ask_callback = ask_callback
        self.approvals = approvals          # 审批口径（本次/永久/拒绝）
        self.subjects = subjects or getattr(gate, "subjects", None)   # 派发者权限上限
        self.default_issuer = getattr(self.subjects, "default_issuer", "owner")

        # ---------- M5 工程化 ----------
        self.cost_calc = CostCalculator(pricing or {})
        self.trace_enabled = bool(trace_enabled)
        self.trace_dir = trace_dir
        self.parallel_tools = bool(parallel_tools)
        self.max_workers = max(1, int(max_workers))
        self.state_machine = StateMachine(strict=state_strict, logger=logger)
        self.cost_totals: Dict[str, Dict[str, Any]] = {}   # 跨多次 run 的累计（CLI /cost）
        self._approval_lock = threading.Lock()             # 审批一次只放一个人进来
        self._trace: Optional[TraceWriter] = None          # 本次 run 的 trace writer

    # ---------- 单步（便于单测与回放） ----------
    def step(self, state: AgentState, trace: Optional[TraceWriter] = None) -> Dict[str, Any]:
        schema = self.tools.to_schema() if self.tools else None
        self._transition(state, RunState.THINKING, "provider_call")
        built = self._build_context(state)
        t0 = time.time()
        r = self.provider.chat(built["messages"], tools=schema)
        dur_ms = int((time.time() - t0) * 1000)
        if not r.get("ok"):
            err = self._classify_error(r.get("error"))
            state.error = err
            self._transition(state, RunState.ERROR, "provider_error:%s" % err.get("code"))
            if trace is not None:
                trace.step(step=state.step_count + 1, ok=False, duration_ms=dur_ms, error=err,
                           phase=state.phase)
            return {"state": state, "action": "abort", "error": err}
        data = r["data"]
        msg = data["message"]
        before = dict(state.usage_total)
        state.add_usage(data.get("usage"))
        state.messages.append(sanitize_messages([msg])[0])
        state.step_count += 1
        delta = _usage_delta(before, state.usage_total)
        if trace is not None:
            trace.step(step=state.step_count, ok=True, duration_ms=dur_ms,
                       usage=delta, usage_total=dict(state.usage_total),
                       cost=self.cost_calc.compute(self.provider.name, delta),
                       phase=state.phase, finish_reason=data.get("finish_reason"),
                       tool_calls=[tc.get("name") for tc in (msg.get("tool_calls") or [])],
                       content_chars=len(str(msg.get("content") or "")))
        if msg.get("content"):
            self.bus.emit("thought", {"text": msg["content"]}, trace_id=state.trace_id)
        if not msg.get("tool_calls"):
            self._transition(state, RunState.DONE, "final_answer")
            return {"state": state, "action": "final", "message": msg}
        self._transition(state, RunState.ACTING, "tool_calls")
        return {"state": state, "action": "tool_call", "message": msg}

    def _build_context(self, state: AgentState) -> Dict[str, Any]:
        import os
        import time as _time
        extra = dict(self.prompt_vars)
        extra.setdefault("now", _time.strftime("%Y-%m-%d %H:%M:%S %Z"))
        extra.setdefault("cwd", os.getcwd())
        extra.setdefault("model", self.provider.model or "")
        built = self.context.build(state, memory_block=state.memory_block,
                                   memory_ids=state.memory_ids,
                                   summary_block=state.summary_block,
                                   procedural_block=state.procedural_block,
                                   extra_vars=extra, system_override=self.system_prompt)
        state.context_stats = built["stats"]
        return built

    def _maybe_compact(self, session_id: str) -> Optional[Dict[str, Any]]:
        """历史占满窗口七成就压一次：保留最近 N 条原文，更早的压成摘要。"""
        if not (self.store and self.summarize_enabled):
            return None
        try:
            from providers.base import estimate_tokens
            info = self.store.session_summary(session_id)
            opts = {"after_id": info["upto_msg_id"]} if info.get("upto_msg_id") else None
            rows = self.store.recent(session_id, n=400, opts=opts)
            if len(rows) <= self.summarize_keep_last:
                return None
            est = estimate_tokens("\n".join(str(m.get("content") or "") for m in rows))
            if est <= self.summarize_trigger * self.context.budget.quota("history"):
                return None
            res = self.store.compact(session_id, {"keep_last": self.summarize_keep_last})
            if not res.get("skipped"):
                self.bus.emit("compact", res)
            return res
        except Exception as e:
            self._log("compact_failed", error=str(e))
            return None

    def _retrieve_memory(self, state: AgentState, task: Dict[str, Any]) -> None:
        """按本次输入检索 L2；检索坏了就当没有记忆，不影响主流程。"""
        if self.retriever is None or not self.inject_enabled:
            return
        query = str(task.get("input") or "")
        if not query.strip():
            return
        try:
            hits = self.retriever.search(query, k=self.top_k, user_id=self.user_id, scope=self.scope)
            block, ids = self.retriever.format_block(hits, max_chars=self.inject_max_chars)
            if block:
                state.memory_block, state.memory_ids = block, ids
                self.bus.emit("memory_hit", {"count": len(ids), "ids": ids,
                                             "scores": [h["score"] for h in hits]},
                              trace_id=state.trace_id)
        except Exception as e:
            self._log("memory_search_failed", error=str(e))

    # ---------- 完整一次任务 ----------
    def run(self, task: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        ctx = dict(ctx or {})
        # 入口清洗：Windows 终端/输入法可能带进孤立代理项（半个 emoji），
        # 不清干净会导致落盘失败 + 请求被模型 API 拒收（HTTP 400）。
        task = dict(task or {})
        if isinstance(task.get("input"), str):
            task["input"] = sanitize(task["input"])
        session_id = (task.get("session_id") or ctx.get("session_id") or self.session_id
                      or (self.store.new_session_id() if self.store else "cli"))
        trace_id = ctx.get("trace_id") or _trace()
        state = AgentState(
            session_id=session_id,
            trace_id=trace_id,
            budget_left=int(task.get("budget") or self.context_budget),
            issuer=str(task.get("issuer") or ctx.get("issuer") or self.default_issuer),
        )
        # 新的一跑：状态机归零；trace 开工（写失败不拖垮主流程）
        self.state_machine.reset(RunState.IDLE)
        trace = TraceWriter(trace_id, self.trace_dir, enabled=self.trace_enabled, logger=self.logger)
        self._trace = trace
        state.trace_path = trace.path

        # 取消令牌（ctx.cancel_token 兼容旧约定，也认关键字 cancel）
        cancel = ctx.get("cancel_token") or ctx.get("cancel")

        # L1：先把历史捞回来（重启后接着聊）；太长了先做滚动摘要
        hist: List[Dict[str, Any]] = []
        if self.store:
            self._maybe_compact(session_id)
            try:
                self.store.ensure_session(session_id, channel=task.get("channel", "cli"))
                info = self.store.session_summary(session_id)
                state.summary_block = info.get("summary") or ""
                opts = {"after_id": info["upto_msg_id"]} if info.get("upto_msg_id") else None
                hist = [_strip_meta(m) for m in self.store.recent(session_id, n=self.history_load, opts=opts)]
            except Exception as e:            # 记忆坏了不能拖垮内核
                self._log("memory_load_failed", error=str(e))
        # L4：开场先读经验文件（只给标题+摘要，正文按需自取）
        if self.procedural is not None:
            try:
                block, _paths = self.procedural.inject_block(max_chars=self.procedural_max_chars)
                state.procedural_block = block
            except Exception as e:
                self._log("procedural_load_failed", error=str(e))
        state.messages = hist + [{"role": "user", "content": task.get("input", "")}]
        saved = len(hist)
        self._persist(state, saved)
        saved = len(state.messages)

        # L2：先按这次输入捞相关记忆（命中即注入，并记下 id 以便回查）
        self._retrieve_memory(state, task)

        guard: List[str] = []
        error: Optional[Dict[str, Any]] = None
        started = time.time()
        trace.run_start(input=_snip(task.get("input"), 4000),
                        input_chars=len(str(task.get("input") or "")),
                        session_id=session_id, provider=self.provider.name,
                        model=self.provider.model, max_steps=self.max_steps,
                        context_budget=self.context_budget, issuer=state.issuer)

        try:
            while True:
                if cancel is not None and getattr(cancel, "cancelled", False):
                    self._terminate(state, Termination.CANCELLED, trace,
                                    reason=str(getattr(cancel, "reason", "") or "cancel"))
                    break
                if state.step_count >= self.max_steps:
                    self._terminate(state, Termination.MAX_STEPS, trace)
                    break
                if state.budget_left <= self.reserve_output:
                    self._terminate(state, Termination.BUDGET_EXHAUSTED, trace)
                    break

                out = self.step(state, trace=trace)
                self.bus.emit("step", {"step": state.step_count, "action": out["action"]},
                              trace_id=state.trace_id)
                if out["action"] == "abort":
                    error = out.get("error")
                    self._terminate(state, Termination.ERROR, trace, error=error)
                    self.bus.emit("error", {"error": error}, trace_id=state.trace_id)
                    self._persist(state, saved); saved = len(state.messages)
                    break
                if out["action"] == "final":
                    self._terminate(state, Termination.DONE, trace)
                    self._persist(state, saved); saved = len(state.messages)
                    break

                # 执行本轮全部工具：无依赖并行、有依赖串行，结果按序归并
                blocked = self._run_tools(out["message"], state, guard, cancel, trace=trace)
                self._persist(state, saved); saved = len(state.messages)
                if blocked:
                    error = state.error
                    self._terminate(state, Termination.LOOP_GUARD, trace, error=error)
                    self.bus.emit("error", {"error": error}, trace_id=state.trace_id)
                    break
                if cancel is not None and getattr(cancel, "cancelled", False):
                    self._terminate(state, Termination.CANCELLED, trace,
                                    reason=str(getattr(cancel, "reason", "") or "cancel"))
                    break
        except Exception as e:  # noqa: BLE001 —— 任何意外都归一成信封，绝不崩
            error = from_exception(e)
            state.error = error
            self._log("loop_failed", error=error.get("message"))
            self._terminate(state, Termination.ERROR, trace, error=error)
            self.bus.emit("error", {"error": error}, trace_id=state.trace_id)

        answer = None
        for m in reversed(state.messages):
            if m.get("role") == "assistant" and m.get("content") and not m.get("tool_calls"):
                answer = m["content"]
                break
        # L2：干完一件事，把值得记的沉淀下来（规则先筛，闲聊不写）
        if self.writer is not None and self.write_enabled:
            try:
                state.memory_written = self.writer.extract_and_write(
                    [m for m in state.messages if m.get("role") in ("user", "assistant")],
                    provider=self.provider, session_id=state.session_id, scope=self.scope)
                if state.memory_written:
                    self.bus.emit("memory_written", {"items": state.memory_written},
                                  trace_id=state.trace_id)
            except Exception as e:
                self._log("memory_write_failed", error=str(e))

        elapsed_ms = int((time.time() - started) * 1000)
        cost = self.cost_calc.compute(self.provider.name, state.usage_total)
        state.cost_total = cost
        self.cost_totals[self.provider.name] = CostCalculator.add(
            self.cost_totals.get(self.provider.name), cost)
        status = state.status or Termination.ERROR
        trace.final(status=status, termination=state.termination or status,
                    answer=_snip(answer, 2000), answer_chars=len(str(answer or "")),
                    steps=state.step_count, elapsed_ms=elapsed_ms, usage=dict(state.usage_total),
                    cost=cost, phase=state.phase,
                    cancel=(cancel.to_dict() if hasattr(cancel, "to_dict") else None),
                    error=state.error)
        self.bus.emit("final", {"status": status, "answer": answer, "phase": state.phase,
                                "cost": cost}, trace_id=state.trace_id)
        return envelope(status == Termination.DONE,
                        data={"status": status, "termination": state.termination or status,
                              "phase": state.phase, "answer": answer,
                              "steps": state.step_count, "usage": state.usage_total,
                              "cost": cost, "budget_left": state.budget_left,
                              "session_id": state.session_id, "issuer": state.issuer,
                              "context_stats": state.context_stats,
                              "memory_hits": len(state.memory_ids),
                              "memory_written": state.memory_written,
                              "trace_id": state.trace_id, "trace_path": trace.path},
                        error=error, meta={"trace_id": state.trace_id, "provider": self.provider.name,
                                           "elapsed_ms": elapsed_ms, "issuer": state.issuer},
                        keep_data=True)

    # ---------- 工具执行（M5：并行 + 归并） ----------
    def _run_tools(self, message: Dict[str, Any], state: AgentState, guard: List[str],
                   cancel: Any = None, trace: Optional[TraceWriter] = None) -> bool:
        """执行一条 assistant 消息里的全部工具调用。

        返回 True 表示触发了重复调用护栏（调用方按 loop_guard 终止）。
        事件顺序：先按原顺序发全部 tool_call，收集完再按原顺序发 tool_result。
        """
        tcs: List[Dict[str, Any]] = list(message.get("tool_calls") or [])
        # 1) 护栏 + 发 tool_call 事件（顺序确定，不受并发影响）
        for tc in tcs:
            sig = "%s:%s" % (tc.get("name"),
                             json.dumps(tc.get("arguments"), ensure_ascii=False, sort_keys=True))
            guard.append(sig)
            if len(guard) >= 3 and guard[-1] == guard[-2] == guard[-3]:
                state.error = classify("SYS_LOOP_GUARD", "同一工具同参数连续调用 3 次，已打断",
                                       detail=sig)
                return True
            self.bus.emit("tool_call", {"name": tc.get("name"), "arguments": tc.get("arguments")},
                          trace_id=state.trace_id)

        # 2) 按声明的依赖分轮：同轮无依赖 -> 并行；跨轮有依赖 -> 串行
        batches = self._plan_batches(tcs)
        results: List[Optional[Dict[str, Any]]] = [None] * len(tcs)
        for batch in batches:
            if cancel is not None and getattr(cancel, "cancelled", False):
                break
            if len(batch) == 1 or not self.parallel_tools:
                for i in batch:
                    results[i] = self._execute(tcs[i], state)
            else:
                workers = max(1, min(self.max_workers, len(batch)))
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tool") as ex:
                    futs = {ex.submit(self._execute, tcs[i], state): i for i in batch}
                    for fut in as_completed(futs):
                        i = futs[fut]
                        try:
                            results[i] = fut.result()
                        except Exception as e:  # noqa: BLE001 —— 单个工具线程挂了不拖垮整轮
                            results[i] = {"ok": False, "content": "工具线程异常：%s: %s"
                                          % (e.__class__.__name__, e),
                                          "meta": {"error": "TOOL_FAILED"}}

        # 3) 按原顺序归并（结果、事件、消息三处顺序一致）
        for i, tc in enumerate(tcs):
            r = results[i]
            if r is None:
                r = {"ok": False, "content": "已中断，未执行",
                     "meta": {"error": "SYS_CANCELLED", "cancelled": True}}
            self.bus.emit("tool_result", r, trace_id=state.trace_id)
            meta = r.get("meta") or {}
            if trace is not None:
                trace.tool(name=tc.get("name"), arguments=tc.get("arguments"),
                           ok=bool(r.get("ok")), error=meta.get("error"),
                           latency_ms=meta.get("latency_ms"), tier=meta.get("tier"),
                           decision=meta.get("decision"),
                           content_chars=len(str(r.get("content") or "")),
                           parallel=len(tcs) > 1)
            state.messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id"),
                "name": tc.get("name"),
                "content": sanitize(json.dumps(r, ensure_ascii=False, default=str))[:20000],
            })
        return False

    def _plan_batches(self, tcs: List[Dict[str, Any]]) -> List[List[int]]:
        """依赖分层：一个调用只有在它声明的依赖都已跑完的那一轮才能上。

        - 默认（未声明 `depends_on`）：全部放同一轮 -> 并行。
        - 声明了依赖且依赖也在本轮消息里：放到依赖之后的那一轮 -> 串行。
        - 环依赖：拆开兜底串行，不会死循环。
        """
        names = [str(tc.get("name")) for tc in tcs]
        deps: List[set] = []
        for tc in tcs:
            m = self.tools.meta(tc.get("name")) if self.tools else None
            deps.append(set(getattr(m, "depends_on", None) or []) if m else set())
        remaining = list(range(len(tcs)))
        done: set = set()
        batches: List[List[int]] = []
        while remaining:
            present = {names[i] for i in remaining}
            batch = [i for i in remaining
                     if all((dep not in names) or (dep in done) for dep in deps[i])]
            if not batch:               # 环：强行拆一个，保证收敛
                batch = [remaining[0]]
            batches.append(batch)
            got = set(batch)
            remaining = [i for i in remaining if i not in got]
            done |= {names[i] for i in batch}
        return batches

    # ---------- 状态机 / 终止 ----------
    def _transition(self, state: AgentState, to: str, reason: str = "") -> Dict[str, Any]:
        res = self.state_machine.transition(to, reason=reason)
        state.phase = self.state_machine.state
        if res.get("ok"):
            state.status = state.phase
            if not res.get("noop"):
                self.bus.emit("state_change", {"from": res["from"], "to": res["to"],
                                               "reason": reason}, trace_id=state.trace_id)
                if self._trace is not None:
                    self._trace.state(**{"from": res["from"], "to": res["to"], "reason": reason})
        return res

    def _terminate(self, state: AgentState, code: str,
                   trace: Optional[TraceWriter] = None, error: Any = None,
                   reason: str = "") -> None:
        """收敛到终态：相位 + 既有 status 口径都落定，并写一条 trace。"""
        if error is not None:
            state.error = error
        target = _TERMINAL_PHASE.get(code, RunState.ERROR)
        self._transition(state, target, "terminate:%s" % code)
        state.status = code
        state.termination = code
        if trace is not None:
            trace.write({"type": "terminate", "termination": code, "phase": state.phase,
                         "reason": reason, "status": code, "error": state.error})

    # ---------- 内部 ----------
    def _persist(self, state: AgentState, from_idx: int) -> None:
        """把新增的消息落进 L1（append-only）。记忆坏了不影响主流程。

        逐条清洗 + 逐条兜错：一条脏消息不该拖垮整批落盘（2026-09-28 踩过）。
        """
        if not self.store:
            return
        for m in sanitize_messages(state.messages[from_idx:]):
            try:
                self.store.append(state.session_id, _strip_meta(m), trace_id=state.trace_id)
            except Exception as e:  # noqa: BLE001
                self._log("memory_write_failed", error=str(e))

    def _log(self, msg: str, **fields: Any) -> None:
        if self.logger is None:
            return
        try:
            from runtime.log import log_event
            log_event(self.logger, msg, **fields)
        except Exception:
            pass

    def _classify_error(self, error: Any) -> Dict[str, Any]:
        """把任意错误归一成 {code,message,category,retryable,remediation,detail}。"""
        if isinstance(error, AgentError):
            return error.to_dict()
        if isinstance(error, BaseException):
            return from_exception(error)
        if isinstance(error, dict):
            return classify(error.get("code") or "SYS_INTERNAL", error.get("message") or "",
                            detail=error.get("detail"), retryable=error.get("retryable"))
        if error is None:
            return classify("SYS_INTERNAL", "unknown error")
        return classify("SYS_INTERNAL", str(error))

    def _execute(self, tc: Dict[str, Any], state: AgentState) -> Dict[str, Any]:
        """执行一次工具调用：全部走注册表这条唯一路径（校验→鉴权→执行→截断→审计）。"""
        name = tc.get("name")
        args = tc.get("arguments") or {}
        if self.tools is None:
            return {"ok": False, "content": "没有可用的工具", "meta": {"error": "TOOL_NOT_FOUND"}}
        try:
            ctx: Dict[str, Any] = {"gate": self.gate, "session_id": state.session_id,
                                   "trace_id": state.trace_id, "issuer": state.issuer}
            if self.gate is not None:
                ctx["approval"] = self._make_approval(name, state)
            return self.tools.run(name, args, ctx)
        except Exception as e:  # noqa: BLE001 —— 工具层意外也不许崩
            d = from_exception(e)
            return {"ok": False, "content": "工具执行异常：%s" % d.get("message"),
                    "meta": {"error": d.get("code") or "TOOL_FAILED"}}

    def _make_approval(self, name: str, state: AgentState) -> Callable[[Dict[str, Any]], bool]:
        """把"是否批准"包成回调：发事件、问人、记住口径（永久允许/永久拒绝）。

        并发工具可能同时要审批：用锁串行化，保证相位（acting->waiting_approval->acting）
        不会被两个线程交叉踩乱。
        """
        def ask(prompt: Dict[str, Any]) -> bool:
            with self._approval_lock:
                self._transition(state, RunState.WAITING_APPROVAL, "approval:%s" % name)
                self.bus.emit("approval_required", {"name": prompt.get("name"),
                                                    "arguments": prompt.get("args"),
                                                    "tier": prompt.get("tier"),
                                                    "second_confirm": prompt.get("second_confirm"),
                                                    "reason": prompt.get("reason")},
                              trace_id=state.trace_id)
                raw = self.ask_callback(prompt) if self.ask_callback else False
                choice = _norm_choice(raw)
                if choice in ("allow_always", "deny_always"):
                    # 长期授权只有 owner 能给；assistant/external/self 只能给"本次"
                    if self.subjects is not None and not self.subjects.can_grant_persistent(state.issuer):
                        self._log("persistent_grant_denied", issuer=state.issuer, skill=name)
                        self.bus.emit("approval_note",
                                      {"name": name, "note": "派发者 %s 无权授予长期授权，本次按一次性处理"
                                       % state.issuer}, trace_id=state.trace_id)
                        choice = "allow_once" if choice == "allow_always" else "deny_once"
                    elif self.approvals is not None:
                        try:
                            self.approvals.remember(name, choice)
                        except Exception as e:  # noqa: BLE001
                            self._log("approval_remember_failed", error=str(e))
                self._transition(state, RunState.ACTING, "approval_decided:%s" % choice)
                self.bus.emit("approval_decided", {"name": name, "choice": choice},
                              trace_id=state.trace_id)
                return choice in ("allow_once", "allow_always")
        return ask


def _trace() -> str:
    import uuid
    return uuid.uuid4().hex[:16]


def _usage_delta(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, int]:
    return {k: int(after.get(k, 0)) - int(before.get(k, 0))
            for k in ("prompt_tokens", "completion_tokens")}


def _snip(text: Any, n: int = 500) -> str:
    s = str(text or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def _strip_meta(m: Dict[str, Any]) -> Dict[str, Any]:
    """去掉内部用的 _id/_ts 之类字段（接口之间只传纯数据）。"""
    return {k: v for k, v in m.items() if not str(k).startswith("_")}


def _norm_choice(raw: Any) -> str:
    """把审批返回值归一成四选一：allow_once / allow_always / deny_once / deny_always。"""
    if raw is True:
        return "allow_once"
    if raw is False or raw is None:
        return "deny_once"
    s = str(raw).strip().lower()
    table = {
        "y": "allow_once", "yes": "allow_once", "allow": "allow_once", "once": "allow_once",
        "allow_once": "allow_once", "1": "allow_once",
        "a": "allow_always", "always": "allow_always", "allow_always": "allow_always",
        "永久": "allow_always", "all": "allow_always",
        "n": "deny_once", "no": "deny_once", "deny": "deny_once", "deny_once": "deny_once", "0": "deny_once",
        "never": "deny_always", "deny_always": "deny_always", "永不": "deny_always",
    }
    return table.get(s, "deny_once")
