# -*- coding: utf-8 -*-
"""MCP 管理器：按配置拉起 MCP server，把它们的工具注册进技能注册表。

配置（config.yaml）：
    mcp:
      enabled: true
      servers:
        fs:
          command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
          env: {KEY: "..."}
          timeout: 30
          default_tier: write          # 该 server 工具的默认级别
          tool_tiers: {read_file: read}
          enabled: true

工具命名：`<server>__<tool>`（避免撞名，也让你一眼看出它从哪来）。
级别映射：优先 tool_tiers → 工具 annotations（readOnlyHint/destructiveHint）→ default_tier。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from mcpclient.client import MCPClient, MCPError
from skills.registry import SkillMeta

TAG = "mcp"


class MCPManager:
    def __init__(self, cfg: Dict[str, Any] = None, registry=None, logger=None):
        m = dict(cfg or {})
        self.enabled = bool(m.get("enabled", True))
        self.servers_cfg: Dict[str, Any] = dict(m.get("servers") or {})
        self.registry = registry
        self.logger = logger
        self.clients: Dict[str, MCPClient] = {}
        self.errors: Dict[str, str] = {}

    # ---------------- 启动 / 注册 ----------------
    def start_all(self) -> Dict[str, Any]:
        report: Dict[str, Any] = {"started": [], "tools": 0, "errors": {}}
        if not self.enabled:
            return report
        for name, sc in self.servers_cfg.items():
            if not (sc or {}).get("enabled", True):
                continue
            try:
                n = self.start(name, sc)
                report["started"].append("%s(%d)" % (name, n))
                report["tools"] += n
            except Exception as e:  # 一个 server 挂了不影响其他
                msg = "%s: %s" % (e.__class__.__name__, e)
                report["errors"][name] = msg
                self.errors[name] = msg
                self._log("mcp_start_failed", server=name, error=msg)
        return report

    def start(self, name: str, sc: Dict[str, Any]) -> int:
        cmd = sc.get("command")
        if isinstance(cmd, str):
            cmd = [cmd]
        if not cmd:
            raise ValueError("缺少 command")
        client = MCPClient(name, cmd, env=sc.get("env"), cwd=sc.get("cwd"),
                           timeout=float(sc.get("timeout", 30)), logger=self.logger)
        client.start()
        self.clients[name] = client
        tools = client.list_tools()
        metas = [self._to_meta(name, t, sc) for t in tools]
        if self.registry is not None:
            self.registry.replace_source("mcp:%s" % name, metas)
        return len(metas)

    def _to_meta(self, server: str, tool: Dict[str, Any], sc: Dict[str, Any]) -> SkillMeta:
        tname = tool.get("name")
        annotations = tool.get("annotations") or {}
        tier = (sc.get("tool_tiers") or {}).get(tname) or _tier_from_annotations(annotations) \
            or sc.get("default_tier") or "write"
        client = self.clients[server]

        def handler(args: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None, _c=client, _t=tname):
            try:
                return _c.call_tool(_t, args or {})
            except MCPError as e:
                return {"ok": False, "content": "MCP 调用失败：%s" % e.message,
                        "meta": {"error": "TOOL_FAILED", "mcp_code": e.code}}
        handler.wants_ctx = True

        schema = tool.get("inputSchema") or {"type": "object", "properties": {}, "required": []}
        return SkillMeta(
            name="%s__%s" % (server, tname),
            description=(tool.get("description") or tool.get("title") or tname)[:500],
            permission=tier,
            parameters=schema,
            handler=handler,
            timeout_ms=int(float(sc.get("timeout", 30)) * 1000),
            source="mcp:%s" % server,
            tags=[TAG, server],
        )

    # ---------------- 热重载 / 关闭 ----------------
    def reload(self, name: Optional[str] = None) -> Dict[str, Any]:
        out: Dict[str, Any] = {"reloaded": {}, "errors": {}}
        targets = [name] if name else list(self.servers_cfg)
        for s in targets:
            sc = self.servers_cfg.get(s)
            if not sc:
                out["errors"][s] = "没有这个 server"
                continue
            try:
                self.stop(s)
                out["reloaded"][s] = self.start(s, sc)
            except Exception as e:  # noqa: BLE001
                out["errors"][s] = "%s: %s" % (e.__class__.__name__, e)
        return out

    def stop(self, name: str) -> None:
        c = self.clients.pop(name, None)
        if c is not None:
            c.close()
        if self.registry is not None:
            self.registry.remove_source("mcp:%s" % name)

    def stop_all(self) -> None:
        for name in list(self.clients):
            self.stop(name)

    # ---------------- 状态 ----------------
    def status(self) -> List[Dict[str, Any]]:
        rows = []
        for name, sc in self.servers_cfg.items():
            c = self.clients.get(name)
            n = len([t for t in (self.registry.names() if self.registry else []) if t.startswith(name + "__")])
            rows.append({"server": name, "enabled": bool((sc or {}).get("enabled", True)),
                         "alive": bool(c and c.alive), "tools": n,
                         "command": " ".join(sc.get("command") or ([] if not isinstance(sc.get("command"), str) else [sc["command"]])),
                         "server_info": (c.server_info if c else {}),
                         "error": self.errors.get(name)})
        return rows

    def _log(self, msg: str, **fields: Any) -> None:
        if self.logger is None:
            return
        try:
            from runtime.log import log_event
            log_event(self.logger, msg, **fields)
        except Exception:
            pass


def _tier_from_annotations(ann: Dict[str, Any]) -> Optional[str]:
    if not ann:
        return None
    if ann.get("destructiveHint"):
        return "danger"
    if ann.get("readOnlyHint"):
        return "read"
    return None
