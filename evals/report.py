# -*- coding: utf-8 -*-
"""回归报告（M4）：与基线对比，标出退步项与进步项；首次运行自动写基线。

基线文件（evals/baseline.json）只存最小必要信息：每条任务的 score / passed / 失败原因。
对比口径：分数下降 = 退步；分数上升 = 进步；本次新增 / 基线有本次无 单独列出。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

EPS = 1e-6


def build_baseline(run: Dict[str, Any]) -> Dict[str, Any]:
    tasks = {}
    for t in run.get("tasks") or []:
        tasks[str(t.get("id"))] = {
            "score": float(t.get("score") or 0.0),
            "passed": bool(t.get("passed")),
            "status": t.get("status"),
            "fail_reasons": list(t.get("fail_reasons") or []),
        }
    return {
        "version": 1,
        "created_at": int(time.time() * 1000),
        "run_id": run.get("run_id"),
        "provider": run.get("provider"),
        "judge": bool(run.get("judge")),
        "summary": run.get("summary") or {},
        "tasks": tasks,
    }


def load_baseline(path: str) -> Optional[Dict[str, Any]]:
    if not path or not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else None


def write_baseline(path: str, run: Dict[str, Any]) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(build_baseline(run), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def compare(baseline: Optional[Dict[str, Any]], run: Dict[str, Any]) -> Dict[str, Any]:
    base_tasks = (baseline or {}).get("tasks") or {}
    cur = {str(t.get("id")): t for t in (run.get("tasks") or [])}
    regressions: List[Dict[str, Any]] = []
    improvements: List[Dict[str, Any]] = []
    unchanged: List[str] = []
    added: List[Dict[str, Any]] = []
    removed: List[Dict[str, Any]] = []

    for tid, t in cur.items():
        cs = float(t.get("score") or 0.0)
        if tid not in base_tasks:
            added.append({"id": tid, "score": cs, "tags": t.get("tags") or []})
            continue
        bs = float(base_tasks[tid].get("score") or 0.0)
        if cs < bs - EPS:
            regressions.append({"id": tid, "before": round(bs, 4), "after": round(cs, 4),
                                "delta": round(cs - bs, 4), "tags": t.get("tags") or [],
                                "fail_reasons": list(t.get("fail_reasons") or [])})
        elif cs > bs + EPS:
            improvements.append({"id": tid, "before": round(bs, 4), "after": round(cs, 4),
                                 "delta": round(cs - bs, 4), "tags": t.get("tags") or []})
        else:
            unchanged.append(tid)
    for tid in base_tasks:
        if tid not in cur:
            removed.append({"id": tid, "before": round(float(base_tasks[tid].get("score") or 0.0), 4)})

    return {"regressions": regressions, "improvements": improvements, "unchanged": unchanged,
            "added": added, "removed": removed,
            "has_baseline": baseline is not None}


def format_report(cmp: Dict[str, Any], run: Dict[str, Any],
                  baseline: Optional[Dict[str, Any]] = None,
                  run_path: Optional[str] = None,
                  baseline_note: Optional[str] = None) -> str:
    s = run.get("summary") or {}
    lines: List[str] = []
    lines.append("=" * 64)
    lines.append("M4 评测报告")
    lines.append("=" * 64)
    lines.append("provider=%s  judge=%s  任务=%s  通过=%s  均分=%.4f  总token=%s  用时=%.1fs"
                 % (run.get("provider"), "on" if run.get("judge") else "off",
                    s.get("total"), s.get("passed"), float(s.get("mean_score") or 0.0),
                    s.get("total_tokens"), (s.get("elapsed_ms") or 0) / 1000.0))
    if run_path:
        lines.append("轨迹：%s" % run_path)
    if baseline is not None:
        created = baseline.get("created_at")
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(created / 1000.0)) if created else "?"
        lines.append("基线：run_id=%s（%s，provider=%s）" % (baseline.get("run_id"), when,
                                                             baseline.get("provider")))
    else:
        lines.append("基线：无（本次为首轮）")
    if baseline_note:
        lines.append("→ %s" % baseline_note)
    lines.append("-" * 64)

    def _list(title: str, items: List[Dict[str, Any]], sign: str = "") -> None:
        lines.append("%s（%d）" % (title, len(items)))
        for it in items:
            if sign == "-":
                lines.append("  ↓ %s  %.4f → %.4f (%+.4f)  tags=%s"
                             % (it["id"], it["before"], it["after"], it["delta"], it.get("tags")))
                for r in (it.get("fail_reasons") or [])[:3]:
                    lines.append("      · %s" % r)
            elif sign == "+":
                lines.append("  ↑ %s  %.4f → %.4f (%+.4f)  tags=%s"
                             % (it["id"], it["before"], it["after"], it["delta"], it.get("tags")))
            elif sign == "removed":
                lines.append("  – %s  （本次未跑；基线 %.4f）" % (it["id"], it.get("before", 0.0)))
            else:
                lines.append("  · %s  score=%.4f" % (it["id"], it.get("score", 0.0)))

    if not cmp.get("has_baseline"):
        lines.append("没有基线：已按本次结果写入基线，之后每次运行都会对比出退步/进步。")
    else:
        _list("退步项", cmp["regressions"], "-")
        _list("进步项", cmp["improvements"], "+")
        _list("本次新增（基线里没有）", cmp["added"])
        partial = bool(cmp["removed"]) and len(run.get("tasks") or []) < len(
            (baseline or {}).get("tasks") or {})
        _list("基线有、本次没跑%s" % ("（部分运行，不算退步）" if partial else ""),
              cmp["removed"], "removed")
        lines.append("其余 %d 项与基线持平。" % len(cmp["unchanged"]))
    lines.append("-" * 64)
    failed = [t for t in (run.get("tasks") or []) if not t.get("passed")]
    lines.append("本次未满分（%d）：" % len(failed))
    for t in failed:
        lines.append("  ✗ %s  score=%.2f  %s" % (t.get("id"), t.get("score"),
                                                  "; ".join((t.get("fail_reasons") or [])[:2])))
    lines.append("=" * 64)
    return "\n".join(lines)


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
