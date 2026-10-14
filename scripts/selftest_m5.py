#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M5 自检：状态机 + 并发 + trace + 费用 + 中断恢复 + 错误分类。

**全程不调外部模型**：用脚本化假 provider + sleep 假工具，结果可离线复现。
跑法：.venv/bin/python scripts/selftest_m5.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.events import EventBus                                   # noqa: E402
from agent.loop import AgentLoop, Termination                       # noqa: E402
from agent.state import AgentState                                  # noqa: E402
from context import Budget, ContextBuilder, PromptStore             # noqa: E402
from memory import MemoryStore                                      # noqa: E402
from providers.base import envelope, err                            # noqa: E402
from runtime.cancel import CancelToken                              # noqa: E402
from runtime.cost import CostCalculator, format_cost                # noqa: E402
from runtime.errors import (AgentError, FATAL, NEEDS_HUMAN,         # noqa: E402
                            RETRASSISTANTBLE, classify, from_exception, retry_call)
from runtime.state_machine import (IllegalTransition, RunState,     # noqa: E402
                                    StateMachine, is_legal)
from runtime.trace import TraceStore, plan_resume                   # noqa: E402
from skills.registry import SkillMeta, ToolRegistry                 # noqa: E402

TMP = tempfile.mkdtemp(prefix="m5test-")
TRACES = os.path.join(TMP, "traces")
PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("✓" if cond else "✗", name, ("  — " + str(extra)) if (extra and not cond) else ""))


# ---------------- 假 provider / 假工具 ----------------
def resp_final(text, p=5, c=2):
    return {"ok": True, "v": 1,
            "data": {"message": {"role": "assistant", "content": text},
                     "usage": {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c},
                     "finish_reason": "stop"},
            "error": None, "meta": {}}


def resp_tools(calls, p=5, c=2, prefix=""):
    return {"ok": True, "v": 1,
            "data": {"message": {"role": "assistant", "content": prefix, "tool_calls": [
                {"id": "c%d" % i, "name": n, "arguments": a} for i, (n, a) in enumerate(calls)]},
                "usage": {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c},
                "finish_reason": "tool_calls"},
            "error": None, "meta": {}}


def resp_error(code, message, retryable=None):
    return {"ok": False, "v": 1, "data": None, "error": err(code, message, retryable), "meta": {}}


class ScriptProvider:
    """按调用次序吐脚本；每条是 dict 或返回 dict 的函数。"""

    def __init__(self, script, name="fake", model="fake"):
        self.script = list(script)
        self.calls = 0
        self.name = name
        self.model = model

    def chat(self, messages, tools=None, cfg=None):
        i = min(self.calls, len(self.script) - 1)
        self.calls += 1
        r = self.script[i]
        return r() if callable(r) else r


def _sleep_handler(seconds, tag=""):
    def h(args):
        time.sleep(seconds)
        return {"ok": True, "content": "slept %.2f:%s" % (seconds, tag), "meta": {}}
    return h


def _echo_handler(tag):
    def h(args):
        return {"ok": True, "content": "echo:%s:%s" % (tag, args.get("x", "")), "meta": {}}
    return h


def build_loop(tmp, provider, tools, trace=True, store=None, pricing=None, parallel=True,
               gate=None):
    prompts = PromptStore(os.path.join(ROOT, "config", "prompts"))
    context = ContextBuilder(budget=Budget(4096, 512), prompts=prompts, keep_last=4)
    return AgentLoop(provider=provider, tools=tools, gate=gate, context=context,
                     prompt_vars={"name": "测试调用方", "creature": "测试助理", "style": "简洁"},
                     max_steps=6, store=store, retriever=None, writer=None, procedural=None,
                     memory_cfg={"summarize": {"enabled": False}},
                     pricing=pricing or {}, trace_enabled=trace,
                     trace_dir=os.path.join(tmp, "traces"),
                     parallel_tools=parallel, max_workers=4)


