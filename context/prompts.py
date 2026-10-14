# -*- coding: utf-8 -*-
"""提示词模板管理：系统提示外置成文件、带版本号、支持变量注入。

改提示词不改代码；文件没了退回内置默认，不崩。
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional, Tuple

DEFAULT_SYSTEM = (
    "你是骛（myagent），一个务实、不端不装的助理。\n"
    "需要动手时就用工具，用完根据结果继续，不要编造工具结果。\n"
    "回答用中文，简洁直接。"
)

_VER_RE = re.compile(r"version\s*[:=]\s*([\w.\-]+)", re.I)
_VAR_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


class PromptStore:
    def __init__(self, directory: Optional[str] = None):
        self.directory = os.path.expanduser(directory) if directory else None
        self._cache: Dict[str, Tuple[str, str, Optional[str]]] = {}   # name -> (text, version, path)

    def path_of(self, name: str) -> Optional[str]:
        if not self.directory:
            return None
        for ext in (".md", ".txt"):
            p = os.path.join(self.directory, name + ext)
            if os.path.isfile(p):
                return p
        return None

    def load(self, name: str = "system", use_cache: bool = True) -> Tuple[str, str, Optional[str]]:
        """返回 (模板正文, 版本号, 文件路径)；文件不存在则给默认。"""
        if use_cache and name in self._cache:
            return self._cache[name]
        p = self.path_of(name)
        if not p:
            out: Tuple[str, str, Optional[str]] = (DEFAULT_SYSTEM if name == "system" else "", "builtin", None)
            self._cache[name] = out
            return out
        with open(p, "r", encoding="utf-8") as f:
            raw = f.read()
        head = raw[:400]
        m = _VER_RE.search(head)
        version = m.group(1) if m else "unknown"
        body = re.sub(r"^\s*<!--.*?-->\s*", "", raw, count=1, flags=re.S).strip()
        out = (body, version, p)
        self._cache[name] = out
        return out

    def render(self, name: str = "system", variables: Optional[Dict[str, Any]] = None,
               use_cache: bool = True) -> Dict[str, Any]:
        body, version, path = self.load(name, use_cache=use_cache)
        vars_ = dict(variables or {})

        def sub(m: "re.Match[str]") -> str:
            key = m.group(1)
            if key in vars_:
                return str(vars_[key])
            if key.startswith("env."):
                return os.environ.get(key[4:], "")
            return m.group(0)          # 没给值就留原样，方便发现漏填

        return {"text": _VAR_RE.sub(sub, body), "version": version, "path": path,
                "missing": sorted({m.group(1) for m in _VAR_RE.finditer(body)} - set(vars_))}

    def reload(self) -> None:
        self._cache.clear()
