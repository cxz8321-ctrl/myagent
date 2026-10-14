#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上下文工程自检（不调模型，秒级出结果）。

验收点（对齐规格书 3.5）：
  1. 任意长输入都不炸，且不超预算
  2. 系统提示与最近 N 轮永不被裁
  3. 超长工具结果先削再进上下文
  4. 工具往返单元不可拆（tool 消息不能丢下它的调用）
  5. 提示词外置：能读文件、能注变量、缺了会报、没了退回内置
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.state import AgentState                      # noqa: E402
from context import Budget, ContextBuilder, PromptStore  # noqa: E402
from context.prompts import DEFAULT_SYSTEM              # noqa: E402
from context.truncate import group_units, truncate_text  # noqa: E402
from runtime.config import load_config, project_root    # noqa: E402

RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print("  %s %s%s" % ("✓" if ok else "✗", name, ("  — " + detail) if detail else ""))


def main() -> int:
    cfg = load_config()
    c = cfg["context"]
    budget = Budget(c["total_tokens"], c["reserve_output"], c.get("weights"))
    prompts = PromptStore(os.path.join(project_root(), c.get("prompt_dir", "config/prompts")))
    builder = ContextBuilder(budget=budget, prompts=prompts, keep_last=3, max_tool_result_chars=1000)

    print("① 超长输入 + 混合历史：不炸、不超预算、最近 3 单元保住")
    st = AgentState(session_id="selftest", trace_id="t-selftest")
    st.messages = [{"role": "user", "content": "超长开场" + "A" * 40000}]
    for i in range(12):
        st.messages.append({"role": "assistant", "content": "答复%d：" % i + "B" * 3000})
        st.messages.append({"role": "user", "content": "问题%d：" % i + "C" * 3000})
    st.messages.append({"role": "assistant", "content": None,
                        "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {"path": "/x"}}]})
    st.messages.append({"role": "tool", "tool_call_id": "c1", "name": "read_file",
                        "content": "工具结果：" + "D" * 50000})
    built = builder.build(st, memory_block="记忆：用户偏好 docx 交付。", memory_ids=["m1", "m2"])
    msgs, stats = built["messages"], built["stats"]
    check("首条是 system", msgs[0].get("role") == "system")
    check("总用量不超可用预算",
          stats["used_tokens"] <= budget.available,
          "used=%d available=%d" % (stats["used_tokens"], budget.available))
    check("最近 3 单元仍在", any("问题11" in str(m.get("content")) for m in msgs)
          and any("答复11" in str(m.get("content")) for m in msgs)
          and any(m.get("role") == "tool" for m in msgs))
    check("最老的开场被丢弃", not any("超长开场" in str(m.get("content")) for m in msgs))
    check("记忆块被注入且记了 id",
          any("记忆片段" in str(m.get("content")) for m in msgs)
          and stats["injected_memory_ids"] == ["m1", "m2"])

    print("② 超长工具结果被削")
    tools_txt = [m["content"] for m in msgs if m.get("role") == "tool"]
    check("工具结果 ≤ 上限", all(len(t) <= 1200 for t in tools_txt),
          "最长=%d" % (max(len(t) for t in tools_txt) if tools_txt else 0))
    t, cut = truncate_text("x" * 10000, 100)
    check("掐头留尾函数正常", cut and "中略" in t and len(t) < 200)

    print("③ 工具往返不可拆")
    units = group_units(st.messages)
    paired = [u for u in units if any(m.get("role") == "tool" for m in u)]
    check("tool 与其调用在同一单元", len(paired) == 1 and len(paired[0]) == 2)

    print("④ 提示词外置")
    r = prompts.render("system", {"name": "测试助手", "creature": "AI", "style": "务实", "now": "t", "cwd": "/", "model": "m"})
    check("能读到文件模板", bool(r["version"]) and r["version"] != "builtin" and "测试助手" in r["text"],
          "version=%s path=%s" % (r["version"], r["path"]))
    r2 = prompts.render("system", {})
    check("缺变量会被报出来", "name" in r2["missing"])
    empty = PromptStore("/nonexistent-dir-xyz").render("system")
    check("文件没了退回内置默认", empty["version"] == "builtin" and empty["text"] == DEFAULT_SYSTEM)

    print("⑤ 预算分配表")
    for k, v in budget.to_dict()["quota"].items():
        print("      %-8s %d" % (k, v))

    bad = [n for n, ok, _ in RESULTS if not ok]
    print("\n结果：%d/%d 通过%s" % (len(RESULTS) - len(bad), len(RESULTS),
                                  ("  失败：" + ", ".join(bad)) if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