def run_calls(loop, calls, state=None):
    """直接驱动一轮工具执行，返回 (耗时, 事件, state)。"""
    st = state or AgentState(session_id="s", trace_id="t-%d" % time.time_ns())
    msg = {"role": "assistant", "content": "",
           "tool_calls": [{"id": "c%d" % i, "name": n, "arguments": a}
                          for i, (n, a) in enumerate(calls)]}
    loop.bus = EventBus()
    events = []
    loop.bus.subscribe(lambda e: events.append(e))
    t0 = time.time()
    blocked = loop._run_tools(msg, st, [], None, trace=None)
    dt = time.time() - t0
    return dt, blocked, events, st


# ================= 1. 状态机 =================
def test_state_machine():
    print("\n[1] 状态机：合法 / 非法转移")
    sm = StateMachine()
    check("初始相位唯一且可查询 = idle", sm.state == RunState.IDLE)
    chain = [RunState.THINKING, RunState.ACTING, RunState.WAITING_APPROVAL,
             RunState.ACTING, RunState.THINKING, RunState.DONE]
    ok = True
    for to in chain:
        ok = ok and sm.transition(to)["ok"]
    check("合法链路 idle→thinking→acting→waiting_approval→acting→thinking→done 全通",
          ok and sm.state == RunState.DONE, sm.state)
    check("终态没有出边", sm.legal_next() == [])
    check("合法表判断函数", is_legal(RunState.IDLE, RunState.THINKING)
          and not is_legal(RunState.DONE, RunState.THINKING))
    check("终态再转移被拒（非 strict 只记录）",
          not sm.transition(RunState.THINKING)["ok"] and len(sm.violations) == 1
          and sm.state == RunState.DONE, sm.violations)
    strict = StateMachine(strict=True)
    strict.transition(RunState.THINKING)
    raised = False
    try:
        strict.transition(RunState.WAITING_APPROVAL)   # thinking -> waiting_approval 非法
    except IllegalTransition:
        raised = True
    check("strict 模式非法转移直接报错 IllegalTransition", raised)
    check("strict 报错后状态没有被改坏", strict.state == RunState.THINKING, strict.state)
    seen = []
    sm2 = StateMachine(on_change=lambda rec: seen.append((rec["from"], rec["to"])))
    sm2.transition(RunState.THINKING)
    check("状态变化有回调（可观测）", seen == [(RunState.IDLE, RunState.THINKING)], seen)

    # 内核集成：跑完一条任务后相位落定，事件里有状态轨迹
    reg = ToolRegistry()
    loop = build_loop(TMP, ScriptProvider([resp_final("好")]), reg, trace=False)
    loop.bus = EventBus()
    seq = []
    loop.bus.subscribe(lambda e: seq.append((e["type"], (e.get("payload") or {}).get("to"))))
    r = loop.run({"input": "你好"})
    check("内核跑完后引擎状态 = done", loop.state_machine.state == RunState.DONE)
    check("信封里带着 phase/termination", r["data"]["phase"] == "done"
          and r["data"]["termination"] == "done", r["data"].get("phase"))
    check("状态变化走事件总线（state_change）", any(t == "state_change" for t, _ in seq))


