# -*- coding: utf-8 -*-
"""沙盒约束（规格书 3.7 · 沙盒执行）。

两件事：
  1) 文件操作限定允许目录（读根 / 写根），越界路径被拒并留痕（留痕由 gate/audit 做）。
  2) 命令执行走白名单（可执行程序名 + 可选子串黑名单）。

设计成纯函数式：check_path / check_command 只判不执行。
路径判定用 realpath 消掉 `..` 与符号链接，避免"绕出去"。
"""
from __future__ import annotations

import os
import shlex
from fnmatch import fnmatch
from typing import Any, Dict, List, Tuple

# 默认禁用的危险命令片段（即使可执行名在白名单里，命中这些也拒）
DEFAULT_CMD_DENY = ["rm -rf /", "mkfs", "dd if=", ":(){", "chmod -R 777 /", "> /dev/sda",
                    "shutdown", "reboot", "halt", "passwd", "sudo rm"]


class Sandbox:
    def __init__(self, cfg: Dict[str, Any] = None, base_dir: str = None):
        cfg = dict(cfg or {})
        self.enabled = bool(cfg.get("enabled", True))
        base = os.path.abspath(os.path.expanduser(base_dir or os.getcwd()))
        self.base_dir = base

        self.read_roots: List[str] = [_abs(p, base) for p in
                                      (cfg.get("read_roots") or ["~", "/tmp", base])]
        self.write_roots: List[str] = [_abs(p, base) for p in
                                       (cfg.get("write_roots") or [os.path.join(base, "data"), "/tmp"])]
        self.cmd_allow: List[str] = list(cfg.get("cmd_allow") or
                                         ["python3", "python", "ls", "cat", "head", "tail", "wc",
                                          "grep", "find", "echo", "date", "pwd", "du", "df", "uname"])
        self.cmd_deny_substrings: List[str] = list(cfg.get("cmd_deny_substrings") or DEFAULT_CMD_DENY)
        self.max_script_seconds = float(cfg.get("max_script_seconds", 30))
        # 自我保护清单：即使落在写根里，也**永远不许工具写**
        # （改自己的权限口径 / 直接碰记忆库 —— 这两样只能由人手动改）
        self.protect: List[str] = list(cfg.get("protect") or
                                       ["data/approvals.json", "*.db", "*.db-wal", "*.db-shm",
                                        "config/config.yaml"])

    # ---- 自我保护 ----
    def is_protected(self, path: str) -> Optional[str]:
        """命中自我保护清单返回命中的模式名，否则 None。"""
        if not self.enabled or not path:
            return None
        p = _abs(path, self.base_dir)
        base = os.path.basename(p)
        for pat in self.protect:
            full = _abs(pat, self.base_dir)
            if p == full or _within(p, full):
                return pat
            if fnmatch(p, full) or fnmatch(base, pat):
                return pat
        return None

    # ---- 路径 ----
    def check_path(self, path: str, mode: str = "read") -> Tuple[bool, str]:
        if not self.enabled:
            return True, "sandbox_off"
        if not path:
            return False, "空路径"
        if mode in ("write", "danger"):
            hit = self.is_protected(path)
            if hit:
                return False, "命中自我保护清单（%s），工具不许写：%s" % (hit, path)
        roots = self.write_roots if mode == "write" else self.read_roots
        p = _abs(path, self.base_dir)
        for r in roots:
            if _within(p, r):
                return True, "在允许目录内：%s" % r
        return False, "路径越界（%s 不在%s根内）：%s" % (p, "写" if mode == "write" else "读", roots)

    # ---- 命令 ----
    def check_command(self, cmd: Any) -> Tuple[bool, str]:
        if not self.enabled:
            return True, "sandbox_off"
        argv = cmd if isinstance(cmd, (list, tuple)) else shlex.split(str(cmd or ""))
        if not argv:
            return False, "空命令"
        prog = os.path.basename(str(argv[0]))
        if prog not in self.cmd_allow:
            return False, "可执行程序不在白名单：%s（允许：%s）" % (prog, ", ".join(self.cmd_allow))
        joined = " ".join(str(a) for a in argv)
        for bad in self.cmd_deny_substrings:
            if bad and bad in joined:
                return False, "命中危险命令片段：%s" % bad
        # 命令里出现的绝对路径也要在允许范围内（粗筛，防 cd 出去乱翻）
        for a in argv:
            s = str(a)
            if s.startswith("/") and os.path.sep in s[1:] and not s.startswith("-"):
                if self.is_protected(s):
                    return False, "命令里碰到了自我保护清单里的路径：%s" % s
                ok, _why = self.check_path(s, "read")
                if not ok:
                    return False, "命令参数里的路径越界：%s" % s
        return True, "白名单通过"

    def to_dict(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "read_roots": self.read_roots, "write_roots": self.write_roots,
                "cmd_allow": self.cmd_allow, "cmd_deny_substrings": self.cmd_deny_substrings,
                "protect": self.protect}


def _abs(p: str, base: str) -> str:
    p = os.path.expanduser(str(p))
    if not os.path.isabs(p):
        p = os.path.join(base, p)
    return os.path.realpath(p)


def _within(path: str, root: str) -> bool:
    path = os.path.realpath(path)
    root = os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)
