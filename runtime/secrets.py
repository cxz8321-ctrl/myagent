# -*- coding: utf-8 -*-
"""密钥读取：只从环境变量或独立密钥文件读；代码与日志里不出现任何 key。

查找顺序（先命中先用）：
  1. 环境变量            DEEPSEEK_API_KEY / DEEPSEEK_KEY / DEEPSEEK_TOKEN
  2. ~/.myagent/secrets.json            {"deepseek": "sk-..."}  或 {"deepseek": {"apiKey": "..."}}
  3. ~/.config/myagent/secrets.json           {"deepseek": {"apiKey": "..."}}
  4. ~/.config/external-agent/credentials.yaml           refs: {DEEPSEEK_API_KEY: ...}
"""
from __future__ import annotations

import json
import os
from typing import List, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ENV_SUFFIXES = ("_API_KEY", "_KEY", "_TOKEN")


def _env_names(ref: str) -> List[str]:
    up = ref.upper().replace("-", "_").replace(".", "_")
    return [up + s for s in ENV_SUFFIXES]


def _from_env(ref: str) -> Optional[str]:
    for name in _env_names(ref):
        v = os.environ.get(name)
        if v:
            return v.strip()
    return None


def _from_json(path: str, ref: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    node = d.get(ref)
    if isinstance(node, str) and node.strip():
        return node.strip()
    if isinstance(node, dict):
        for k in ("apiKey", "api_key", "key", "token"):
            if node.get(k):
                return str(node[k]).strip()
    return None


def _from_external_creds(ref: str) -> Optional[str]:
    p = os.path.expanduser("~/.config/external-agent/credentials.yaml")
    if yaml is None or not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = yaml.safe_load(f) or {}
    except Exception:
        return None
    refs = d.get("refs") or {}
    for name in _env_names(ref):
        if refs.get(name):
            return str(refs[name]).strip()
    return None


def get_secret(ref: str) -> Optional[str]:
    """按顺序找密钥，找不到返回 None（由调用方决定怎么报错）。"""
    if not ref:
        return None
    for fn in (
        lambda: _from_env(ref),
        lambda: _from_json(os.path.expanduser("~/.myagent/secrets.json"), ref),
        lambda: _from_json(os.path.expanduser("~/.config/myagent/secrets.json"), ref),
        lambda: _from_external_creds(ref),
    ):
        try:
            v = fn()
        except Exception:
            v = None
        if v:
            return v
    return None


def mask(secret: Optional[str]) -> str:
    """只用于诊断输出；不要打印明文。"""
    if not secret:
        return "(none)"
    s = str(secret)
    if len(s) <= 8:
        return "*" * len(s)
    return s[:3] + "*" * 6 + s[-3:]
