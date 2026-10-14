# -*- coding: utf-8 -*-
"""写入筛选与落库：什么值得记（规则先筛 + 模型兜底）、怎么写（查重 / 冲突消解 / 版本链）。

口径（规格书 4.2 / B.8）：
  - 四类白名单：偏好 / 事实 / 决策 / 待办；四类之外默认不写，宁可漏写不可滥写
  - 闲聊、寒暄、过程性对话一律不写
  - 归一化 → 算 hash → 查重 → 冲突处理 → 写主表与向量表 → 写审计
  - 归一化去空白/标点/大小写后再 hash（防"同一句话换个写法又记一遍"）
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Dict, List, Optional

from runtime.text import sanitize

WRITE_TYPES = ("preference", "fact", "decision", "todo")

# 规则信号：命中才考虑写（宁少勿多）。正则版更耐写法变化。
_SIGNALS = {
    "preference": [r"(喜欢|偏好|习惯|讨厌|不喜欢|别用|不要用)", r"以后.{0,8}(都|统一|一律|尽量)"],
    "fact": [r"我(叫|是|住|在|养|有)", r"我的.{0,8}是", r"生日是", r"账号"],
    "decision": [r"(决定|定了|就这么|方案是|以后用|敲定)"],
    "todo": [r"(待办|要做|记得|明天|下次|回头|提醒我|别忘)"],
}
_REMEMBER_HINT = ["记住", "记一下", "存一下", "备注"]

_PUNCT_CHARS = " \t\n\r，。！？、；：''（）【】《》,.!?;:\"'()[]<>~`*#-_/\\|@$%^&+=—…·"
_PUNCT = re.compile("[" + re.escape(_PUNCT_CHARS) + "]+")

EXTRACT_SYSTEM = (
    "你是一个记忆抽取器。从对话里抽出值得长期记住的条目。\n"
    "只允许四类：preference(偏好) / fact(事实) / decision(决定) / todo(待办)。\n"
    "闲聊、寒暄、过程性对话一律不要。宁可少抽，不要滥抽。\n"
    "每条：content（≤200字，第三人称陈述，如「用户偏好 docx 交付」）、type、importance(0~1)。\n"
    "只输出 JSON 数组，不要任何解释。格式：[{\"content\":\"...\",\"type\":\"fact\",\"importance\":0.8}]\n"
    "没有可记的就输出 []。"
)


def normalize(text: str) -> str:
    return _PUNCT.sub("", str(text or "")).lower()


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


class MemoryWriter:
    def __init__(self, store, index=None, embedder=None, retriever=None, logger=None,
                 max_len: int = 200, model_judge: bool = True, judge_provider=None,
                 user_id: str = "default", scope: str = "global"):
        self.store = store
        self.index = index
        self.embedder = embedder
        self.retriever = retriever
        self.logger = logger
        self.max_len = int(max_len)
        self.model_judge = bool(model_judge)
        self.judge_provider = judge_provider
        self.user_id = user_id
        self.scope = scope

    # ================= ① 要不要记 =================
    def should_write(self, text: str, hint: Optional[str] = None) -> Dict[str, Any]:
        s = str(text or "")
        explicit = any(h in s for h in _REMEMBER_HINT)
        hit_types = [t for t, pats in _SIGNALS.items() if any(re.search(p, s) for p in pats)]
        if hint in WRITE_TYPES:
            hit_types.insert(0, hint)
        if not hit_types and not explicit:
            return {"write": False, "type": None, "importance": 0.0, "reason": "规则：无四类信号"}
        mtype = hit_types[0] if hit_types else "fact"
        imp = 0.75 if explicit else 0.55
        return {"write": True, "type": mtype, "importance": imp,
                "reason": "规则命中：%s%s" % (mtype, "（显式要求记住）" if explicit else "")}

    # ================= ② 落库 =================
    def write(self, item: Dict[str, Any], session_id: Optional[str] = None,
              message_id: Optional[int] = None, judge: bool = True) -> Dict[str, Any]:
        content = sanitize(str(item.get("content") or "")).strip()
        mtype = item.get("type") if item.get("type") in WRITE_TYPES else "fact"
        if not content:
            return {"id": None, "action": "skipped", "reason": "空内容"}
        if len(content) > self.max_len:
            content = content[: self.max_len]          # 长了先截（规格书：≤200 字）
        user_id = item.get("user_id") or self.user_id
        scope = item.get("scope") or self.scope
        h = content_hash(content)

        # 查重
        dup = self.store.conn.execute(
            "SELECT id FROM memories WHERE user_id=? AND scope=? AND content_hash=? AND status='active'",
            (user_id, scope, h)).fetchone()
        if dup:
            self.store.memory_touch([dup["id"]])
            self.store.audit("agent", "memory", "dedup", dup["id"], {"content": content[:80]}, approved=True)
            return {"id": dup["id"], "action": "merged", "reason": "内容重复，只加热度"}

        # 冲突消解：新事实盖旧事实（有模型就判，没有就看相似度）
        superseded_id = None
        if judge:
            superseded_id = self._detect_conflict(content, mtype, user_id, scope)

        mid = "m" + hashlib.sha1(("%s|%s|%s|%d" % (user_id, scope, content, time.time_ns())).encode()).hexdigest()[:14]
        now = int(time.time() * 1000)
        self.store.conn.execute(
            "INSERT INTO memories(id,user_id,scope,type,content,content_hash,importance,confidence,"
            "source,source_session_id,source_message_id,created_at,updated_at,last_accessed_at,"
            "access_count,status,tags) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,'active',?)",
            (mid, user_id, scope, mtype, content, h,
             float(item.get("importance", 0.5)), float(item.get("confidence", 1.0)),
             item.get("source", "user_stated"), session_id, message_id,
             now, now, now, json.dumps(item.get("tags") or [], ensure_ascii=False)))
        if superseded_id:
            self.store.conn.execute(
                "UPDATE memories SET status='superseded', superseded_by=?, updated_at=? WHERE id=?",
                (mid, now, superseded_id))
        self.store.conn.commit()
        if self.index is not None:
            try:
                self.index.index_memory(mid, content)
            except Exception as e:
                self._log("vector_index_failed", error=str(e))
        self.store.audit("agent", "memory", "create", mid,
                         {"type": mtype, "scope": scope, "content": content[:80],
                          "superseded": superseded_id}, approved=True)
        return {"id": mid, "action": "created", "superseded_id": superseded_id}

    def _detect_conflict(self, content: str, mtype: str, user_id: str, scope: str) -> Optional[str]:
        """找"同一件事被改口"的旧条目。先向量找相近的，再让模型判是否覆盖。"""
        cands: List[Dict[str, Any]] = []
        if self.retriever is not None:
            try:
                cands = self.retriever.search(content, k=3, user_id=user_id, scope=scope,
                                              types=[mtype], touch=False)
            except Exception:
                cands = []
        if not cands:
            return None
        top = cands[0]
        if top["id"] and content_hash(top["content"]) == content_hash(content):
            return None
        sim = (top.get("score_breakdown") or {}).get("vector", 0.0)
        if self.model_judge and self.judge_provider is not None:
            verdict = self._judge(top["content"], content)
            if verdict is True:
                return top["id"]
            if verdict is False:
                return None
        return top["id"] if sim >= 0.90 else None

    def _judge(self, old: str, new: str) -> Optional[bool]:
        prompt = ("旧记忆：%s\n新记忆：%s\n\n新记忆是否更新/取代了旧记忆（说的是同一件事，但改口了）？"
                  "只回答 yes 或 no。" % (old, new))
        try:
            r = self.judge_provider.chat([{"role": "user", "content": prompt}],
                                         cfg={"temperature": 0, "max_tokens": 8})
            if not r.get("ok"):
                return None
            ans = (r["data"]["message"].get("content") or "").strip().lower()
            if ans.startswith("y") or "是" in ans[:3]:
                return True
            if ans.startswith("n") or "否" in ans[:3]:
                return False
        except Exception:
            return None
        return None

    # ================= ③ 从对话里抽 =================
    def extract(self, messages: List[Dict[str, Any]], provider=None,
                scope: Optional[str] = None) -> List[Dict[str, Any]]:
        """规则先筛：整段对话里没有四类信号就免了（省一次模型调用）。"""
        joined = "\n".join(str(m.get("content") or "") for m in messages
                           if m.get("role") in ("user", "assistant"))
        gate = self.should_write(joined)
        if not gate["write"]:
            return []
        if provider is None:
            return []
        try:
            r = provider.chat([{"role": "system", "content": EXTRACT_SYSTEM},
                               {"role": "user", "content": joined[:4000]}],
                              cfg={"temperature": 0, "max_tokens": 600})
            if not r.get("ok"):
                return []
            txt = (r["data"]["message"].get("content") or "").strip()
        except Exception:
            return []
        items = _parse_json_list(txt)
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            c = sanitize(str(it.get("content") or "")).strip()
            t = it.get("type")
            if not c or t not in WRITE_TYPES:
                continue
            try:
                imp = float(it.get("importance", 0.6))
            except (TypeError, ValueError):
                imp = 0.6
            out.append({"content": c[: self.max_len], "type": t,
                        "importance": max(0.0, min(1.0, imp)), "scope": scope or self.scope,
                        "source": "model_inferred"})
        return out

    def extract_and_write(self, messages: List[Dict[str, Any]], provider=None,
                          session_id: Optional[str] = None, scope: Optional[str] = None,
                          judge: bool = True) -> List[Dict[str, Any]]:
        results = []
        for item in self.extract(messages, provider=provider, scope=scope):
            try:
                results.append(self.write(item, session_id=session_id, judge=judge))
            except Exception as e:
                self._log("memory_write_failed", error=str(e))
        return results

    def _log(self, msg: str, **fields: Any) -> None:
        if self.logger:
            try:
                self.logger.warning("%s %s" % (msg, fields))
            except Exception:
                pass


def _parse_json_list(txt: str) -> List[Any]:
    s = txt.strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.M).strip()
    i, j = s.find("["), s.rfind("]")
    if i >= 0 and j > i:
        s = s[i:j + 1]
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except ValueError:
        return []
