# -*- coding: utf-8 -*-
"""L4 程序性记忆：经验与教训写成 markdown 文件（人能读能改），说话前先读。

为什么不入库（规格书 B.5）：人和智能体都要能直接读改 —— 这是自我进化那一层，塞进 sqlite 就死了。
库里只留一张 procedural_index，让程序知道"有哪些经验文件、什么时候变过"。
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

TITLE_RE = re.compile(r"^\s*#\s*(.+)$", re.M)


class ProceduralStore:
    def __init__(self, directory: str, store=None, logger=None, max_chars_per_file: int = 1200):
        self.directory = os.path.expanduser(directory)
        self.store = store
        self.logger = logger
        self.max_chars_per_file = int(max_chars_per_file)
        os.makedirs(self.directory, exist_ok=True)

    # ---------- 扫描与索引 ----------
    def scan(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not os.path.isdir(self.directory):
            return out
        for name in sorted(os.listdir(self.directory)):
            if not name.endswith(".md"):
                continue
            path = os.path.join(self.directory, name)
            try:
                st = os.stat(path)
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                continue
            title = (TITLE_RE.search(text).group(1).strip() if TITLE_RE.search(text) else name[:-3])
            summary = _summary(text)
            h = hashlib.sha1(("%d|%d" % (st.st_mtime_ns, st.st_size)).encode()).hexdigest()[:16]
            item = {"path": path, "title": title, "summary": summary,
                    "updated_at": int(st.st_mtime * 1000), "mtime_hash": h}
            out.append(item)
            if self.store is not None:
                self.store.conn.execute(
                    "INSERT INTO procedural_index(path,title,summary,updated_at,mtime_hash) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET title=excluded.title, "
                    "summary=excluded.summary, updated_at=excluded.updated_at, mtime_hash=excluded.mtime_hash",
                    (path, title, summary, item["updated_at"], h))
        if self.store is not None:
            self.store.conn.commit()
        return out

    # ---------- 读 / 写 ----------
    def read(self, path: str) -> str:
        p = path if os.path.isabs(path) else os.path.join(self.directory, path)
        with open(p, "r", encoding="utf-8") as f:
            return f.read()

    def append_note(self, topic: str, text: str, title: Optional[str] = None) -> str:
        """往某个主题文件里追加一条经验（没有就建）。"""
        safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", str(topic or "经验")).strip("_") or "经验"
        path = os.path.join(self.directory, safe + ".md")
        new = not os.path.exists(path)
        ts = time.strftime("%Y-%m-%d %H:%M")
        line = "- (%s) %s\n" % (ts, str(text).strip().replace("\n", " "))
        with open(path, "a", encoding="utf-8") as f:
            if new:
                f.write("# %s\n\n" % (title or safe))
            f.write(line)
        self.scan()
        return path

    # ---------- 注入 ----------
    def inject_block(self, max_chars: int = 2000) -> Tuple[str, List[str]]:
        """开场注入：只给标题 + 摘要（正文让模型按需自己去读）。"""
        items = self.scan()
        if not items:
            return "", []
        lines, paths, used = [], [], 0
        for it in items:
            line = "- %s：%s（文件 %s）" % (it["title"], it["summary"][:120], it["path"])
            if used + len(line) > max_chars and lines:
                break
            lines.append(line)
            paths.append(it["path"])
            used += len(line)
        return "\n".join(lines), paths

    def list(self) -> List[Dict[str, Any]]:
        return self.scan()


def _summary(text: str) -> str:
    """取正文里第一段正经话当摘要（跳过标题；经验条目剥掉时间戳）。"""
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^-\s*\([^)]*\)\s*(.+)$", s)
        if m:
            return m.group(1)[:160]
        if s.startswith(("-", "*")):
            return s.lstrip("-* ")[:160]
        return s[:160]
    return ""
