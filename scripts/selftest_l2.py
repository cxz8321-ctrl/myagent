#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L2 语义记忆自检：语义召回 / 查重 / 冲突消解 / 作用域隔离 / 写入筛选 / 注入格式。

用真的 embedding 小模型（本地 GGUF）；判冲突的模型调用用假的 provider（保证可复现）。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import Embedder, MemoryStore, MemoryWriter, Retriever, VectorIndex  # noqa: E402
from runtime.config import load_config                                          # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print("  %s %s%s" % ("✓" if ok else "✗", name, ("  — " + detail) if detail else ""))


class MockJudge:
    """只会在"同一件事改口"时答 yes 的假模型。"""
    name, model = "mock", "mock"

    def chat(self, messages, tools=None, cfg=None):
        prompt = messages[-1].get("content") or ""
        yes = ("西安" in prompt and "北京" in prompt)
        return {"ok": True, "v": 1, "data": {"message": {"role": "assistant",
                                                         "content": "yes" if yes else "no"},
                                             "usage": {}, "finish_reason": "stop"},
                "error": None, "meta": {}}


def main() -> int:
    cfg = load_config()
    mcfg = cfg.get("memory") or {}
    emb_cfg = mcfg.get("embed") or {}
    emb = Embedder(model_path=emb_cfg.get("model_path", "~/.local/models/Qwen3-Embedding-0.6B-Q8_0.gguf"),
                   model_name=emb_cfg.get("model_name"))
    if not emb.available():
        print("✗ embedding 模型还没下好：%s" % emb.model_path)
        return 1

    tmp = tempfile.mkdtemp(prefix="myagent-l2-")
    store = MemoryStore(db_path=os.path.join(tmp, "l2.db"))
    index = VectorIndex(store, emb)
    rcfg = mcfg.get("retrieve") or {}
    retriever = Retriever(store, embedder=emb, index=index, weights=rcfg.get("weights"),
                          min_score=float(rcfg.get("min_score", 0.15)))
    store.retriever = retriever
    writer = MemoryWriter(store, index=index, embedder=emb, retriever=retriever,
                          model_judge=True, judge_provider=MockJudge(), scope="global")

    print("库：%s" % store.path)
    print("① 写三条记忆 + 建向量")
    for it in [{"content": "用户偏好 docx 交付格式", "type": "preference", "importance": 0.8},
               {"content": "用户住在西安", "type": "fact", "importance": 0.7},
               {"content": "用户讨厌用表格汇报", "type": "preference", "importance": 0.6}]:
        writer.write(it)
    n_vec = store.conn.execute("SELECT COUNT(*) c FROM memory_vectors").fetchone()["c"]
    check("三条都落库", len(store.memories_active()) == 3)
    check("向量表也写了", n_vec == 3, "vectors=%d" % n_vec)

    print("② 语义召回（换个说法也能想起来）")
    hits = retriever.search("交付文档用什么格式比较好？", k=3)
    check("换措辞能召回「偏好 docx」",
          bool(hits) and "docx" in hits[0]["content"], hikes(hits))
    check("分数分解齐全", set(hits[0]["score_breakdown"]) == {"vector", "keyword", "recency", "importance"},
          str(hits[0]["score_breakdown"]))
    hits2 = retriever.search("我不喜欢什么形式的汇报", k=3)
    check("另一条也能召回", bool(hits2) and "表格" in hits2[0]["content"], hikes(hits2))

    print("③ 冲突消解（改口后旧的别再被召回）")
    old = store.conn.execute("SELECT id FROM memories WHERE content LIKE '%西安%'").fetchone()["id"]
    res = writer.write({"content": "用户已经搬到北京住了", "type": "fact", "importance": 0.8})
    check("新条落库并置旧条 superseded",
          res["action"] == "created" and res["superseded_id"] == old,
          "superseded=%s" % res["superseded_id"])
    check("旧条状态被改", store.memory_get(old)["status"] == "superseded")
    check("版本链可回溯", len(store.memory_history(res["id"])) == 2)
    hits3 = retriever.search("用户现在住在哪个城市", k=3)
    check("召回的是新地址不是旧地址",
          bool(hits3) and "北京" in hits3[0]["content"] and
          all("西安" not in h["content"] for h in hits3), hikes(hits3))

    print("④ 查重（换个写法别又记一遍）")
    before = len(store.memories_active())
    r = writer.write({"content": "用户偏好docx交付格式。", "type": "preference"})
    check("归一化后判重", r["action"] == "merged" and len(store.memories_active()) == before,
          r["action"])

    print("⑤ 作用域隔离")
    writer2 = MemoryWriter(store, index=index, retriever=retriever, scope="project:miao")
    writer2.write({"content": "喵项目的接口约定是 snake_case", "type": "decision"})
    a = retriever.search("接口约定", k=3, scope="project:miao")
    b = retriever.search("接口约定", k=3, scope="global")
    check("A 项目口径能查到", bool(a) and "snake_case" in a[0]["content"])
    check("不污染 global", all("snake_case" not in h["content"] for h in b))

    print("⑥ 写入筛选（闲聊不写）")
    for txt, expect in [("哈哈今天天气不错啊", False), ("以后文档都给我 docx", True),
                        ("记住：我下周要去上海", True), ("嗯嗯好的", False)]:
        g = writer.should_write(txt)
        check("「%s」→ %s" % (txt[:12], "写" if g["write"] else "不写"), g["write"] == expect,
              g.get("reason", ""))

    print("⑦ 注入格式（带来源与时间）")
    block, ids = retriever.format_block(retriever.search("交付格式", k=2))
    check("块里有类型与时间标注", block.startswith("- [") and "·" in block, block.splitlines()[0][:40])
    check("id 对得上", len(ids) == len(block.splitlines()))
    check("恰好只注入命中的片段", all(i in [m["id"] for m in store.memories_active()] for i in ids))

    bad = [n for n, ok in RESULTS if not ok]
    print("\n结果：%d/%d 通过%s" % (len(RESULTS) - len(bad), len(RESULTS),
                                  ("  失败：" + ", ".join(bad)) if bad else ""))
    store.close()
    print("临时库：%s（可删）" % tmp)
    return 1 if bad else 0


def hikes(hits, n=3):
    return " | ".join("%.3f:%s" % (h["score"], h["content"][:14]) for h in hits[:n])


if __name__ == "__main__":
    raise SystemExit(main())
