# -*- coding: utf-8 -*-
"""配置加载：默认值 → config.yaml → 环境变量覆盖（删掉配置文件也能跑）。"""
from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

DEFAULTS: Dict[str, Any] = {
    "default_provider": "deepseek",
    "providers": {
        "deepseek": {
            "type": "deepseek",
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com",
            "api_key_ref": "deepseek",
            "timeout_ms": 120000,
            "retries": 3,
            "temperature": 0.3,
        },
        "local": {
            "type": "llamacpp",
            "model": "qwen3-4b-q4km",
            "model_path": "~/.local/models/Qwen3-4B-Q4_K_M.gguf",
            "n_ctx": 8192,
            "n_threads": 0,
            "no_think": True,
            "max_tokens": 1024,
            "timeout_ms": 300000,
            "retries": 0,
            "temperature": 0.6,
        },
    },
    "context": {
        "total_tokens": 32768,
        "reserve_output": 2048,
        "keep_last": 6,
        "max_tool_result_chars": 6000,
        "prompt_dir": "config/prompts",
        "prompt_name": "system",
        "weights": {"system": 0.15, "memory": 0.25, "tools": 0.15, "history": 0.45},
    },
    "prompt": {
        "name": "骛",
        "creature": "一个本地运行的智能体助手",
        "style": "说人话、务实、有自己判断，不端不装，不说客套话。",
    },
    "storage": {
        "data_dir": "data",
        "db_file": "myagent.db",
        "inline_limit": 8192,
        "history_load": 12,
    },
    "memory": {
        "enabled": True,
        "user_id": "default",
        "scope": "global",
        "embed": {"enabled": True, "model_path": "~/.local/models/Qwen3-Embedding-0.6B-Q8_0.gguf",
                  "model_name": "qwen3-embedding-0.6b", "n_ctx": 512, "query_prefix": ""},
        "retrieve": {"top_k": 5, "min_score": 0.15,
                     "weights": {"vector": 0.55, "keyword": 0.20, "recency": 0.15, "importance": 0.10}},
        "write": {"enabled": True, "model_judge": True, "max_len": 200},
        "inject": {"enabled": True, "max_chars": 2000},
        "summarize": {"enabled": True, "trigger_ratio": 0.7, "keep_last": 8},
        "procedural": {"enabled": True, "dir": "memory/procedural", "inject_max_chars": 2000},
    },
    "security": {
        "subjects": {
            "default_issuer": "owner",
            "subjects": {
                "owner": {"max_tier": "danger", "can_grant_persistent": True, "note": "本地管理员"},
                "assistant": {"max_tier": "write", "can_grant_persistent": False, "note": "上层调用者"},
                "external": {"max_tier": "write", "can_grant_persistent": False, "note": "外部编码助手"},
                "self": {"max_tier": "read", "can_grant_persistent": False, "note": "它自己发起的"},
                "unknown": {"max_tier": "read", "can_grant_persistent": False, "note": "未识别（按最严）"},
            },
        },
        "policy": {
            "default_tier": "danger",         # 漏网工具一律按最严
            "actions": {"read": "allow", "write": "ask", "danger": "ask"},
            "second_confirm": True,           # 危险级二次确认
            "tiers": {},                      # 工具名 -> 级别（覆盖内置表）
            "overrides": {},
        },
        "sandbox": {
            "enabled": True,
            # 只读根：放宽到系统信息（这样它能答"这台机器什么配置"），但仍不含其它人的目录
            "read_roots": ["~", "/tmp", "/proc", "/sys", "/etc/os-release", "/etc/lsb-release",
                           "/etc/hostname", "/etc/hosts", "/etc/timezone"],
            "write_roots": ["data", "workspace", "/tmp"],
            # 自我保护：即使落在写根里，工具也永远不许写（改自己权限口径 / 碰记忆库）
            "protect": ["data/approvals.json", "*.db", "*.db-wal", "*.db-shm", "config/config.yaml"],
            "cmd_allow": ["python3", "python", "ls", "cat", "head", "tail", "wc", "grep",
                          "find", "echo", "date", "pwd", "du", "df", "uname"],
            "max_script_seconds": 30,
        },
        "approvals": {"path": "data/approvals.json", "allow": [], "deny": []},
    },
    "mcp": {
        "enabled": True,
        "servers": {},
    },
    # M5 运行时工程：并发 / trace 落盘 / 状态机
    "runtime": {
        "tools": {"parallel": True, "max_workers": 4},   # 同一轮无依赖工具并行
        "trace": {"enabled": True, "dir": "data/traces"},  # 每步 trace 落盘（JSONL）
        "state_machine": {"strict": False},                # 非法转移：False=记录不炸
    },
    # M5 费用统计：token × 单价（占位价，实际以厂商官网为准）
    "pricing": {
        "currency": "CNY",
        "unit": 1000000,
        "notes": "占位价，仅用于本地可观测统计，实际以各厂商官网为准。",
        "providers": {
            "deepseek": {"input": 2.0, "output": 8.0, "note": "占位：以 DeepSeek 官网为准"},
            "local": {"input": 0.0, "output": 0.0},
            "default": {"input": 0.0, "output": 0.0},
        },
    },
    "paths": {"data_dir": "data", "log_dir": "logs", "skill_dir": "skills",
              "skill_dirs": ["skillpacks"]},
    # M7 入口层：HTTP API（规格书 3.8）。host/port/tokens/session_scope 都在这。
    "api": {
        "host": "127.0.0.1",
        "port": 8765,
        "tokens": [],              # Bearer 白名单；也可用 MASSISTANTGENT_API_TOKEN / MASSISTANTGENT_API_TOKENS
        "session_scope": "user",   # user=同一用户跨入口共用一条会话；channel=按渠道各一条
        "app_name": "myagent",     # 网页版/清单里显示的名字（改这里就换名）
    },
}

ENV_PREFIX = "MASSISTANTGENT__"


def _coerce(v: str) -> Any:
    s = v.strip()
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    if s[:1] in "[{":
        try:
            return json.loads(s)
        except ValueError:
            pass
    return v


def deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_config() -> Optional[str]:
    cand = []
    if os.environ.get("MASSISTANTGENT_CONFIG"):
        cand.append(os.environ["MASSISTANTGENT_CONFIG"])
    cand.append(os.path.join(project_root(), "config", "config.yaml"))
    cand.append(os.path.expanduser("~/.myagent/config.yaml"))
    for p in cand:
        if p and os.path.isfile(p):
            return p
    return None


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    cfg = copy.deepcopy(DEFAULTS)
    p = path or find_config()
    if p:
        if yaml is None:
            raise RuntimeError("需要 PyASSISTANTML 才能读配置文件：%s" % p)
        with open(p, "r", encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        if not isinstance(user, dict):
            raise ValueError("配置文件顶层必须是映射：%s" % p)
        cfg = deep_merge(cfg, user)
    cfg["_config_path"] = p
    cfg["_env_overrides"] = []
    for env_k, env_v in os.environ.items():
        if not env_k.startswith(ENV_PREFIX):
            continue
        keys = [x.lower() for x in env_k[len(ENV_PREFIX):].split("__") if x]
        if not keys:
            continue
        node = cfg
        ok = True
        for k in keys[:-1]:
            nxt = node.setdefault(k, {})
            if not isinstance(nxt, dict):
                ok = False
                break
            node = nxt
        if ok:
            node[keys[-1]] = _coerce(env_v)
            cfg["_env_overrides"].append(env_k)
    return cfg
