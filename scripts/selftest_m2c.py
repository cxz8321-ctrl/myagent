#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M2-c 自检：滚动摘要（不删原文、只推指针、幂等、窗口七成触发）+ L4 程序性记忆。
摘要器用假的（保证可复现），不调真模型。"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.events import EventBus                        # noqa: E402
from agent.loop import AgentLoop                         # noqa: E402
from context import Budget, ContextBuilder, PromptStore  # noqa: E402
from memory import MemoryStore, ProceduralStore          # noqa: E402
from runtime.config import load_config, project_root     # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print("  %s %s%s" % ("✓" if ok else "✗", name, ("  — " + detail) if detail else ""))


class FakeProvider:
    name, model = "fake", "fake"

    def chat(self, messages, tools=None, cfg=None):
        return {"ok": True, "v": 1,
                "data": {"message": {"role": "assistant", "content": "好的"},
                         "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                         "finish_reason": "stop"},
                "error": None, "meta": {}}


def fake_summarizer(prev, new_text):
    return ("旧摘要；" if prev else "") + "压缩了%d字" % len(new_text)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="myagent-m2c-")
    store = MemoryStore(db_path=os.path.join(tmp, "m2c.db"))
    store.summarizer = fake_summarizer
    sid = "s-compact"
    store.ensure_session(sid)
    for i in range(40):
        store.append(sid, {"role": "user" if i % 2 == 0 else "assistant",
                           "content": "第%d轮：%s" % (i, "讨论内容" * 20)})

    print("① 滚动摘要（保留最近 8 条，更早的压掉）")
    n_before = store.count_messages(sid)
    r1 = store.compact(sid, {"keep_last": 8})
    check("压了，且给出了覆盖指针", not r1.get("skipped") and r1["upto_msg_id"] > 0, str(r1)[:110])
    check("只压了除最近 8 条以外的部分", r1["compressed_count"] == 32, "条数=%s" % r1.get("compressed_count"))
    check("原文一条没删", store.count_messages(sid) == n_before, "before=%d now=%d" % (n_before, store.count_messages(sid)))
    check("摘要与指针落进 sessions", bool(store.session_summary(sid)["summary"])
          and store.session_summary(sid)["upto_msg_id"] == r1["upto_msg_id"])
    r2 = store.compact(sid, {"keep_last": 8})
    check("幂等：同一段不重复压", r2.get("skipped") and "压过" in (r2.get("reason") or ""), str(r2)[:60])

    print("② 摘要进了上下文（原文不再重复进）")
    cfg = load_config()
    prompts = PromptStore(os.path.join(project_root(), "config/prompts"))
    builder = ContextBuilder(budget=Budget(4096, 512), prompts=prompts, keep_last=3)
    from agent.state import AgentState
    st = AgentState(session_id=sid, trace_id="t1")
    info = store.session_summary(sid)
    st.messages = [m for m in store.recent(sid, n=8, opts={"after_id": info["upto_msg_id"]})]
    st.summary_block = info["summary"]
    st.procedural_block = "- 经验.md：装本地推理走 ModelScope（文件 /tmp/x）"
    built = builder.build(st)
    texts = "\n".join(str(m.get("content")) for m in built["messages"])
    check("摘要块在", "更早对话的摘要" in texts)
    check("经验块在", "经验文件" in texts)
    check("单元数变少（历史只剩最近几条）", built["stats"]["units_total"] <= 9,
          "units=%d" % built["stats"]["units_total"])

    print("③ 内核级触发（窗口用满七成就自动压）")
    bus = EventBus()
    events = []
    bus.subscribe(lambda e: events.append(e["type"]))
    loop = AgentLoop(provider=FakeProvider(), store=store,
                     context=ContextBuilder(budget=Budget(4096, 512), prompts=prompts, keep_last=3),
                     procedural=None, memory_cfg={"summarize": {"enabled": True,
                                                                "trigger_ratio": 0.7, "keep_last": 8}})
    loop.bus = bus
    sid2 = "s-auto"
    store.ensure_session(sid2)
    for i in range(40):
        store.append(sid2, {"role": "user", "content": "长历史第%d条 %s" % (i, "内容" * 40)})
    out = loop.run({"input": "你好", "session_id": sid2})
    check("自动压了一次", "compact" in events, "events=%s" % events)
    check("摘要写入会话", bool(store.session_summary(sid2)["summary"]))
    check("任务照常完成", out["ok"] and out["data"]["status"] == "done")

    print("④ L4 程序性记忆（经验文件 + 索引 + 注入）")
    pdir = os.path.join(tmp, "procedural")
    ps = ProceduralStore(pdir, store=store)
    ps.append_note("踩坑", "pip 被 PEP668 拦时用 conda 的 pip 灌 venv")
    ps.append_note("口径", "文档一律优先 docx 交付")
    items = ps.scan()
    check("文件被扫到并建了索引", len(items) == 2, ", ".join(i["title"] for i in items))
    idx_rows = store.conn.execute("SELECT COUNT(*) c FROM procedural_index").fetchone()["c"]
    check("索引表有记录", idx_rows == 2)
    old_hash = {i["title"]: i["mtime_hash"] for i in items}["踩坑"]
    ps.append_note("踩坑", "又补了一条新坑")
    items2 = {i["title"]: i for i in ps.scan()}
    check("文件改动能被发现（指纹变了）", items2["踩坑"]["mtime_hash"] != old_hash)
    block, paths = ps.inject_block()
    check("注入块带标题与摘要", "踩坑" in block and "pip 被 PEP668" in block, block.splitlines()[0][:60])
    check("给出文件路径便于自取", all(os.path.isfile(p) for p in paths))

    bad = [n for n, ok in RESULTS if not ok]
    print("\n结果：%d/%d 通过%s" % (len(RESULTS) - len(bad), len(RESULTS),
                                  ("  失败：" + ", ".join(bad)) if bad else ""))
    store.close()
    print("临时目录：%s（可删）" % tmp)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
