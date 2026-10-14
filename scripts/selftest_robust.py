#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""脏输入免疫自检（2026-09-28 修的那个 bug）。

背景：Windows 终端/输入法/剪贴板会送进**孤立代理项**（半个 emoji），
导致：sqlite 落盘失败 + 向量检索退化 + trace 写不进 + 模型 API 400 整轮死。
本脚本验证三件事：
  1. 清洗函数本身对（脏的洗掉、干净的无损）；
  2. 单点（trace / 日志 / 消息组装 / store）遇到脏字符都不炸；
  3. **端到端**：脏输入进来，发给模型的消息必须是干净的、且整轮能正常跑完。

跑法：.venv/bin/python scripts/selftest_robust.py          # 不调外部模型
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.loop import AgentLoop                       # noqa: E402
from context.budget import Budget                      # noqa: E402
from context.builder import ContextBuilder             # noqa: E402
from context.prompts import PromptStore                # noqa: E402
from memory.store import MemoryStore                   # noqa: E402
from providers.base import envelope, err               # noqa: E402
from runtime.log import JsonFormatter                  # noqa: E402
from runtime.text import (safe_dumps, sanitize,        # noqa: E402
                          sanitize_deep, sanitize_messages)
from runtime.trace import TraceWriter                  # noqa: E402
from skills.registry import SkillMeta, ToolRegistry    # noqa: E402

BAD = "\ud83d"          # 孤立代理项（半个 emoji）
TMP = tempfile.mkdtemp(prefix="robust-")
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("✓" if cond else "✗", name, ("  — " + str(extra)) if (extra and not cond) else ""))


def has_surrogate(s):
    return any(0xD800 <= ord(c) <= 0xDFFF for c in str(s or ""))


# ================= 1. 清洗函数 =================
print("\n[1] 清洗函数")
check("孤立代理项被丢掉", not has_surrogate(sanitize("看看" + BAD + "负载")) and "负载" in sanitize("看看" + BAD + "负载"))
check("完整 emoji 无损保留", sanitize("好耶🎉") == "好耶🎉")
check("普通文本不动", sanitize("普通文本 123") == "普通文本 123")
check("非字符串原样返回", sanitize(123) == 123 and sanitize(None) is None)
check("空串安全", sanitize("") == "")
check("嵌套结构递归清洗", not has_surrogate(json.dumps(sanitize_deep({"a": ["x" + BAD], "b": {"c": "y" + BAD}}), ensure_ascii=True)))
check("消息清洗覆盖 content/tool_calls/name",
      not has_surrogate(json.dumps(sanitize_messages(
          [{"role": "assistant", "content": "a" + BAD,
            "name": "n" + BAD,
            "tool_calls": [{"function": {"arguments": '{"x":"' + BAD + '"'}}]}]), ensure_ascii=True)))
check("safe_dumps 结果一定可 UTF-8 编码", isinstance(safe_dumps({"x": "a" + BAD}).encode("utf-8"), bytes))

# ================= 2. 单点不炸 =================
print("\n[2] 单点（trace / 日志 / 错误信封 / store）")
tw = TraceWriter(trace_id="dirty", dir_path=os.path.join(TMP, "traces"))
rec = tw.write({"type": "step", "note": "脏" + BAD + "字"})
ok_file = False
if tw.path and os.path.isfile(tw.path):
    with open(tw.path, encoding="utf-8") as f:
        ok_file = bool(f.read())
check("trace 写脏字符不炸、且文件可读", rec is not None and ok_file)

fmt = JsonFormatter()
lr = logging.LogRecord("t", logging.INFO, __file__, 1, "msg" + BAD, None, None)
rendered = None
try:
    rendered = fmt.format(lr)
except Exception as e:  # noqa: BLE001
    rendered = "RAISED:%s" % e
check("日志格式化对脏字符免疫", isinstance(rendered, str) and not rendered.startswith("RAISED") and bool(rendered.encode("utf-8")))

