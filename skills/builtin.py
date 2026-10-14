# -*- coding: utf-8 -*-
"""内置基础技能包（规格书 3.3：先亲手写五个）。

1) read_file   读文件（只读）
2) list_dir    目录遍历（只读）
3) calc        算数/数据处理（只读）
4) now         时间（只读）
5) http_get    联网检索（只读，走本地 SearXNG 或直连）
"""
from __future__ import annotations

import ast
import json
import math
import os
import time
from typing import Any, Dict, List

from skills.registry import SkillMeta

MAX_FILE_BYTES = 200_000


# ---------------- 1. 读文件 ----------------
def _read_file(args: Dict[str, Any]) -> Any:
    path = os.path.expanduser(str(args.get("path", "")))
    if not path:
        raise ValueError("path 必填")
    if not os.path.isfile(path):
        return "文件不存在：%s" % path
    size = os.path.getsize(path)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read(MAX_FILE_BYTES)
    return {"path": path, "size": size, "truncated": size > MAX_FILE_BYTES,
            "content": text}


READ_FILE = SkillMeta(
    name="read_file",
    description="读一个文本文件的全部内容（最多 200KB）。参数 path 为绝对路径或 ~ 开头路径。",
    permission="read",
    parameters={"type": "object", "properties": {"path": {"type": "string", "description": "文件路径"}},
                "required": ["path"]},
    handler=_read_file,
)


# ---------------- 2. 目录遍历 ----------------
def _list_dir(args: Dict[str, Any]) -> Any:
    path = os.path.expanduser(str(args.get("path") or "."))
    if not os.path.isdir(path):
        return "目录不存在：%s" % path
    items: List[Dict[str, Any]] = []
    for name in sorted(os.listdir(path))[:200]:
        p = os.path.join(path, name)
        try:
            st = os.stat(p)
        except OSError:
            continue
        items.append({"name": name, "type": "dir" if os.path.isdir(p) else "file",
                      "size": st.st_size, "mtime": int(st.st_mtime * 1000)})
    return {"path": os.path.abspath(path), "count": len(items), "items": items}


LIST_DIR = SkillMeta(
    name="list_dir",
    description="列出一个目录下的文件与子目录（最多 200 项）。参数 path 为目录路径，默认当前目录。",
    permission="read",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": []},
    handler=_list_dir,
)


# ---------------- 3. 算数 ----------------
_ALLOWED = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
_ALLOWED.update({"abs": abs, "round": round, "min": min, "max": max, "sum": sum, "len": len,
                 "int": int, "float": float, "str": str, "list": list, "range": range,
                 "sorted": sorted, "set": set, "dict": dict, "pow": pow})


def _safe_eval(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.BinOp):
        return {"Add": lambda a, b: a + b, "Sub": lambda a, b: a - b, "Mult": lambda a, b: a * b,
                "Div": lambda a, b: a / b, "FloorDiv": lambda a, b: a // b,
                "Mod": lambda a, b: a % b, "Pow": lambda a, b: a ** b}[type(node.op).__name__](
            _safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp):
        v = _safe_eval(node.operand)
        return -v if isinstance(node.op, ast.USub) else +v if isinstance(node.op, ast.UAdd) else not v
    if isinstance(node, ast.Call):
        fn = _safe_eval(node.func)
        if not callable(fn):
            raise ValueError("不允许调用：%s" % ast.dump(node.func))
        return fn(*[_safe_eval(a) for a in node.args], **{k.arg: _safe_eval(k.value) for k in node.keywords})
    if isinstance(node, ast.Name):
        if node.id in _ALLOWED:
            return _ALLOWED[node.id]
        raise ValueError("未知名字：%s" % node.id)
    if isinstance(node, (ast.Tuple, ast.List)):
        return [_safe_eval(e) for e in node.elts]
    raise ValueError("不支持的表达式：%s" % type(node).__name__)


def _calc(args: Dict[str, Any]) -> Any:
    expr = str(args.get("expr", ""))
    if not expr:
        raise ValueError("expr 必填")
    if len(expr) > 500:
        raise ValueError("表达式过长")
    tree = ast.parse(expr, mode="eval")
    return {"expr": expr, "value": _safe_eval(tree)}


CALC = SkillMeta(
    name="calc",
    description="算一个数学表达式（支持 + - * / ** 与 math 里的函数，如 sqrt/log/sin）。参数 expr 为表达式字符串。",
    permission="read",
    parameters={"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]},
    handler=_calc,
)


# ---------------- 4. 时间 ----------------
def _now(args: Dict[str, Any]) -> Any:
    tz = args.get("tz") or "Asia/Shanghai"
    try:
        from zoneinfo import ZoneInfo
        now = time.time()
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        return {"epoch_ms": int(now * 1000), "local": t, "tz": tz,
                "weekday": time.strftime("%A", time.localtime(now))}
    except Exception:
        return {"epoch_ms": int(time.time() * 1000), "local": time.strftime("%Y-%m-%d %H:%M:%S")}


NOW = SkillMeta(
    name="now",
    description="取当前时间（本地时区 Asia/Shanghai）。无需参数。",
    permission="read",
    parameters={"type": "object", "properties": {}, "required": []},
    handler=_now,
)


# ---------------- 5. 联网检索 ----------------
def _http_get(args: Dict[str, Any]) -> Any:
    import requests
    url = str(args.get("url", ""))
    if not url.startswith(("http://", "https://")):
        raise ValueError("url 必须是 http(s)")
    kwargs: Dict[str, Any] = {"timeout": 15}
    if args.get("proxy"):
        kwargs["proxies"] = {"http": args["proxy"], "https": args["proxy"]}
    elif "127.0.0.1" in url or "localhost" in url:
        kwargs["proxies"] = {"http": "", "https": ""}   # 本地服务绕开代理
    elif os.environ.get("MASSISTANTGENT_HTTPS_PROXY"):
        kwargs["proxies"] = {"http": os.environ["MASSISTANTGENT_HTTPS_PROXY"],
                             "https": os.environ["MASSISTANTGENT_HTTPS_PROXY"]}
    r = requests.get(url, **kwargs)
    text = r.text[:20000]
    ctype = r.headers.get("Content-Type", "")
    out: Dict[str, Any] = {"url": url, "status": r.status_code, "content_type": ctype,
                           "truncated": len(r.text) > 20000, "body": text}
    if "json" in ctype:
        try:
            out["json"] = r.json()
        except ValueError:
            pass
    return out


HTTP_GET = SkillMeta(
    name="http_get",
    description="GET 一个 URL 并返回正文（最多 20KB）。本机服务用 127.0.0.1，被墙站点可传 proxy。",
    permission="read",
    parameters={"type": "object", "properties": {"url": {"type": "string"},
                                                "proxy": {"type": "string"}},
                "required": ["url"]},
    handler=_http_get,
)


BUILTIN: List[SkillMeta] = [READ_FILE, LIST_DIR, CALC, NOW, HTTP_GET]
