#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M4 自检：任务集加载 + 规则/模型打分器 + 回归报告 + runner 端到端。

全程用假 provider / 离线任务，**不调任何外部模型**。
跑法：.venv/bin/python scripts/selftest_evals.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from evals import loader, report, runner, scorer          # noqa: E402
from evals.scorer import is_subsequence                   # noqa: E402

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("✓" if cond else "✗", name, ("  — " + str(extra)) if (extra and not cond) else ""))


def _env(message, p=5, c=2):
    return {"ok": True, "v": 1,
            "data": {"message": message,
                     "usage": {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c},
                     "finish_reason": "stop"},
            "error": None, "meta": {}}


class FakeProvider:
    """按消息内容脚本化的假模型：够跑通 calc / 权限被拦 / 闲聊，不联网。"""
    name = "fake"
    model = "fake"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None, cfg=None):
        self.calls += 1
        system_text = " ".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
        last = messages[-1] if messages else {}
        role = last.get("role")
        content = str(last.get("content") or "")
        if "记忆抽取器" in system_text:
            return _env({"role": "assistant",
                         "content": '[{"content":"用户交付文档偏好 docx 格式","type":"preference","importance":0.8}]'})
        if role == "tool":
            if "42" in content:
                return _env({"role": "assistant", "content": "6*7=42。"})
            return _env({"role": "assistant", "content": "好的，完成了。"})
        if "run_command" in content:
            return _env({"role": "assistant",
                         "tool_calls": [{"id": "c0", "name": "run_command", "arguments": {"cmd": "ls"}}]})
        if "note_append" in content:
            return _env({"role": "assistant",
                         "tool_calls": [{"id": "c1", "name": "note_append", "arguments": {"text": "评测"}}]})
        if "calc" in content or "6*7" in content or "6 * 7" in content:
            return _env({"role": "assistant",
                         "tool_calls": [{"id": "c2", "name": "calc", "arguments": {"expr": "6*7"}}]})
        return _env({"role": "assistant", "content": "好的，你好。"})


class JudgeProvider:
    """模型判分用的假 provider：固定给 9 分。"""

    def chat(self, messages, tools=None, cfg=None):
        return _env({"role": "assistant", "content": '{"score": 9, "reason": "切题且简洁"}'})


def build_fake_ctx(tmp):
    from agent.loop import AgentLoop
    from context import Budget, ContextBuilder, PromptStore
    from evals.tools import register_eval_tools
    from security import ApprovalStore, AuditLog, PermissionGate, PermissionPolicy, Sandbox
    from skills import builtin as builtin_tools
    from skills.registry import SkillMeta, ToolRegistry

    reg = ToolRegistry()
    reg.register_many(builtin_tools.BUILTIN)
    register_eval_tools(reg)
    reg.register(SkillMeta(name="note_append", description="写", permission="write",
                           parameters={"type": "object", "properties": {"text": {"type": "string"}},
                                       "required": ["text"]},
                           handler=lambda a: {"ok": True, "content": "wrote"}))
    sandbox = Sandbox({"read_roots": [ROOT, "/tmp"], "write_roots": [tmp, "/tmp"]}, base_dir=ROOT)
    gate = PermissionGate({}, policy=PermissionPolicy({}), sandbox=sandbox,
                          approvals=ApprovalStore(path=os.path.join(tmp, "approvals.json")),
                          audit=AuditLog(path=os.path.join(tmp, "audit.jsonl")), base_dir=ROOT)
    reg.gate = gate
    reg.audit = gate.audit
    prompts = PromptStore(os.path.join(ROOT, "config", "prompts"))
    context = ContextBuilder(budget=Budget(4096, 512), prompts=prompts, keep_last=4)
    provider = FakeProvider()
    loop = AgentLoop(provider=provider, tools=reg, gate=gate, context=context,
                     prompt_vars={"name": "测试调用方", "creature": "测试助理", "style": "简洁"},
                     max_steps=4, store=None, retriever=None, writer=None, procedural=None,
                     memory_cfg={"summarize": {"enabled": False}}, ask_callback=None)
    return {"loop": loop, "tools": reg, "gate": gate, "provider_name": "fake",
            "providers": {"fake": provider}, "cfg": {}, "budget": context.budget, "prompts": prompts}


OFFLINE_RAW = [
    {"id": "off_chat", "input": "你好", "tags": ["chat"],
     "expect": {"status": "done", "no_tools": True, "answer_any": ["好"]}, "constraints": {"max_steps": 1}},
    {"id": "off_calc", "input": "用 calc 算 6*7", "tags": ["single_tool"],
     "expect": {"status": "done", "tools": ["calc"], "answer_any": ["42"]}, "constraints": {"max_steps": 3}},
    {"id": "off_perm", "input": "用 note_append 记一条", "tags": ["permission"],
     "expect": {"status": "done", "tools": ["note_append"], "tool_errors": {"note_append": "TOOL_DENIED"}},
     "constraints": {"max_steps": 3, "no_error": True}},
    {"id": "off_danger", "input": "用 run_command 执行 ls", "tags": ["permission"],
     "expect": {"status": "done", "tools": ["run_command"], "tool_errors": {"run_command": "TOOL_DENIED"},
                "tool_tiers": {"run_command": "danger"}},
     "constraints": {"max_steps": 3, "no_error": True}},
]