# ================= 2. 错误分类 =================
def test_error_classification():
    print("\n[2] 错误分类：可重试 / 不可重试 / 需人工")
    check("超时 -> 可重试", classify("MODEL_TIMEOUT", "slow")["category"] == RETRASSISTANTBLE)
    check("限流 -> 可重试", classify("MODEL_RATE_LIMIT", "429")["category"] == RETRASSISTANTBLE)
    check("鉴权失败 -> 需人工", classify("MODEL_AUTH", "bad key")["category"] == NEEDS_HUMAN)
    check("权限被拒 -> 需人工", classify("TOOL_DENIED", "no")["category"] == NEEDS_HUMAN)
    check("请求非法 -> 不可重试", classify("MODEL_BAD_REQUEST", "bad")["category"] == FATAL)
    check("未登记错误码按关键词兜底（permission -> 需人工）",
          classify("WAT", "permission denied")["category"] == NEEDS_HUMAN)
    d = classify("MODEL_TIMEOUT", "slow")
    check("统一错误字典带补救路径", d["retryable"] is True and bool(d["remediation"]))
    ae = AgentError("MODEL_AUTH", "bad key").to_dict()
    check("AgentError 归一成同一形状", ae["category"] == NEEDS_HUMAN and ae["retryable"] is False
          and "remediation" in ae, ae)
    ex = from_exception(TimeoutError("took too long"))
    check("任意异常也能归类", ex["category"] == RETRASSISTANTBLE and ex.get("cause"), ex)

    # retry_call：可重试重试、不可重试立即透出
    tries = {"n": 0}

    def flaky():
        tries["n"] += 1
        if tries["n"] < 3:
            raise AgentError("MODEL_TIMEOUT", "slow")
        return "ok"

    got = retry_call(flaky, attempts=3, sleep=lambda s: None)
    check("可重试错误按次数重试后成功", got == "ok" and tries["n"] == 3, tries)
    fatal_tries = {"n": 0}

    def fatal():
        fatal_tries["n"] += 1
        raise AgentError("MODEL_AUTH", "bad key")

    threw = False
    try:
        retry_call(fatal, attempts=3, sleep=lambda s: None)
    except AgentError:
        threw = True
    check("不可重试错误不重试、直接透出", threw and fatal_tries["n"] == 1, fatal_tries)

    # 内核：provider 报错不崩，归类进信封
    loop = build_loop(TMP, ScriptProvider([resp_error("MODEL_AUTH", "bad key")]),
                      ToolRegistry(), trace=False)
    r = loop.run({"input": "你好"})
    check("provider 出错不崩、返回信封", r["ok"] is False and r["data"]["status"] == "error")
    check("出错有归类 + 补救路径", r["error"]["category"] == NEEDS_HUMAN
          and bool(r["error"]["remediation"]), r.get("error"))
    check("出错后相位 = error", r["data"]["phase"] == "error")
    loop2 = build_loop(TMP, ScriptProvider([resp_error("MODEL_TIMEOUT", "slow")]),
                       ToolRegistry(), trace=False)
    r2 = loop2.run({"input": "你好"})
    check("可重试错误标 retryable=True", r2["error"]["category"] == RETRASSISTANTBLE
          and r2["error"]["retryable"] is True)


