# -*- coding: utf-8 -*-
"""记忆门面：四层记忆的统一入口。

铁律（规格书 B.1 / B.10）：
  - 时间统一 INTEGER 毫秒 UTC
  - 消息表 append-only，只插不改
  - 一切软删除（改 status），不物理删
  - 大内容（>8KB）走 blobs 旁路，主表只存 blob:<id>
  - schema 能演进：PRAGMA user_version + migrations/
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

INLINE_LIMIT = 8 * 1024


def now_ms() -> int:
    return int(time.time() * 1000)


class MemoryStore:
    def __init__(self, db_path: Optional[str] = None, data_dir: Optional[str] = None,
                 db_file: str = "myagent.db", inline_limit: int = INLINE_LIMIT, logger=None):
        base = os.path.expanduser(data_dir or "data")
        self.path = os.path.expanduser(db_path) if db_path else os.path.join(base, db_file)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.inline_limit = int(inline_limit)
        self.logger = logger
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.schema_version = self._migrate()

    # ================= 建表与演进 =================
    def _migrate(self) -> int:
        cur = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
        files = sorted(f for f in os.listdir(d) if f.endswith(".sql"))
        for i, fn in enumerate(files, start=1):
            if i <= cur:
                continue
            with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                self.conn.executescript(f.read())
            self.conn.execute("PRAGMA user_version=%d" % i)
            self.conn.commit()
            cur = i
        return cur

    def close(self) -> None:
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:
            pass

    # ================= L1 会话层 =================
    def ensure_session(self, session_id: str, user_id: str = "default",
                       channel: str = "cli", title: Optional[str] = None) -> str:
        t = now_ms()
        row = self.conn.execute("SELECT id FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row:
            return session_id
        self.conn.execute(
            "INSERT INTO sessions(id,user_id,channel,title,created_at,updated_at,last_message_at,"
            "message_count,status,meta) VALUES(?,?,?,?,?,?,?,0,'active','{}')",
            (session_id, user_id, channel, title or "", t, t, t))
        self.conn.commit()
        return session_id

    def append(self, session_id: str, message: Dict[str, Any],
               client_msg_id: Optional[str] = None, trace_id: Optional[str] = None) -> Dict[str, Any]:
        """追加一条消息（append-only，幂等：同 client_msg_id 不重复插）。"""
        self.ensure_session(session_id)
        if client_msg_id:
            row = self.conn.execute(
                "SELECT id FROM messages WHERE session_id=? AND client_msg_id=?",
                (session_id, client_msg_id)).fetchone()
            if row:
                return {"id": row["id"], "duplicated": True}

        content = message.get("content")
        if isinstance(content, str) and len(content.encode("utf-8")) > self.inline_limit:
            bid = self.put_blob(content)
            content = "blob:%s" % bid
        tool_calls = message.get("tool_calls")
        t = now_ms()
        cur = self.conn.execute(
            "INSERT INTO messages(session_id,role,content,tool_calls,tool_call_id,name,"
            "token_count,created_at,trace_id,client_msg_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (session_id, message.get("role"), content,
             json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
             message.get("tool_call_id"), message.get("name"),
             message.get("token_count"), t, trace_id, client_msg_id))
        self.conn.execute(
            "UPDATE sessions SET updated_at=?, last_message_at=?, message_count=message_count+1 WHERE id=?",
            (t, t, session_id))
        self.conn.commit()
        return {"id": cur.lastrowid, "duplicated": False}

    def recent(self, session_id: str, n: int = 10, opts: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """按时间正序取最近 n 条（不含已被摘要覆盖的部分可选）。"""
        opts = opts or {}
        where, args = ["session_id=?", "status='active'"], [session_id]
        if opts.get("after_id"):
            where.append("id>?"); args.append(int(opts["after_id"]))
        if opts.get("before_id"):
            where.append("id<?"); args.append(int(opts["before_id"]))
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE %s ORDER BY id DESC LIMIT ?" % " AND ".join(where),
            (*args, int(n))).fetchall()
        return [self._to_message(r) for r in reversed(rows)]

    def count_messages(self, session_id: str) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE session_id=? AND status='active'",
            (session_id,)).fetchone()["c"])

    def delete_messages(self, ids: List[int], reason: str = "") -> int:
        """软删除消息（改 status，不物理删）。返回实际改动条数。"""
        if not ids:
            return 0
        n = 0
        for i in ids:
            cur = self.conn.execute("UPDATE messages SET status='deleted' WHERE id=? AND status!='deleted'",
                                    (int(i),))
            n += cur.rowcount or 0
        self.conn.commit()
        try:
            self.audit("user", "message", "soft_delete", ",".join(str(i) for i in ids),
                       {"reason": reason}, approved=True)
        except Exception:
            pass
        return n

    def list_sessions(self, status: str = "active", limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id,user_id,channel,title,message_count,summary_upto_msg_id,last_message_at,status "
            "FROM sessions WHERE status=? ORDER BY last_message_at DESC LIMIT ?", (status, limit)).fetchall()
        return [dict(r) for r in rows]

    def session_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        r = self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(r) if r else None

    @staticmethod
    def _to_message(r: sqlite3.Row) -> Dict[str, Any]:
        m: Dict[str, Any] = {"role": r["role"], "content": r["content"]}
        if r["tool_calls"]:
            try:
                m["tool_calls"] = json.loads(r["tool_calls"])
            except ValueError:
                pass
        for k in ("tool_call_id", "name"):
            if r[k]:
                m[k] = r[k]
        m["_id"] = r["id"]
        m["_ts"] = r["created_at"]
        return m

    # ================= blobs 旁路 =================
    def put_blob(self, content: str) -> str:
        h = hashlib.sha256(content.encode("utf-8")).hexdigest()
        row = self.conn.execute("SELECT id FROM blobs WHERE sha256=?", (h,)).fetchone()
        if row:
            return row["id"]
        bid = h[:16]
        self.conn.execute("INSERT OR REPLACE INTO blobs(id,sha256,content,size,created_at) VALUES(?,?,?,?,?)",
                          (bid, h, content, len(content.encode("utf-8")), now_ms()))
        self.conn.commit()
        return bid

    def get_blob(self, blob_id: str) -> Optional[str]:
        row = self.conn.execute("SELECT content FROM blobs WHERE id=?", (blob_id,)).fetchone()
        return row["content"] if row else None

    def resolve_content(self, content: Optional[str]) -> Optional[str]:
        if isinstance(content, str) and content.startswith("blob:"):
            return self.get_blob(content[5:])
        return content

    # ================= L3 档案层 =================
    def profile_set(self, user_id: str, key: str, value: Any, value_type: Optional[str] = None,
                    source: str = "user_stated", confidence: float = 1.0) -> None:
        vt = value_type or ("json" if isinstance(value, (dict, list)) else
                            "bool" if isinstance(value, bool) else
                            "number" if isinstance(value, (int, float)) else "string")
        self.conn.execute(
            "INSERT INTO profile(user_id,key,value,value_type,confidence,source,updated_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET "
            "value=excluded.value, value_type=excluded.value_type, confidence=excluded.confidence, "
            "source=excluded.source, updated_at=excluded.updated_at",
            (user_id, key, json.dumps(value, ensure_ascii=False) if vt == "json" else str(value),
             vt, float(confidence), source, now_ms()))
        self.conn.commit()

    def profile_all(self, user_id: str = "default") -> Dict[str, Any]:
        rows = self.conn.execute("SELECT * FROM profile WHERE user_id=?", (user_id,)).fetchall()
        out: Dict[str, Any] = {}
        for r in rows:
            v = r["value"]
            if r["value_type"] == "json":
                try:
                    v = json.loads(v)
                except ValueError:
                    pass
            elif r["value_type"] == "number":
                v = float(v)
            elif r["value_type"] == "bool":
                v = str(v).lower() in ("true", "1", "yes")
            out[r["key"]] = v
        return out

    def profile_get(self, user_id: str, key: str, default: Any = None) -> Any:
        return self.profile_all(user_id).get(key, default)

    # ================= 审计 =================
    def audit(self, actor: str, target_type: str, action: str, target_id: str = "",
              detail: Optional[Dict[str, Any]] = None, approved: bool = False) -> int:
        cur = self.conn.execute(
            "INSERT INTO audit_log(ts,actor,target_type,action,target_id,detail,approved) VALUES(?,?,?,?,?,?,?)",
            (now_ms(), actor, target_type, action, str(target_id),
             json.dumps(detail, ensure_ascii=False) if detail else None, 1 if approved else 0))
        self.conn.commit()
        return cur.lastrowid

    def audit_recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ================= L2 语义记忆 =================
    def memory_touch(self, ids: List[str]) -> int:
        if not ids:
            return 0
        now = now_ms()
        q = ("UPDATE memories SET last_accessed_at=?, access_count=access_count+1 "
             "WHERE id IN (%s)" % ",".join("?" * len(ids)))
        cur = self.conn.execute(q, (now, *ids))
        self.conn.commit()
        return cur.rowcount

    def memory_get(self, memory_id: str) -> Optional[Dict[str, Any]]:
        r = self.conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return dict(r) if r else None

    def memories_active(self, user_id: str = "default", scope: Optional[str] = None,
                        types: Optional[List[str]] = None, limit: int = 200) -> List[Dict[str, Any]]:
        q = "SELECT * FROM memories WHERE status='active'"
        args: List[Any] = []
        if user_id:
            q += " AND user_id=?"; args.append(user_id)
        if scope:
            q += " AND scope=?"; args.append(scope)
        if types:
            q += " AND type IN (%s)" % ",".join("?" * len(types)); args.extend(types)
        q += " ORDER BY created_at DESC LIMIT ?"; args.append(int(limit))
        return [dict(r) for r in self.conn.execute(q, tuple(args)).fetchall()]

    def memory_history(self, memory_id: str) -> List[Dict[str, Any]]:
        """版本链：给链条里任意一条，返回整条链（从旧到新），回答"以前是怎么说的"。"""
        cur_id = memory_id
        for _ in range(20):                      # 先往回找到最早的版本
            row = self.conn.execute(
                "SELECT id FROM memories WHERE superseded_by=? AND superseded_by IS NOT NULL",
                (cur_id,)).fetchone()
            if not row:
                break
            cur_id = row["id"]
        out: List[Dict[str, Any]] = []
        for _ in range(50):                      # 再从旧到新走一遍
            r = self.memory_get(cur_id) if cur_id else None
            if not r:
                break
            out.append({"id": r["id"], "content": r["content"], "status": r["status"],
                        "updated_at": r["updated_at"]})
            cur_id = r.get("superseded_by")
        return out

    # ---- 规格书 A.1 里的门面方法 ----
    def search(self, query: str, k: int = 5, **kw: Any) -> List[Dict[str, Any]]:
        """四层记忆统一门面：L2 语义检索（由 Retriever 实现，没接就返回空）。"""
        r = getattr(self, "retriever", None)
        if r is None:
            return []
        return r.search(query, k=k, **kw)

    def compact(self, session_id: str, opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """滚动摘要：保留最近 N 条原文，更早的压成摘要；原文不删，只推指针（幂等）。

        规矩（规格书 B.10-3）：摘要不进 messages，用 sessions.summary + summary_upto_msg_id。
        """
        opts = opts or {}
        keep_last = int(opts.get("keep_last", 8))
        summarizer = opts.get("summarizer") or getattr(self, "summarizer", None)
        info = self.session_info(session_id)
        if not info:
            return {"skipped": True, "reason": "会话不存在"}
        rows = self.conn.execute(
            "SELECT id, role, content FROM messages WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        if len(rows) <= keep_last:
            return {"skipped": True, "reason": "消息太少（%d 条）" % len(rows)}
        upto = int(rows[-keep_last - 1]["id"])
        prev_upto = int(info.get("summary_upto_msg_id") or 0)
        if upto <= prev_upto:
            return {"skipped": True, "reason": "这段已经压过了", "summary_upto_msg_id": prev_upto}
        if summarizer is None:
            return {"skipped": True, "reason": "没有摘要器（summarizer 未注入）"}

        part = [r for r in rows if prev_upto < int(r["id"]) <= upto]
        text = "\n".join("%s: %s" % (r["role"], (r["content"] or "")[:800]) for r in part)
        tokens_before = sum(len((r["content"] or "")) for r in part) // 2
        prev = info.get("summary") or ""
        summary = summarizer(prev, text)
        if not summary:
            return {"skipped": True, "reason": "摘要器返回空"}
        t = now_ms()
        self.conn.execute("UPDATE sessions SET summary=?, summary_upto_msg_id=?, updated_at=? WHERE id=?",
                          (summary, upto, t, session_id))
        self.conn.commit()
        self.audit("agent", "memory", "compact", session_id,
                   {"upto_msg_id": upto, "compressed": len(part)}, approved=True)
        return {"skipped": False, "summary": summary, "upto_msg_id": upto,
                "compressed_count": len(part), "tokens_before": tokens_before,
                "tokens_after": len(summary) // 2}

    def session_summary(self, session_id: str) -> Dict[str, Any]:
        info = self.session_info(session_id) or {}
        return {"summary": info.get("summary") or "", "upto_msg_id": int(info.get("summary_upto_msg_id") or 0)}

    def new_session_id(self, prefix: str = "s") -> str:
        return "%s%s" % (prefix, uuid.uuid4().hex[:12])

    # ================= 入口会话路由（M7 · 规格书 3.8） =================
    @staticmethod
    def route_key(channel: str, user_id: str, scope: str = "user") -> str:
        """路由键：scope=user 时同一用户跨入口共用一条会话；scope=channel 时按渠道分开。"""
        if str(scope) == "channel":
            return "%s:%s" % (channel or "default", user_id or "default")
        return "user:%s" % (user_id or "default")

    def route_session(self, channel: str = "cli", user_id: str = "default",
                      scope: str = "user", prefix: str = "s",
                      title: Optional[str] = None) -> str:
        """按 (channel,user_id,scope) 找会话；没有就建一条并落盘映射。返回 session_id。"""
        key = self.route_key(channel, user_id, scope)
        t = now_ms()
        row = self.conn.execute("SELECT session_id FROM channel_sessions WHERE scope_key=?",
                                (key,)).fetchone()
        if row:
            sid = row["session_id"]
            self.ensure_session(sid, user_id=user_id, channel=channel, title=title)
            self.conn.execute("UPDATE channel_sessions SET updated_at=? WHERE scope_key=?", (t, key))
            self.conn.commit()
            return sid
        sid = self.new_session_id(prefix)
        self.ensure_session(sid, user_id=user_id, channel=channel, title=title)
        self.conn.execute(
            "INSERT INTO channel_sessions(scope_key,channel,user_id,session_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(scope_key) DO UPDATE SET session_id=excluded.session_id, "
            "channel=excluded.channel, user_id=excluded.user_id, updated_at=excluded.updated_at",
            (key, channel, user_id, sid, t, t))
        self.conn.commit()
        return sid

    def session_for_route(self, channel: str = "cli", user_id: str = "default",
                          scope: str = "user") -> Optional[str]:
        row = self.conn.execute("SELECT session_id FROM channel_sessions WHERE scope_key=?",
                                (self.route_key(channel, user_id, scope),)).fetchone()
        return row["session_id"] if row else None

    def list_routes(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT scope_key,channel,user_id,session_id,updated_at FROM channel_sessions "
            "ORDER BY updated_at DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]