def test_memory_offline(tmp):
    """记忆写入 → 跨会话召回（不经过模型，只验存储+检索链路）。"""
    from memory import MemoryStore
    from memory.retrieval import Retriever
    from memory.writer import MemoryWriter

    store = MemoryStore(db_path=os.path.join(tmp, "mem.db"))
    retriever = Retriever(store)                     # 无 embedding，纯关键词/时间/重要度
    writer = MemoryWriter(store, retriever=retriever, model_judge=False)
    items = writer.extract([{"role": "user", "content": "请记住：我以后交付文档统一用 docx 格式。"}],
                           provider=FakeProvider())
    check("记忆抽取（假 provider）", bool(items) and "docx" in items[0]["content"], items)
    res = writer.write(items[0], session_id="s1", judge=False)
    check("记忆落库", res.get("action") == "created", res)
    hits = retriever.search("我交付文档偏好什么格式？", k=5)
    check("跨会话检索命中", bool(hits) and any("docx" in h["content"] for h in hits), hits)
    store.close()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="myagent-eval-selftest-")

    # ================= 1. 任务集加载与覆盖 =================
    print("\n[1] 任务集加载与覆盖")
    tasks = loader.load_tasks(os.path.join(ROOT, "evals", "tasks"))
    check("任务数在 20~30 之间", 20 <= len(tasks) <= 30, "实际 %d" % len(tasks))
    ids = [t["id"] for t in tasks]
    check("任务 id 唯一", len(ids) == len(set(ids)))
    check("每条都有 id/input/expect/tags", all(t["id"] and t["input"] and t["expect"] and t["tags"] for t in tasks))
    cats = set()
    for t in tasks:
        cats.update(t["tags"])
    need = {"chat", "no_tool", "single_tool", "multi_step", "permission", "memory", "long_input"}
    check("六类覆盖齐全", need.issubset(cats), "缺：%s" % sorted(need - cats))
    mem = [t for t in tasks if "memory" in t["tags"]]
    check("记忆任务都是多轮（跨会话）", bool(mem) and all(len(t["turns"]) >= 2 for t in mem))
    long_t = [t for t in tasks if "long_input" in t["tags"]]
    check("超长输入任务已被填充展开", bool(long_t) and max(len(t["input"]) for t in long_t) > 20000,
          "最长 %d" % max((len(t["input"]) for t in long_t), default=0))
    bad_ok = False
    try:
        loader.normalize_task({"input": "x", "expect": {}, "tags": []}, source="bad")
    except ValueError:
        bad_ok = True
    check("缺必填字段会报错", bad_ok)

    # ================= 2. 规则打分器 =================
    print("\n[2] 规则打分器")
    check("子序列判定", is_subsequence(["a", "b"], ["a", "x", "b"]) and not is_subsequence(["b", "a"], ["a", "b"]))
    calc_task = loader.normalize_task(
        {"id": "t_calc", "input": "算 6*7", "tags": ["single_tool"],
         "expect": {"status": "done", "tools": ["calc"], "answer_any": ["42"],
                    "tool_errors": {"calc": "TOOL_DENIED"}}, "constraints": {"max_steps": 3}}, source="inline")
    good = {"status": "done", "answer": "6*7=42", "steps": 2, "usage": {"total_tokens": 10}, "latency_ms": 100,
            "tool_names": ["calc"], "tool_calls": [{"name": "calc", "error": None}], "context_stats": {}}
    sc = scorer.score_task(calc_task, dict(good, tool_calls=[{"name": "calc", "error": "TOOL_DENIED"}]), None)
    check("全项通过 → score=1.0 且 passed", sc["score"] == 1.0 and sc["passed"], sc)
    bad = scorer.score_task(calc_task, dict(good, tool_names=[], steps=9, answer="不知道"), None)
    check("工具没调/步数超/答案错 → 扣分且给失败原因", not bad["passed"] and len(bad["fail_reasons"]) >= 3,
          bad["fail_reasons"])
    check("失败原因可读", all(isinstance(x, str) and ":" in x for x in bad["fail_reasons"]), bad["fail_reasons"])

    # ================= 3. 可选模型判分 =================
    print("\n[3] 可选模型判分")
    jr = scorer.judge_answer("回答要提到 docx", "偏好什么格式？", "你用 docx。", JudgeProvider())
    check("模型判分解析 JSON", jr["score"] == 0.9 and jr["ok"], jr)
    jtask = dict(calc_task)
    jtask["judge"] = "回答要提到 docx"
    with_judge = scorer.score_task(jtask, dict(good, tool_calls=[{"name": "calc", "error": "TOOL_DENIED"}])
                                   , JudgeProvider())
    check("judge 作为一项计入 checks", any(c["name"] == "judge" for c in with_judge["checks"]),
          [c["name"] for c in with_judge["checks"]])
    no_judge = scorer.score_task(jtask, good, None)
    check("默认关判分（不传 provider 就没有 judge 项）",
          not any(c["name"] == "judge" for c in no_judge["checks"]))

    # ================= 4. 回归报告对比 =================
    print("\n[4] 回归报告对比")
    base = {"tasks": {"a": {"score": 1.0}, "b": {"score": 0.5}, "c": {"score": 1.0}}}
    cur = {"tasks": [{"id": "a", "score": 0.5, "fail_reasons": ["status: 挂了"]},
                     {"id": "b", "score": 0.8}, {"id": "d", "score": 1.0}]}
    cmp = report.compare(base, cur)
    check("退步项识别", [x["id"] for x in cmp["regressions"]] == ["a"], cmp["regressions"])
    check("进步项识别", [x["id"] for x in cmp["improvements"]] == ["b"], cmp["improvements"])
    check("新增/移除识别", [x["id"] for x in cmp["added"]] == ["d"] and [x["id"] for x in cmp["removed"]] == ["c"],
          (cmp["added"], cmp["removed"]))

    # ================= 5. runner 端到端（假 provider） =================
    print("\n[5] runner 端到端（假 provider）")
    off_tasks = [loader.normalize_task(r, source="inline") for r in OFFLINE_RAW]
    ctx = build_fake_ctx(tmp)
    seen = []
    run = runner.run_tasks(off_tasks, ctx, provider_name="fake", run_id="selftest-run",
                           progress=lambda i, n, r: seen.append(r["id"]))
    check("四条离线任务全部满分", run["summary"]["total"] == 4 and run["summary"]["passed"] == 4,
          run["summary"])
    by_id = {t["id"]: t for t in run["tasks"]}
    check("单工具任务记录了 calc 轨迹", by_id["off_calc"]["tool_names"] == ["calc"]
          and by_id["off_calc"]["usage"]["total_tokens"] > 0, by_id["off_calc"]["tool_names"])
    check("写入级无审批通道 → TOOL_DENIED", any(c["error"] == "TOOL_DENIED"
          for c in by_id["off_perm"]["tool_calls"]), by_id["off_perm"]["tool_calls"])
    check("危险级无审批通道 → TOOL_DENIED 且 tier=danger",
          any(c["error"] == "TOOL_DENIED" and c["tier"] == "danger" for c in by_id["off_danger"]["tool_calls"]),
          by_id["off_danger"]["tool_calls"])
    check("进度回调被逐条触发", seen == [t["id"] for t in off_tasks], seen)

    runs_dir = os.path.join(tmp, "runs")
    path = runner.save_run(run, runs_dir)
    check("轨迹落盘到 runs/<时间戳>.json", os.path.isfile(path) and path.endswith("selftest-run.json"), path)
    loaded = report.load_json(runner.latest_run_path(runs_dir))
    check("轨迹可回读且含逐条工具与 token", loaded["tasks"][1]["tool_names"] == ["calc"]
          and "usage" in loaded["tasks"][1])

    bpath = os.path.join(tmp, "baseline.json")
    report.write_baseline(bpath, run)
    bl = report.load_baseline(bpath)
    check("基线可写可读", bl and bl["tasks"]["off_calc"]["score"] == 1.0)
    txt = report.format_report(report.compare(bl, run), run, bl, path)
    check("报告含退步/进步小节", "退步项" in txt and "进步项" in txt)
    rc = runner.main(["--report", "--runs-dir", runs_dir, "--baseline", bpath])
    check("--report 一条命令出报告（不调模型）", rc == 0)

    # ================= 6. 超长输入不炸 =================
    print("\n[6] 超长输入（离线假模型）")
    long_task = loader.normalize_task(
        {"id": "off_long", "input": "读完只回一句：好的。",
         "input_pad": {"text": "这是一段填充文本，本身没有意义。", "repeat": 3000},
         "tags": ["long_input"], "expect": {"status": "done", "answer_nonempty": True},
         "constraints": {"max_steps": 2, "no_error": True}}, source="inline")
    check("超长输入已展开到 2 万字以上", len(long_task["input"]) > 20000, len(long_task["input"]))
    run_long = runner.run_tasks([long_task], ctx, provider_name="fake", run_id="selftest-long")
    check("超长输入任务正常收敛", run_long["tasks"][0]["status"] == "done"
          and run_long["tasks"][0]["passed"], run_long["tasks"][0]["fail_reasons"])

    # ================= 7. 记忆链路（离线） =================
    print("\n[7] 记忆写入 → 跨会话召回（离线）")
    test_memory_offline(tmp)

    # ================= 汇总 =================
    print("\n" + "=" * 52)
    print("M4 自检：%d/%d 通过" % (len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - %s" % f)
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
