# -*- coding: utf-8 -*-
"""评测运行器（M4）：批量跑任务 → 逐条落完整轨迹 → 打分 → 回归报告。

一条命令跑完全量：
    .venv/bin/python scripts/eval.py                 # 用默认 provider（deepseek）跑全量并出报告
    .venv/bin/python scripts/eval.py --report        # 只看最近一次 run 与基线的对比，不调模型

轨迹落盘：evals/runs/<时间戳>.json（每条任务：工具调用、token、步数、上下文统计、答案、分数）。
"""
from __future__ import annotations

import argparse
import copy
import glob
import json
import os
import shutil
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional

from evals import report as report_mod
from evals.loader import load_tasks
from evals.scorer import score_task
from evals.tools import register_eval_tools

EVALS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TASKS_DIR = os.path.join(EVALS_DIR, "tasks")
DEFAULT_BASELINE = os.path.join(EVALS_DIR, "baseline.json")
DEFAULT_RUNS_DIR = os.path.join(EVALS_DIR, "runs")


# ---------------- 运行 ----------------
def now_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def filter_tasks(tasks: List[Dict[str, Any]], only: Optional[str] = None,
                 limit: Optional[int] = None) -> List[Dict[str, Any]]:
    out = list(tasks)
    if only:
        keys = [k.strip() for k in str(only).split(",") if k.strip()]
        def match(t: Dict[str, Any]) -> bool:
            if t["id"] in keys:
                return True
            if any(k in (t.get("tags") or []) for k in keys):
                return True
            return any(k.lower() in str(t["id"]).lower() for k in keys)
        out = [t for t in out if match(t)]
    if limit is not None:
        out = out[: max(0, int(limit))]
    return out


def run_tasks(tasks: List[Dict[str, Any]], ctx: Dict[str, Any], *,
              provider_name: str = "", judge: bool = False, judge_provider: Any = None,
              run_id: Optional[str] = None,
              progress: Optional[Callable[[int, int, Dict[str, Any]], None]] = None) -> Dict[str, Any]:
    """在已装配好的 agent 上下文上批量跑任务，返回可序列化的 run 结构。"""
    loop = ctx["loop"]
    bus = loop.bus
    run_id = run_id or now_id()
    started = time.time()
    results: List[Dict[str, Any]] = []

    for idx, task in enumerate(tasks):
        t0 = time.time()
        tool_calls: List[Dict[str, Any]] = []
        memory_written: List[Any] = []
        memory_hits = 0
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        turns_out: List[Dict[str, Any]] = []
        session_ids: List[str] = []
        status: Optional[str] = None
        answer: Optional[str] = None
        steps = 0
        error: Any = None
        context_stats: Dict[str, Any] = {}
        budget_left: Any = None

        for ti, text in enumerate(task["turns"]):
            sid = task.get("session_id") or "%s-%s" % (run_id, task["id"])
            if len(task["turns"]) > 1 and not task.get("same_session"):
                sid = "%s-t%d" % (sid, ti + 1)
            session_ids.append(sid)
            start = len(bus.history)
            env = loop.run({"input": text, "session_id": sid})
            events = bus.history[start:]
            data = env.get("data") or {}
            status = data.get("status") or ("error" if not env.get("ok") else None)
            answer = data.get("answer")
            steps = int(data.get("steps") or 0)
            context_stats = data.get("context_stats") or {}
            error = env.get("error")
            budget_left = data.get("budget_left")
            u = _norm_usage(data.get("usage"))
            for k in usage_total:
                usage_total[k] += int(u.get(k) or 0)
            written = list(data.get("memory_written") or [])
            memory_written += written
            hits = int(data.get("memory_hits") or 0)
            memory_hits += hits
            calls = _extract_tools(events)
            tool_calls += calls
            turns_out.append({
                "input": text if len(text) <= 4000 else text[:4000] + "…（截断）",
                "input_chars": len(text),
                "session_id": sid,
                "status": status,
                "answer": answer,
                "steps": steps,
                "usage": u,
                "context_stats": context_stats,
                "memory_hits": hits,
                "memory_written": written,
                "tool_calls": calls,
                "events": _slim_events(events),
            })

        res: Dict[str, Any] = {
            "id": task["id"],
            "tags": task.get("tags") or [],
            "input": task["input"] if len(task["input"]) <= 4000 else task["input"][:4000] + "…（截断）",
            "turns": [t if len(t) <= 2000 else t[:2000] + "…（截断）" for t in task["turns"]],
            "status": status,
            "answer": answer,
            "steps": steps,
            "usage": usage_total,
            "budget_left": budget_left,
            "latency_ms": int((time.time() - t0) * 1000),
            "error": error,
            "context_stats": context_stats,
            "memory_hits": memory_hits,
            "memory_written": memory_written,
            "tool_calls": tool_calls,
            "tool_names": [c.get("name") for c in tool_calls],
            "session_ids": session_ids,
            "turns_detail": turns_out,
        }
        res.update(score_task(task, res, judge_provider=judge_provider if judge else None))
        results.append(res)
        if progress:
            try:
                progress(idx, len(tasks), res)
            except Exception:
                pass

    finished = time.time()
    passed = sum(1 for r in results if r.get("passed"))
    mean = round(sum(float(r.get("score") or 0.0) for r in results) / len(results), 4) if results else 0.0
    total_tokens = sum(int((r.get("usage") or {}).get("total_tokens") or 0) for r in results)
    summary = {"total": len(results), "passed": passed, "failed": len(results) - passed,
               "mean_score": mean, "total_tokens": total_tokens,
               "elapsed_ms": int((finished - started) * 1000)}
    return {"version": 1, "run_id": run_id, "provider": provider_name, "judge": bool(judge),
            "started_at": int(started * 1000), "finished_at": int(finished * 1000),
            "elapsed_ms": int((finished - started) * 1000),
            "tasks": results, "summary": summary}


