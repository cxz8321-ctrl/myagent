# -*- coding: utf-8 -*-
"""打分器（M4）：规则判分（工具序列 / 约束 / 答案关键点）+ 可选模型判分（答案质量）。

每条任务产出：score（0~1，通过项/总项）、passed、checks、fail_reasons、judge。
规则判分不调模型，逐项可解释；模型判分默认关闭（省 token），由 runner --judge 打开。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence

JUDGE_PASS = 0.6          # 模型判分 >= 0.6 视为该项通过
JUDGE_SYSTEM = "你是一个严格的评测判分器。只输出一行 JSON，不要解释。"


def is_subsequence(expected: Sequence[str], actual: Sequence[str]) -> bool:
    """expected 是否按顺序出现在 actual 里（可跳过多余的工具调用）。"""
    it = iter(actual)
    return all(any(a == e for a in it) for e in expected)


def score_task(task: Dict[str, Any], result: Dict[str, Any],
               judge_provider: Any = None) -> Dict[str, Any]:
    expect = task.get("expect") or {}
    cons = task.get("constraints") or {}
    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: Any, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    status = str(result.get("status") or "")
    want_status = str(expect.get("status", "done"))
    add("status", status == want_status, "期望 %s，实际 %s" % (want_status, status))

    names = [str(x) for x in (result.get("tool_names") or [])]
    calls = list(result.get("tool_calls") or [])

    if expect.get("no_tools"):
        add("no_tools", not names, "实际调用了：%s" % (names or "无"))
    if expect.get("tools") is not None:
        want = [str(x) for x in expect.get("tools") or []]
        add("tool_sequence", is_subsequence(want, names),
            "期望序列 %s ⊑ 实际 %s" % (want, names))
    if expect.get("tools_any"):
        want = [str(x) for x in expect["tools_any"]]
        add("tools_any", any(w in names for w in want), "期望其一 %s，实际 %s" % (want, names))
    if expect.get("min_tools") is not None:
        n = int(expect["min_tools"])
        add("min_tools", len(names) >= n, "期望至少 %d 次工具调用，实际 %d 次" % (n, len(names)))
    for tool, code in (expect.get("tool_errors") or {}).items():
        hits = [c for c in calls if c.get("name") == tool and c.get("error") == code]
        seen = [c.get("error") for c in calls if c.get("name") == tool]
        add("tool_error:%s" % tool, bool(hits),
            "期望 %s 报 %s，实际 %s" % (tool, code, seen or "未调用"))
    for tool, tier in (expect.get("tool_tiers") or {}).items():
        hits = [c for c in calls if c.get("name") == tool and c.get("tier") == tier]
        add("tool_tier:%s" % tool, bool(hits), "期望 %s 的级别是 %s" % (tool, tier))

    answer = "" if result.get("answer") is None else str(result.get("answer"))
    if expect.get("answer_nonempty"):
        add("answer_nonempty", bool(answer.strip()), "答案为空")
    if expect.get("answer_any"):
        want = [str(x) for x in expect["answer_any"]]
        hit = [x for x in want if x.lower() in answer.lower()]
        add("answer_any", bool(hit), "期望命中其一 %s，答案：%s" % (want, _snip(answer)))
    if expect.get("answer_all"):
        want = [str(x) for x in expect["answer_all"]]
        miss = [x for x in want if x.lower() not in answer.lower()]
        add("answer_all", not miss, "缺少 %s，答案：%s" % (miss, _snip(answer)))
    if expect.get("answer_not_any"):
        bad = [str(x) for x in expect["answer_not_any"] if str(x).lower() in answer.lower()]
        add("answer_not_any", not bad, "答案里不该出现 %s" % bad)
    if expect.get("answer_regex"):
        add("answer_regex", bool(re.search(str(expect["answer_regex"]), answer, re.I)),
            "期望匹配 /%s/，答案：%s" % (expect["answer_regex"], _snip(answer)))

    if expect.get("memory_written"):
        add("memory_written", bool(result.get("memory_written")), "没有写入记忆")
    if expect.get("memory_hits"):
        add("memory_hits", int(result.get("memory_hits") or 0) > 0, "没有召回记忆")

    usage = result.get("usage") or {}
    if cons.get("max_steps") is not None:
        n = int(cons["max_steps"])
        add("max_steps", int(result.get("steps") or 0) <= n,
            "步数 %s 超过上限 %d" % (result.get("steps"), n))
    if cons.get("max_total_tokens") is not None:
        n = int(cons["max_total_tokens"])
        got = int(usage.get("total_tokens") or 0)
        add("max_total_tokens", got <= n, "token %d 超过上限 %d" % (got, n))
    if cons.get("max_latency_ms") is not None:
        n = int(cons["max_latency_ms"])
        add("max_latency_ms", int(result.get("latency_ms") or 0) <= n,
            "耗时 %sms 超过上限 %dms" % (result.get("latency_ms"), n))
    if cons.get("max_context_units") is not None:
        n = int(cons["max_context_units"])
        stats = result.get("context_stats") or {}
        add("max_context_units", int(stats.get("units_total") or 0) <= n,
            "上下文单元 %s 超过上限 %d" % (stats.get("units_total"), n))
    if cons.get("no_error"):
        err = result.get("error")
        add("no_error", status != "error" and not err, "出现错误：%s" % (err or status))

    judge_out = None
    if judge_provider is not None and task.get("judge"):
        judge_out = judge_answer(str(task["judge"]), str(task.get("input") or ""), answer, judge_provider)
        add("judge", judge_out["ok"], judge_out.get("reason", ""))

    total = len(checks)
    passed_n = sum(1 for c in checks if c["ok"])
    score = round(passed_n / total, 4) if total else 1.0
    fail_reasons = ["%s: %s" % (c["name"], c["detail"]) for c in checks if not c["ok"]]
    return {"score": score, "passed": not fail_reasons, "checks": checks,
            "fail_reasons": fail_reasons, "judge": judge_out}


def judge_answer(criteria: str, question: str, answer: str, provider: Any) -> Dict[str, Any]:
    """模型判分：按 criteria 给回答打 0~10，归一到 0~1。失败不拖垮评测（记 0 分 + 原因）。"""
    prompt = ("评分标准：%s\n\n用户问题：%s\n\n回答文本：%s\n\n"
              "请判断该回答是否满足评分标准，只输出 JSON："
              "{\"score\": 0到10的整数, \"reason\": \"一句话原因\"}" % (criteria, question, _snip(answer, 2000)))
    try:
        r = provider.chat([{"role": "system", "content": JUDGE_SYSTEM},
                           {"role": "user", "content": prompt}],
                          cfg={"temperature": 0, "max_tokens": 200})
        if not r.get("ok"):
            code = (r.get("error") or {}).get("code")
            return {"score": 0.0, "ok": False, "reason": "判分模型调用失败：%s" % code}
        text = (r["data"]["message"].get("content") or "").strip()
    except Exception as e:  # noqa: BLE001
        return {"score": 0.0, "ok": False, "reason": "判分异常：%s" % e}
    data = _parse_json_obj(text)
    try:
        raw = float(data.get("score"))
    except (TypeError, ValueError):
        raw = 0.0
    norm = max(0.0, min(1.0, raw / 10.0))
    return {"score": round(norm, 4), "ok": norm >= JUDGE_PASS,
            "reason": str(data.get("reason") or text)[:300]}


def _parse_json_obj(text: str) -> Dict[str, Any]:
    s = str(text or "").strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.M).strip()
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        s = s[i:j + 1]
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def _snip(text: Any, n: int = 160) -> str:
    s = str(text or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"