# ================= 3. 并发 =================
def test_concurrency():
    print("\n[3] 并发：无依赖并行 ≈ 单个；有依赖串行 ≈ 相加")
    S = 0.5
    reg = ToolRegistry()
    reg.register(SkillMeta(name="sleep_a", description="", permission="read", handler=_sleep_handler(S, "a")))
    reg.register(SkillMeta(name="sleep_b", description="", permission="read", handler=_sleep_handler(S, "b")))
    reg.register(SkillMeta(name="sleep_dep", description="", permission="read",
                           handler=_sleep_handler(S, "dep"), depends_on=["sleep_a"]))
    loop = build_loop(TMP, ScriptProvider([resp_final("x")]), reg, trace=False)

    single, _, _, _ = run_calls(loop, [("sleep_a", {})])
    parallel, blocked_p, events_p, _ = run_calls(loop, [("sleep_a", {}), ("sleep_b", {})])
    serial, _, events_s, _ = run_calls(loop, [("sleep_a", {}), ("sleep_dep", {})])
    print("    单个=%.2fs 并行=%.2fs 串行=%.2fs（sleep 每个 %.1fs）" % (single, parallel, serial, S))
    check("单个工具耗时接近 sleep", S - 0.1 < single < S + 0.6, "%.2f" % single)
    check("两个无依赖工具并行：接近一个而非相加", parallel < single * 1.6, "%.3f" % parallel)
    check("有依赖的工具严格串行：接近相加", serial > single * 1.6, "%.3f" % serial)
    check("并行明显快于串行", parallel < serial * 0.6, "par=%.3f ser=%.3f" % (parallel, serial))

    # 事件与结果顺序都按原调用顺序
    calls_p = [(e.get("payload") or {}).get("name") for e in events_p if e["type"] == "tool_call"]
    results_p = [(e.get("payload") or {}).get("content") for e in events_p if e["type"] == "tool_result"]
    check("并行时 tool_call 事件按原顺序", calls_p == ["sleep_a", "sleep_b"], calls_p)
    check("并行时 tool_result 也按原顺序归并",
          results_p == ["slept %.2f:a" % S, "slept %.2f:b" % S], results_p)
    check("并行结果归并进消息（顺序一致）",
          [m.get("name") for m in run_calls(loop, [("sleep_a", {}), ("sleep_b", {})])[3].messages]
          == ["sleep_a", "sleep_b"])

    # 关掉并行开关：即便无依赖也串行
    serial_loop = build_loop(TMP, ScriptProvider([resp_final("x")]), reg, trace=False, parallel=False)
    off, _, _, _ = run_calls(serial_loop, [("sleep_a", {}), ("sleep_b", {})])
    check("parallel=False 时退化为串行", off > single * 1.6, "%.3f" % off)

    # 并发 + 权限闸/审计：两个工具同时回写审计不踩踏
    from security import ApprovalStore, AuditLog, PermissionGate, PermissionPolicy, Sandbox
    audit = AuditLog(path=os.path.join(TMP, "audit_par.jsonl"))
    gate = PermissionGate({}, policy=PermissionPolicy({}),
                          sandbox=Sandbox({"read_roots": [TMP, "~"], "write_roots": [TMP]},
                                          base_dir=ROOT),
                          approvals=ApprovalStore(path=os.path.join(TMP, "ap_par.json")),
                          audit=audit, base_dir=ROOT)
    reg_g = ToolRegistry(gate=gate)
    reg_g.register(SkillMeta(name="sleep_a", description="", permission="read",
                             handler=_sleep_handler(S, "a")))
    reg_g.register(SkillMeta(name="sleep_b", description="", permission="read",
                             handler=_sleep_handler(S, "b")))
    loop_g = build_loop(TMP, ScriptProvider([resp_final("x")]), reg_g, trace=False, gate=gate)
    dtg, _, _, _ = run_calls(loop_g, [("sleep_a", {}), ("sleep_b", {})])
    rows = [r for r in audit.recent(20) if r.get("action") == "call"]
    check("并行工具同时回写审计不踩踏（2 条且仍并行）",
          len(rows) == 2 and dtg < single * 1.6, "rows=%d dt=%.2f" % (len(rows), dtg))


