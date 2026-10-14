#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆地基自检（L1 会话 + blobs 旁路 + L3 档案 + 审计 + 重开持久化）。"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import MemoryStore  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print("  %s %s%s" % ("✓" if ok else "✗", name, ("  — " + detail) if detail else ""))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="myagent-mem-")
    db = os.path.join(tmp, "test.db")
    s = MemoryStore(db_path=db)
    print("库：%s  schema_version=%d" % (db, s.schema_version))
    check("建库 + 迁移到最新 schema（>=v1）", s.schema_version >= 1)

    print("① L1 会话：追加 / 幂等 / 计数")
    sid = s.new_session_id()
    s.ensure_session(sid, user_id="zhe", channel="cli", title="自检")
    s.append(sid, {"role": "user", "content": "早上好"}, client_msg_id="c1")
    s.append(sid, {"role": "assistant", "content": "早"}, client_msg_id="c2")
    s.append(sid, {"role": "user", "content": "今天做啥"}, client_msg_id="c3")
    check("三条都在", s.count_messages(sid) == 3)
    r = s.append(sid, {"role": "user", "content": "早上好"}, client_msg_id="c1")
    check("同 client_msg_id 幂等", r["duplicated"] and s.count_messages(sid) == 3)

    print("② 大内容走 blobs 旁路")
    big = "长文" + "x" * 20000
    s.append(sid, {"role": "tool", "content": big, "name": "read_file"}, client_msg_id="c4")
    raw = s.conn.execute("SELECT content FROM messages WHERE client_msg_id='c4'").fetchone()["content"]
    check("主表只存引用", raw.startswith("blob:"), raw[:24])
    check("旁路能还原原文", s.resolve_content(raw) == big)

    print("③ recent 顺序与条数")
    rec = s.recent(sid, n=2)
    check("取最近 2 条且正序", [m["content"] for m in rec][:1] == ["今天做啥"], str(len(rec)))

    print("④ 工具调用往返")
    s.append(sid, {"role": "assistant", "content": None,
                   "tool_calls": [{"id": "t1", "name": "calc", "arguments": {"expr": "1+1"}}]})
    last = s.recent(sid, n=1)[0]
    check("tool_calls 能存能取", last.get("tool_calls", [{}])[0].get("name") == "calc")

    print("⑤ L3 档案（多类型）")
    s.profile_set("user", "name", "用户")
    s.profile_set("zhe", "偏好.doc_format", "docx")
    s.profile_set("zhe", "在做的项目", {"名字": "myagent", "阶段": "M2"})
    s.profile_set("zhe", "开发模式", True)
    p = s.profile_all("zhe")
    check("档案读写正确", p.get("name") == "用户" and p.get("偏好.doc_format") == "docx"
          and isinstance(p.get("在做的项目"), dict) and p.get("开发模式") is True, str(p))

    print("⑥ 审计")
    s.audit("agent", "memory", "create", "m-1", {"why": "自检"}, approved=True)
    s.audit("agent", "tool", "deny", "shell", {"reason": "危险操作"})
    au = s.audit_recent(5)
    check("审计可回溯", len(au) >= 2 and au[0]["action"] == "deny" and au[0]["approved"] == 0)

    print("⑦ 软删除")
    s.conn.execute("UPDATE sessions SET status='archived' WHERE id=?", (sid,))
    s.conn.commit()
    check("归档后不出现在活跃列表",
          all(x["id"] != sid for x in s.list_sessions()))
    check("但记录还在", s.session_info(sid)["status"] == "archived")

    print("⑧ 关掉重开：数据不丢")
    n_before = s.count_messages(sid)
    s.close()
    s2 = MemoryStore(db_path=db)
    check("重开后消息还在", s2.count_messages(sid) == n_before)
    check("重开后档案还在", s2.profile_get("user", "name") == "用户")
    s2.close()

    bad = [n for n, ok in RESULTS if not ok]
    print("\n结果：%d/%d 通过%s" % (len(RESULTS) - len(bad), len(RESULTS),
                                  ("  失败：" + ", ".join(bad)) if bad else ""))
    print("临时库：%s（可删）" % tmp)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