e1 = err("MODEL_BAD_REQUEST", "请求非法 " + BAD, detail="lone surrogate " + BAD)
env = envelope(False, error=e1)
check("错误信封里的脏字符被洗干净", not has_surrogate(json.dumps(env, ensure_ascii=True)))

st = MemoryStore(data_dir=os.path.join(TMP, "db"), db_file="t.db")
sid = st.new_session_id()
st.ensure_session(sid)
try:
    st.append(sid, {"role": "user", "content": sanitize("问题" + BAD)})
    stored = st.recent(sid, n=5)
    store_ok = len(stored) == 1 and not has_surrogate(stored[0]["content"])
except Exception as e:  # noqa: BLE001
    store_ok = False
check("清洗后的内容能正常落盘", store_ok)

# ================= 3. 组装器 =================
print("\n[3] 组装器（发给模型的消息必须干净）")
prompts = PromptStore(os.path.join(ROOT, "config", "prompts"))
builder = ContextBuilder(budget=Budget(4096, 512), prompts=prompts, keep_last=4)


class _State:
    def __init__(self, msgs):
        self.messages = msgs
        self.memory_block = "记忆" + BAD
        self.memory_ids = []
        self.summary_block = "摘要" + BAD
        self.procedural_block = "经验" + BAD


built = builder.build(_State([{"role": "user", "content": "历史" + BAD},
                              {"role": "assistant", "content": "回复" + BAD}]))
check("组装出来的 messages 里没有代理项",
      not has_surrogate(json.dumps(built["messages"], ensure_ascii=True)))

built2 = builder.build(_State([{"role": "tool", "tool_call_id": "orphan", "content": "孤儿工具结果"},
                              {"role": "user", "content": "在吗"}]))
check("孤儿 tool 消息被丢掉（否则厂商 API 会 400）",
      not any(m.get("role") == "tool" for m in built2["messages"]),
      built2["messages"])
built3 = builder.build(_State([
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "calc", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c1", "name": "calc", "content": "1"}]))
check("有前置 tool_calls 的 tool 消息要保留",
      any(m.get("role") == "tool" for m in built3["messages"]), built3["messages"])

# ================= 4. 端到端：脏输入整轮跑通 =================
print("\n[4] 端到端（脏输入 → 发给模型的必须是干净消息）")
seen = {"dirty": 0, "calls": 0}


class SpyProvider:
    name = "spy"
    model = "spy"

    def chat(self, messages, tools=None, cfg=None):
        seen["calls"] += 1
        for m in messages:
            if has_surrogate(json.dumps(m, ensure_ascii=True, default=str)):
                seen["dirty"] += 1
        return envelope(True, data={"message": {"role": "assistant", "content": "收到" + BAD},
                                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
                        meta={})


store2 = MemoryStore(data_dir=os.path.join(TMP, "db2"), db_file="t2.db")
tools = ToolRegistry()
tools.register(SkillMeta(name="noop", description="无副作用", permission="read",
                         handler=lambda a: "ok"))
loop = AgentLoop(provider=SpyProvider(), tools=tools, context=builder, max_steps=3,
                 store=store2, session_id="dirty-e2e", retriever=None, writer=None,
                 procedural=None, prompt_vars={"name": "测试助手"}, trace_enabled=False,
                 memory_cfg={"summarize": {"enabled": False}})
res = loop.run({"input": "我是脏输入" + BAD + "结尾"})
d = res.get("data") or {}
check("脏输入整轮跑完不崩", res.get("ok") and d.get("status") == "done", res.get("error"))
check("发给模型的消息里零代理项", seen["dirty"] == 0 and seen["calls"] >= 1, seen)
check("脏输入也能正常落盘（消息条数>0）", store2.count_messages("dirty-e2e") > 0)
check("落盘内容里零代理项",
      not any(has_surrogate(m.get("content")) for m in store2.recent("dirty-e2e", n=20)))

# ================= 汇总 =================
print("\n" + "=" * 52)
print("脏输入免疫自检：%d/%d 通过" % (len(PASS), len(PASS) + len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - %s" % f)
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