# ================= 4. trace 落盘与复盘 =================
def test_trace():
    print("\n[4] trace：每步落盘、可复盘")
    reg = ToolRegistry()
    reg.register(SkillMeta(name="echo_a", description="", permission="read", handler=_echo_handler("a")))
    reg.register(SkillMeta(name="echo_b", description="", permission="read", handler=_echo_handler("b")))
    provider = ScriptProvider([resp_tools([("echo_a", {"x": "1"}), ("echo_b", {"x": "2"})]),
                               resp_final("两个都跑完了", p=11, c=4)])
    loop = build_loop(TMP, provider, reg, trace=True)
    r = loop.run({"input": "跑两个工具"})
    tid = r["meta"]["trace_id"]
    path = r["data"]["trace_path"]
    check("trace 落盘到 <trace_id>.jsonl", bool(path) and os.path.isfile(path), path)
    check("落盘路径含 trace_id", tid in str(path))

    ts = TraceStore(TRACES)
    recs = ts.load(tid)
    types = [x.get("type") for x in recs]
    check("含 run_start / step / tool / terminate / final", all(
        t in types for t in ("run_start", "step", "tool", "terminate", "final")), types)
    steps = [x for x in recs if x.get("type") == "step"]
    tools = [x for x in recs if x.get("type") == "tool"]
    final = [x for x in recs if x.get("type") == "final"][0]
    check("每步有耗时与 token（可复盘）",
          len(steps) == 2 and all(isinstance(s.get("duration_ms"), int) for s in steps)
          and all(s.get("usage", {}).get("prompt_tokens", 0) >= 0 for s in steps), steps)
    check("每次工具调用有耗时/结果大小",
          len(tools) == 2 and all(t.get("latency_ms") is not None for t in tools), tools)
    check("final 含状态/耗时/总 token/费用",
          final.get("status") == "done" and final.get("elapsed_ms") is not None
          and final.get("usage", {}).get("prompt_tokens") == 16 and "cost" in final, final.get("status"))
    s = ts.summarize(recs)
    check("复盘摘要：输入/步数/工具数/费用齐全",
          s["input"] == "跑两个工具" and s["steps"] == 2 and s["tools"] == 2
          and s["status"] == "done" and s["cost"].get("total") is not None, s)
    step_sum = sum(int(x.get("duration_ms") or 0) for x in steps)
    check("每步耗时之和 <= 总耗时（口径自洽）", step_sum <= int(s["elapsed_ms"]) + 5,
          "steps=%d total=%s" % (step_sum, s["elapsed_ms"]))
    check("按 trace_id 也能找到最近一条", ts.latest_trace_id() == tid)


# ================= 5. 费用 =================
def test_cost():
    print("\n[5] 费用：token × 单价（按 provider 配）")
    pricing = {"currency": "CNY", "unit": 1000000,
               "providers": {"deepseek": {"input": 2.0, "output": 8.0},
                             "local": {"input": 0.0, "output": 0.0}}}
    cc = CostCalculator(pricing)
    c = cc.compute("deepseek", {"prompt_tokens": 1_000_000, "completion_tokens": 500_000})
    check("100 万输入 + 50 万输出 @2/8 = 6.0", c["total"] == 6.0, c)
    check("输入/输出分列", c["input_cost"] == 2.0 and c["output_cost"] == 4.0, c)
    check("本地 provider 不计费", cc.compute("local", {"prompt_tokens": 999999,
                                                      "completion_tokens": 999999})["total"] == 0.0)
    check("未配 provider 落到 default=0", cc.compute("mystery", {"prompt_tokens": 10})["total"] == 0.0)
    check("费用一行可读", "6.000000" in format_cost(c), format_cost(c))

    provider = ScriptProvider([resp_final("好", p=1_000_000, c=500_000)], name="deepseek")
    loop = build_loop(TMP, provider, ToolRegistry(), trace=False, pricing=pricing)
    r = loop.run({"input": "hi"})
    check("内核信封带本次费用 = 6.0", r["data"]["cost"]["total"] == 6.0, r["data"].get("cost"))
    check("跨 run 累计费用可查（CLI /cost 用）",
          loop.cost_totals["deepseek"]["total"] == 6.0, loop.cost_totals)
    r2 = loop.run({"input": "再来一次"})
    # ScriptProvider 脚本只有一条，第二次仍返回同一条 -> 再累加 6.0
    check("第二次任务后累计翻倍", loop.cost_totals["deepseek"]["total"] == 12.0, loop.cost_totals)


