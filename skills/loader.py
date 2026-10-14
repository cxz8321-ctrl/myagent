# -*- coding: utf-8 -*-
"""技能加载器：SKILL.md 解析 + 目录扫描 + 脚本技能封装（规格书 5.1）。

一个技能 = 一个文件夹：
    my_skill/
      SKILL.md          头部元数据（ASSISTANTML frontmatter）+ 正文说明
      scripts/run.py    可执行脚本（可选；entry 指向它）
      references/       长文档/模板/数据（可选；按需读取，不占常驻上下文）

SKILL.md 头部字段：
    name, description, triggers, permission(read|write|danger),
    timeout_ms, is_async, entry, tags

脚本技能的执行约定（agent 与脚本之间的合同）：
    - 用 entry 指定的脚本启动；stdin 收一行 JSON：{"args": {...}, "ctx": {...}}
    - stdout 应输出一行 JSON：{"ok": true, "content": "...", "meta": {...}}
      若输出的不是 JSON，则整段 stdout 当 content（ok=true）
    - 退出码非 0 视为 TOOL_FAILED
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

from skills.registry import SkillMeta

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

SKILL_FILE = "SKILL.md"
DEFAULT_TIMEOUT_MS = 30000


# ---------------- 解析 ----------------
def parse_skill_md(text: str) -> Tuple[Dict[str, Any], str]:
    """把 SKILL.md 拆成 (头部元数据, 正文)。没有 frontmatter 时元数据为空。"""
    raw = text or ""
    meta: Dict[str, Any] = {}
    body = raw
    if raw.lstrip().startswith("---"):
        stripped = raw.lstrip()
        end = stripped.find("\n---", 3)
        if end != -1:
            head = stripped[3:end]
            body = stripped[end + 4:].lstrip("\n")
            if yaml is not None:
                meta = yaml.safe_load(head) or {}
            else:
                meta = _mini_yaml(head)
    if not isinstance(meta, dict):
        meta = {}
    return meta, body.strip()


def _mini_yaml(head: str) -> Dict[str, Any]:
    """没装 PyASSISTANTML 时的极简回退：只认 `key: value` 与 `key: [a, b]`。"""
    out: Dict[str, Any] = {}
    for line in head.splitlines():
        if ":" not in line or line.strip().startswith("#"):
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            out[k.strip()] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
        elif v.lower() in ("true", "false"):
            out[k.strip()] = v.lower() == "true"
        else:
            out[k.strip()] = v.strip("'\"")
    return out


# ---------------- 脚本处理器 ----------------
def make_script_handler(entry_path: str, timeout_ms: int, cwd: str):
    """把一个脚本包成 handler(args, ctx) -> 结构化 dict。"""
    def handler(args: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        argv = _script_argv(entry_path)
        payload = json.dumps({"args": args or {}, "ctx": _ctx_public(ctx)}, ensure_ascii=False)
        to = max(1, int(timeout_ms) / 1000.0)
        try:
            p = subprocess.run(argv, input=payload + "\n", capture_output=True, text=True,
                               cwd=cwd, timeout=to)
        except subprocess.TimeoutExpired:
            raise TimeoutError("技能脚本超时（%ss）：%s" % (to, entry_path))
        if p.returncode != 0:
            raise RuntimeError("脚本退出码 %s：%s" % (p.returncode, (p.stderr or "").strip()[:500]))
        out = (p.stdout or "").strip()
        if not out:
            return {"ok": True, "content": "", "meta": {"empty": True}}
        try:
            data = json.loads(out.splitlines()[-1])
            if isinstance(data, dict):
                return data
            return {"ok": True, "content": json.dumps(data, ensure_ascii=False)}
        except ValueError:
            return {"ok": True, "content": out}
    handler.wants_ctx = True
    return handler


def _script_argv(entry_path: str) -> List[str]:
    low = entry_path.lower()
    if low.endswith(".py"):
        return [sys.executable, entry_path]
    if low.endswith(".sh"):
        return ["bash", entry_path]
    return [entry_path]


def _ctx_public(ctx: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    ctx = ctx or {}
    return {k: ctx.get(k) for k in ("session_id", "task_id", "trace_id", "skill_dir") if ctx.get(k)}


# ---------------- 扫描与加载 ----------------
class SkillLoader:
    def __init__(self, dirs: List[str], logger=None):
        self.dirs = [os.path.abspath(os.path.expanduser(d)) for d in (dirs or [])]
        self.logger = logger

    def load(self) -> Dict[str, Any]:
        """扫描所有目录，返回 {skills, errors, scanned}。坏技能不拖垮其他技能。"""
        metas: List[SkillMeta] = []
        errors: List[Dict[str, str]] = []
        scanned: List[str] = []
        for base in self.dirs:
            if not os.path.isdir(base):
                continue
            for name in sorted(os.listdir(base)):
                folder = os.path.join(base, name)
                if not os.path.isdir(folder) or name.startswith((".", "_")):
                    continue
                md = os.path.join(folder, SKILL_FILE)
                if not os.path.isfile(md):
                    continue           # 不是技能包，安静跳过
                scanned.append(folder)
                try:
                    metas.append(self.load_folder(folder))
                except Exception as e:  # noqa: BLE001
                    errors.append({"path": folder, "reason": "%s: %s" % (e.__class__.__name__, e)})
        return {"skills": metas, "errors": errors, "scanned": scanned}

    def load_folder(self, folder: str) -> SkillMeta:
        folder = os.path.abspath(folder)
        md = os.path.join(folder, SKILL_FILE)
        with open(md, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        meta, body = parse_skill_md(text)
        name = str(meta.get("name") or os.path.basename(folder)).strip()
        if not name:
            raise ValueError("SKILL.md 缺少 name")
        timeout_ms = int(meta.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
        entry = meta.get("entry")
        handler = None
        entry_abs = None
        if entry:
            entry_abs = os.path.join(folder, str(entry))
            if not os.path.isfile(entry_abs):
                raise ValueError("entry 不存在：%s" % entry)
            handler = make_script_handler(entry_abs, timeout_ms, cwd=folder)
        elif os.path.isfile(os.path.join(folder, "scripts", "run.py")):
            entry_abs = os.path.join(folder, "scripts", "run.py")
            handler = make_script_handler(entry_abs, timeout_ms, cwd=folder)
        refs = []
        ref_dir = os.path.join(folder, "references")
        if os.path.isdir(ref_dir):
            refs = sorted(os.listdir(ref_dir))
        return SkillMeta(
            name=name,
            description=str(meta.get("description") or body[:200] or name),
            permission=str(meta.get("permission") or "read"),
            parameters=meta.get("parameters") or _default_params(),
            handler=handler,
            timeout_ms=timeout_ms,
            is_async=bool(meta.get("is_async", False)),
            triggers=list(meta.get("triggers") or []),
            source="file:%s" % folder,
            path=folder,
            entry=entry_abs,
            references=refs,
            body=body,
            tags=list(meta.get("tags") or []),
        )


def _default_params() -> Dict[str, Any]:
    return {"type": "object", "properties": {}, "required": []}