# ---------------- 装配（评测用配置：省 token / 省 CPU，不改生产契约） ----------------
def build_eval_agent(provider_name: Optional[str] = None, cfg: Optional[Dict[str, Any]] = None,
                     data_dir: Optional[str] = None) -> Dict[str, Any]:
    from agent.factory import build_agent
    from runtime.config import load_config

    cfg = copy.deepcopy(cfg or load_config())
    if data_dir:
        cfg.setdefault("storage", {})["data_dir"] = data_dir
        cfg["storage"]["db_file"] = cfg["storage"].get("db_file") or "eval.db"
    mem = cfg.setdefault("memory", {})
    # 评测不载本地 embedding（CPU 慢）：关掉向量，纯关键词也足够测记忆读写；
    # 关掉模型判冲突与滚动摘要，避免评测时额外的模型调用（省 token）。
    mem.setdefault("embed", {})["enabled"] = False
    mem.setdefault("write", {})["model_judge"] = False
    mem.setdefault("summarize", {})["enabled"] = False
    mem.setdefault("procedural", {})["enabled"] = False

    ctx = build_agent(cfg, provider_name=provider_name, ask_callback=None)
    register_eval_tools(ctx["tools"])
    return ctx


# ---------------- 落盘 / CLI ----------------
def save_run(run: Dict[str, Any], runs_dir: str) -> str:
    os.makedirs(runs_dir, exist_ok=True)
    rid = run.get("run_id") or now_id()
    path = os.path.join(runs_dir, "%s.json" % rid)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(run, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def latest_run_path(runs_dir: str) -> Optional[str]:
    if not os.path.isdir(runs_dir):
        return None
    files = sorted(glob.glob(os.path.join(runs_dir, "*.json")))
    return files[-1] if files else None


def _print_progress(idx: int, total: int, res: Dict[str, Any]) -> None:
    mark = "✓" if res.get("passed") else "✗"
    line = "[%d/%d] %-26s %s score=%.2f steps=%s tokens=%s" % (
        idx + 1, total, res.get("id"), mark, float(res.get("score") or 0.0),
        res.get("steps"), (res.get("usage") or {}).get("total_tokens"))
    if not res.get("passed"):
        line += "  — " + "; ".join((res.get("fail_reasons") or [])[:2])
    print(line, flush=True)


def _extract_tools(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []
    for ev in events:
        if ev.get("type") == "tool_call":
            calls.append(ev.get("payload") or {})
        elif ev.get("type") == "tool_result":
            results.append(ev.get("payload") or {})
    out = []
    for i, c in enumerate(calls):
        r = results[i] if i < len(results) else {}
        meta = (r or {}).get("meta") or {}
        out.append({"name": c.get("name"), "arguments": c.get("arguments"),
                    "ok": bool(r.get("ok")) if r else False,
                    "error": meta.get("error"), "tier": meta.get("tier"),
                    "decision": meta.get("decision"), "latency_ms": meta.get("latency_ms"),
                    "content": _snip(r.get("content") if r else "", 500)})
    return out


def _norm_usage(u: Any) -> Dict[str, int]:
    """把 provider 的 usage 归一：内核信封只有 prompt/completion，total 这里补上。"""
    u = u or {}
    p = int(u.get("prompt_tokens") or 0)
    c = int(u.get("completion_tokens") or 0)
    t = int(u.get("total_tokens") or 0) or (p + c)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": t}


def _slim_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for ev in events:
        t = ev.get("type")
        p = ev.get("payload") or {}
        if t == "tool_result":
            p = {"ok": p.get("ok"), "content": _snip(p.get("content"), 1000), "meta": p.get("meta")}
        elif t == "thought":
            p = {"text": _snip(p.get("text"), 1000)}
        elif t == "tool_call":
            p = {"name": p.get("name"), "arguments": p.get("arguments")}
        out.append({"type": t, "ts": ev.get("ts"), "payload": p})
    return out


def _snip(text: Any, n: int = 500) -> str:
    s = str(text or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="eval", description="M4 评测：批量跑任务集 → 轨迹 → 打分 → 回归报告")
    ap.add_argument("--provider", default=None, help="模型 provider（默认取配置里的 default_provider，即 deepseek）")
    ap.add_argument("--only", default=None, help="只跑匹配项：逗号分隔的 id / tag / id 子串")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 条")
    ap.add_argument("--judge", action="store_true", help="打开模型判分（默认关，省 token）")
    ap.add_argument("--tasks", default=DEFAULT_TASKS_DIR, help="任务集目录或文件")
    ap.add_argument("--baseline", default=DEFAULT_BASELINE, help="基线 JSON 路径")
    ap.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR, help="轨迹输出目录")
    ap.add_argument("--update-baseline", action="store_true", help="用本次结果覆盖基线")
    ap.add_argument("--report", action="store_true", help="不跑模型，只对比最近一次 run 与基线")
    ap.add_argument("--list", action="store_true", help="只列任务")
    ap.add_argument("--keep-data", action="store_true", help="保留评测用的临时数据目录")
    args = ap.parse_args(argv)

    if args.report:
        path = latest_run_path(args.runs_dir)
        if not path:
            print("没有历史 run（%s），先跑一次评测" % args.runs_dir, file=sys.stderr)
            return 2
        run = report_mod.load_json(path)
        baseline = report_mod.load_baseline(args.baseline)
        cmp = report_mod.compare(baseline, run)
        print(report_mod.format_report(cmp, run, baseline, path))
        return 1 if (baseline and cmp["regressions"]) else 0

    try:
        tasks = load_tasks(args.tasks)
    except (FileNotFoundError, ValueError) as e:
        print("任务集加载失败：%s" % e, file=sys.stderr)
        return 2

    if args.list:
        for t in tasks:
            print("%-28s tags=%s turns=%d" % (t["id"], ",".join(t["tags"]), len(t["turns"])))
        print("共 %d 条任务（%s）" % (len(tasks), args.tasks))
        return 0

    tasks = filter_tasks(tasks, args.only, args.limit)
    if not tasks:
        print("没有匹配的任务（--only=%s）" % args.only, file=sys.stderr)
        return 2

    data_dir = tempfile.mkdtemp(prefix="myagent-eval-")
    ctx = None
    try:
        ctx = build_eval_agent(provider_name=args.provider, data_dir=data_dir)
        provider_name = ctx.get("provider_name") or ""
        print("评测开始：provider=%s 任务=%d judge=%s" % (provider_name, len(tasks),
                                                          "on" if args.judge else "off"), flush=True)
        run = run_tasks(
            tasks, ctx, provider_name=provider_name, judge=args.judge,
            judge_provider=(ctx["providers"].get(provider_name) if args.judge else None),
            run_id=now_id(), progress=_print_progress)
    finally:
        try:
            if ctx and ctx.get("store"):
                ctx["store"].close()
        except Exception:
            pass
        if args.keep_data:
            print("评测数据目录：%s" % data_dir)
        else:
            shutil.rmtree(data_dir, ignore_errors=True)

    run_path = save_run(run, args.runs_dir)
    baseline = report_mod.load_baseline(args.baseline)
    note = None
    if baseline is None or args.update_baseline:
        report_mod.write_baseline(args.baseline, run)
        note = "首次运行，已写入基线：%s" % args.baseline if baseline is None \
            else "已用本次结果更新基线：%s" % args.baseline
    cmp = report_mod.compare(baseline, run)
    print(report_mod.format_report(cmp, run, baseline, run_path, baseline_note=note))
    if baseline is not None and not args.update_baseline and cmp["regressions"]:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
