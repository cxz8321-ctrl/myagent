# -*- coding: utf-8 -*-
"""最小 MCP 客户端（Model Context Protocol，JSON-RPC 2.0 over stdio）。

不依赖官方 SDK：自己按协议收发，少一层依赖，也便于看清"接上就有工具"这件事。
实现范围（够用即止）：
  - 传输：stdio（子进程），换行分隔的 JSON-RPC
  - 握手：initialize → notifications/initialized
  - 能力：tools/list、tools/call、ping
  - 服务端反向请求（roots/list、sampling/createMessage 等）：礼貌回绝，不阻塞
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "myagent", "version": "0.3.0"}


class MCPError(RuntimeError):
    def __init__(self, code: Any, message: str, detail: Any = None):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.detail = detail


class MCPClient:
    def __init__(self, name: str, command: List[str], env: Optional[Dict[str, str]] = None,
                 cwd: Optional[str] = None, timeout: float = 30.0, logger=None):
        self.name = name
        self.command = list(command)
        self.env_extra = dict(env or {})
        self.cwd = cwd
        self.timeout = float(timeout)
        self.logger = logger
        self.proc: Optional[subprocess.Popen] = None
        self.server_info: Dict[str, Any] = {}
        self.capabilities: Dict[str, Any] = {}
        self._id = 0
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None
        self._alive = False
        self._stderr_tail: List[str] = []

    # ---------------- 生命周期 ----------------
    def start(self) -> Dict[str, Any]:
        env = dict(os.environ)
        env.update(self.env_extra)
        self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1, cwd=self.cwd, env=env)
        self._alive = True
        self._reader = threading.Thread(target=self._read_loop, name="mcp-%s" % self.name, daemon=True)
        self._reader.start()
        threading.Thread(target=self._stderr_loop, name="mcp-err-%s" % self.name, daemon=True).start()
        init = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"roots": {"listChanged": False}},
            "clientInfo": CLIENT_INFO,
        })
        self.server_info = init.get("serverInfo") or {}
        self.capabilities = init.get("capabilities") or {}
        self._notify("notifications/initialized", {})
        return {"server": self.server_info.get("name"), "version": self.server_info.get("version"),
                "protocol": init.get("protocolVersion"), "capabilities": sorted(self.capabilities.keys())}

    def close(self) -> None:
        self._alive = False
        if self.proc is not None:
            try:
                self.proc.stdin and self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None

    @property
    def alive(self) -> bool:
        return bool(self._alive and self.proc is not None and self.proc.poll() is None)

    # ---------------- 能力 ----------------
    def list_tools(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        cursor = None
        for _ in range(50):                    # 分页保护
            params = {"cursor": cursor} if cursor else {}
            r = self._request("tools/list", params)
            out.extend(r.get("tools") or [])
            cursor = r.get("nextCursor")
            if not cursor:
                break
        return out

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        r = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        text, artifacts = _flatten_content(r.get("content") or [])
        return {"ok": not bool(r.get("isError")), "content": text,
                "meta": {"is_error": bool(r.get("isError")), "artifacts": artifacts,
                         "structured": r.get("structuredContent")}}

    def ping(self) -> bool:
        try:
            self._request("ping", {})
            return True
        except Exception:
            return False

    # ---------------- JSON-RPC ----------------
    def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._id += 1
            rid = self._id
            slot: Dict[str, Any] = {"event": threading.Event(), "result": None, "error": None}
            self._pending[rid] = slot
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not slot["event"].wait(self.timeout):
            self._pending.pop(rid, None)
            raise MCPError("MCP_TIMEOUT", "请求超时（%ss）：%s" % (self.timeout, method))
        self._pending.pop(rid, None)
        if slot["error"] is not None:
            raise MCPError(slot["error"].get("code"), slot["error"].get("message"), slot["error"].get("data"))
        return slot["result"] or {}

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, obj: Dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise MCPError("MCP_NOT_STARTED", "客户端未启动")
        line = json.dumps(obj, ensure_ascii=False)
        with self._io_lock:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()

    def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for raw in self.proc.stdout:
            if not self._alive:
                break
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            slot = self._pending.get(msg["id"])
            if slot:
                slot["result"] = msg.get("result")
                slot["error"] = msg.get("error")
                slot["event"].set()
            return
        if "method" in msg and "id" in msg:      # 服务端反向请求
            self._reply_unsupported(msg["id"], msg.get("method"))
        # 其余是通知，忽略

    def _reply_unsupported(self, rid: Any, method: Any) -> None:
        try:
            self._send({"jsonrpc": "2.0", "id": rid,
                        "error": {"code": -32601, "message": "客户端不支持：%s" % method}})
        except Exception:
            pass

    def _stderr_loop(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for line in self.proc.stderr:
            self._stderr_tail.append(line.rstrip()[:300])
            self._stderr_tail[:] = self._stderr_tail[-20:]

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail)


def _flatten_content(content: List[Dict[str, Any]]) -> Any:
    parts: List[str] = []
    artifacts: List[str] = []
    for item in content or []:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        t = item.get("type")
        if t == "text":
            parts.append(str(item.get("text", "")))
        elif t == "image":
            artifacts.append("image:%s" % item.get("mimeType", "?"))
            parts.append("[image %s]" % item.get("mimeType", "?"))
        elif t == "resource":
            res = item.get("resource") or {}
            artifacts.append("resource:%s" % res.get("uri", "?"))
            parts.append(str(res.get("text") or res.get("uri") or "[resource]"))
        else:
            parts.append(json.dumps(item, ensure_ascii=False)[:500])
    return "\n".join(parts), artifacts
