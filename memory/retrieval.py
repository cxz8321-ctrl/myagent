# -*- coding: utf-8 -*-
"""检索排序：混合打分 = 语义相似度 × 权重 + 关键词命中 + 时间衰减 + 重要度。

口径（规格书 4.2 / B.10）：
  - 宁少勿多：无关片段比缺少片段伤害更大（top-k 3~8）
  - 衰减分数不存，实时算（由 type 定半衰期 + importance + last_accessed_at + access_count）
  - 返回片段而非全文；每条带来源与时间可追溯
"""
from __future__ import annotations

import math
import re
import time
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_WEIGHTS = {"vector": 0.55, "keyword": 0.20, "recency": 0.15, "importance": 0.10}

# 半衰期（天）：状态类快衰减，事实/经验类慢衰减
HALF_LIFE_DAYS = {"todo": 7, "fact": 90, "decision": 120, "preference": 180,
                  "experience": 180, "profile": 365}
DEFAULT_HALF_LIFE = 90

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[a-zA-Z0-9_]{2,}")


def terms(text: str) -> set:
    """粗分词：中文取双字 + 英文/数字词。够用且零依赖。"""
    s = str(text or "")
    out = set()
    for run in _CJK.findall(s):
        if len(run) == 1:
            out.add(run)
        for i in range(len(run) - 1):
            out.add(run[i:i + 2])
    for w in _WORD.findall(s):
        out.add(w.lower())
    return out


def recency_score(ts_ms: Optional[int], mtype: str, now_ms: Optional[int] = None) -> float:
    if not ts_ms:
        return 0.3
    now = now_ms or int(time.time() * 1000)
    age_days = max(0.0, (now - int(ts_ms)) / 86400000.0)
    hl = HALF_LIFE_DAYS.get(mtype, DEFAULT_HALF_LIFE)
    return float(math.exp(-math.log(2) * age_days / hl))


class Retriever:
    def __init__(self, store, embedder=None, index=None, weights: Optional[Dict[str, float]] = None,
                 min_score: float = 0.15, logger=None):
        self.store = store
        self.embedder = embedder
        self.index = index
        self.weights = dict(DEFAULT_WEIGHTS)
        if weights:
            self.weights.update(weights)
        self.min_score = float(min_score)
        self.logger = logger

    # ---------- 主入口 ----------
    def search(self, query: str, k: int = 5, user_id: str = "default",
               scope: Optional[str] = None, types: Optional[List[str]] = None,
               touch: bool = True, audit: bool = False) -> List[Dict[str, Any]]:
        rows = self._candidates(user_id, scope, types)
        if not rows:
            return []
        qterms = terms(query)
        sims: Dict[str, float] = {}
        if self.embedder and self.embedder.available() and self.index is not None:
            try:
                qvec = self.embedder.embed_one(query, is_query=True)
                allowed = {r["id"] for r in rows}
                sims = dict(self.index.search(qvec, k=len(allowed), allowed_ids=allowed))
            except Exception as e:
                if self.logger:
                    self.logger.warning("向量检索失败，退回纯关键词：%s" % e)

        now = int(time.time() * 1000)
        scored: List[Dict[str, Any]] = []
        for r in rows:
            v = max(0.0, float(sims.get(r["id"], 0.0)))
            t = terms(r["content"])
            kw = (len(qterms & t) / len(qterms)) if qterms else 0.0
            rec = recency_score(r["last_accessed_at"] or r["updated_at"] or r["created_at"],
                                r["type"] or "", now)
            imp = float(r["importance"] if r["importance"] is not None else 0.5)
            total = (self.weights["vector"] * v + self.weights["keyword"] * kw
                     + self.weights["recency"] * rec + self.weights["importance"] * imp)
            if total < self.min_score:
                continue
            scored.append({
                "id": r["id"], "content": r["content"], "type": r["type"], "scope": r["scope"],
                "importance": imp, "created_at": r["created_at"], "status": r["status"],
                "score": round(total, 4),
                "score_breakdown": {"vector": round(v, 4), "keyword": round(kw, 4),
                                    "recency": round(rec, 4), "importance": round(imp, 4)},
            })
        scored.sort(key=lambda x: -x["score"])
        out = scored[:int(k)]
        if touch and out:
            self.store.memory_touch([x["id"] for x in out])
        if audit:
            try:
                self.store.audit("agent", "memory", "search", ",".join(x["id"] for x in out),
                                 {"query": query[:80], "k": k, "hits": len(out)})
            except Exception:
                pass
        return out

    def _candidates(self, user_id: str, scope: Optional[str], types: Optional[List[str]]) -> List[Any]:
        q = ("SELECT id,content,type,scope,importance,created_at,updated_at,last_accessed_at,"
             "access_count,status FROM memories WHERE status='active'")
        args: List[Any] = []
        if user_id:
            q += " AND user_id=?"; args.append(user_id)
        if scope:
            q += " AND scope=?"; args.append(scope)
        if types:
            q += " AND type IN (%s)" % ",".join("?" * len(types)); args.extend(types)
        q += " ORDER BY created_at DESC LIMIT 5000"
        return self.store.conn.execute(q, tuple(args)).fetchall()

    # ---------- 注入格式 ----------
    def format_block(self, items: List[Dict[str, Any]], max_chars: int = 2000,
                     now_ms: Optional[int] = None) -> Tuple[str, List[str]]:
        """把命中片段格式化成上下文块，标注来源与时间。返回 (文本, 命中的 id 列表)。"""
        if not items:
            return "", []
        now = now_ms or int(time.time() * 1000)
        label = {"preference": "偏好", "fact": "事实", "decision": "决定",
                 "todo": "待办", "experience": "经验", "profile": "档案"}
        lines, ids, used = [], [], 0
        for it in items:
            age_d = max(0, (now - int(it.get("created_at") or now)) / 86400000.0)
            when = "刚刚" if age_d < 1 else ("%d天前" % int(age_d) if age_d < 60 else "%d个月前" % int(age_d / 30))
            line = "- [%s·%s] %s" % (label.get(it.get("type"), it.get("type") or "记忆"), when,
                                     str(it.get("content"))[:200])
            if used + len(line) > max_chars and lines:
                break
            lines.append(line)
            ids.append(it["id"])
            used += len(line)
        return "\n".join(lines), ids