# ================= 6. 中断与恢复 =================
def test_interrupt_resume():
    print("\n[6] 中断与恢复：优雅退出 + 会话可续")
    store = MemoryStore(db_path=os.path.join(TMP, "m5.db"))
    token = CancelToken()

    def canceller(args):
        token.cancel("test_cancel")
        return {"ok": True, "content": "已请求中断", "meta": {}}

    reg = ToolRegistry()
    reg.register(SkillMeta(name="canceller", description="", permission="read", handler=canceller))
    provider = ScriptProvider([resp_tools([("canceller", {})]),
                               resp_final("已接着上次完成", p=8, c=3)], name="deepseek")
    loop = build_loop(TMP, provider, reg, trace=True, store=store,
                      pricing={"providers": {"deepseek": {"input": 2.0, "output": 8.0}}})

    # (a) 开跑前就取消：不崩、直接 cancelled
    pre = CancelToken()
    pre.cancel("preset")
    r0 = loop.run({"input": "不该开跑", "session_id": "s-pre"}, {"cancel_token": pre})
    check("开跑前取消 -> status=cancelled 且不崩",
          r0["ok"] is False and r0["data"]["status"] == "cancelled"
          and r0["data"]["phase"] == "done", r0["data"].get("status"))
    check("开跑前取消 -> provider 未被调用", provider.calls == 0, provider.calls)

    # (b) 工具执行中触发取消
    before = store.count_messages("s-cancel")
    r1 = loop.run({"input": "跑一个会取消的工具", "session_id": "s-cancel"}, {"cancel_token": token})
    check("工具中触发取消 -> 优雅退出（不崩、有终态）",
          r1["ok"] is False and r1["data"]["status"] == "cancelled"
          and r1["data"]["phase"] == "done", r1["data"].get("status"))
    check("取消后已产生的消息落盘（append-only）",
          store.count_messages("s-cancel") > before, store.count_messages("s-cancel"))

    # (c) 按 trace 出续跑方案
    tid = r1["meta"]["trace_id"]
    plan = TraceStore(TRACES).plan_resume(tid)
    check("plan_resume 能定位会话与原输入",
          plan and plan["session_id"] == "s-cancel" and plan["input"] == "跑一个会取消的工具", plan)
    check("中断的 trace 标记为可续", plan["resumable"] is True and plan["status"] == "cancelled", plan)
    check("plan_resume(trace_id) 模块函数等价", plan_resume(tid, TRACES)["session_id"] == "s-cancel")

    # (d) 同一会话接着跑：能完成
    cnt1 = store.count_messages("s-cancel")
    r2 = loop.run({"input": plan["resume_prompt"], "session_id": plan["session_id"]})
    check("接着跑能完成（会话可续）", r2["ok"] and r2["data"]["status"] == "done", r2["data"].get("status"))
    check("续跑复用同一会话并追加消息", r2["data"]["session_id"] == "s-cancel"
          and store.count_messages("s-cancel") > cnt1, store.count_messages("s-cancel"))

    # (e) 已正常结束的 trace 不标可续
    r3 = loop.run({"input": "正常一条", "session_id": "s-done"})
    plan3 = TraceStore(TRACES).plan_resume(r3["meta"]["trace_id"])
    check("正常结束的 trace resumable=False", plan3["resumable"] is False, plan3)
    store.close()


# ================= 7. 对外契约未破坏 =================
def test_contracts():
    print("\n[7] 既有对外契约未变")
    env = envelope(True, data={"x": 1}, meta={"trace_id": "t"})
    check("结果信封仍是 {ok,v,data,error,meta}",
          set(env.keys()) == {"ok", "v", "data", "error", "meta"} and env["ok"] is True, list(env))
    check("四类终止条件常量未变",
          (Termination.DONE, Termination.MAX_STEPS, Termination.BUDGET_EXHAUSTED,
           Termination.CANCELLED) == ("done", "max_steps", "budget_exhausted", "cancelled"))
    check("既有事件名仍在 EVENT_TYPES", {"thought", "tool_call", "tool_result", "final",
          "error", "state_change", "approval_required", "step"}.issubset(
          __import__("agent.events", fromlist=["EVENT_TYPES"]).EVENT_TYPES))


def main() -> int:
    print("M5 自检（不调外部模型）：状态机 / 并发 / trace / 费用 / 中断恢复 / 错误分类")
    test_state_machine()
    test_error_classification()
    test_concurrency()
    test_trace()
    test_cost()
    test_interrupt_resume()
    test_contracts()
    print("\n" + "=" * 52)
    print("M5 自检：%d/%d 通过" % (len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - %s" % f)
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
