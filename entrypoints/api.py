#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP API 入口（规格书 3.8）：与 CLI 共用同一内核（同一个 `build_agent` 装配）。

只用标准库：`http.server` + `threading` + `queue`；SSE 走 `text/event-stream` 分块写。

跑法：
  .venv/bin/python -m entrypoints.api                 # 默认 host/port 取 config.api
  .venv/bin/python -m entrypoints.api --port 9000

端点：
  GET  /v1/health    健康 + 内核概况（provider/model/工具数/渠道）
  GET  /v1/tools     可用工具清单
  GET  /v1/sessions  会话列表 + 入口路由映射
  POST /v1/chat      对话；SSE 流：EventBus 事件按 `data: {json}\\n\\n` 推送，结尾一个 final
  鉴权：Authorization: Bearer <token>；token 取 config `api.tokens`（列表）或环境变量
        MASSISTANTGENT_API_TOKEN / MASSISTANTGENT_API_TOKENS；无/错 -> 401
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.factory import build_agent                       # noqa: E402
from entrypoints.channels import get_adapter, list_adapters  # noqa: E402
from runtime.cancel import CancelToken                      # noqa: E402
from runtime.config import load_config                      # noqa: E402
from runtime.log import new_trace_id                        # noqa: E402
from runtime.text import sanitize                           # noqa: E402


def _error(code: str, message: str) -> Dict[str, Any]:
    return {"ok": False, "v": 1, "data": None,
            "error": {"code": code, "message": message}, "meta": {}}


# ---------- 网页版（PWA）静态资源 ----------
WEBUI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui")
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
        ".webmanifest": "application/manifest+json; charset=utf-8",
        ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon",
        ".txt": "text/plain; charset=utf-8"}
# 免鉴权的静态入口（只是外壳；真正的 API 仍要口令）
UI_ROUTES = {"/": "index.html", "/ui": "index.html", "/ui/": "index.html",
             "/manifest.webmanifest": "manifest.webmanifest", "/sw.js": "sw.js"}


def resolve_tokens(cfg: Dict[str, Any]) -> List[str]:
    """token 白名单：config api.tokens 列表 + 环境变量（去重、保序）。"""
    api = dict(cfg.get("api") or {})
    out: List[str] = []
    for t in (api.get("tokens") or []):
        s = str(t).strip()
        if s and s not in out:
            out.append(s)
    for env in ("MASSISTANTGENT_API_TOKEN", "MASSISTANTGENT_API_TOKENS"):
        v = os.environ.get(env)
        if not v:
            continue
        for s in str(v).split(","):
            s = s.strip()
            if s and s not in out:
                out.append(s)
    return out


def _sse_frame(event: Dict[str, Any]) -> bytes:
    return ("data: %s\n\n" % json.dumps(event, ensure_ascii=False)).encode("utf-8")


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "myagent-api/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- 基础 ----------
    def log_message(self, fmt: str, *args: Any) -> None:  # 交给 logger，别刷 stderr
        logger = getattr(self.server, "logger", None)
        if logger is not None:
            try:
                logger.info("api %s" % (fmt % args))
            except Exception:
                pass

    def _authorized(self) -> bool:
        h = self.headers.get("Authorization") or ""
        if not h.startswith("Bearer "):
            return False
        tok = h[7:].strip()
        return bool(tok) and tok in self.server.tokens

    def _send_json(self, code: int, obj: Dict[str, Any]) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _unauthorized(self) -> None:
        body = json.dumps(_error("API_UNAUTHORIZED", "缺少或错误的 Bearer token"),
                          ensure_ascii=False).encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _read_json(self) -> Optional[Dict[str, Any]]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        if not raw:
            return {}
        try:
            v = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return v if isinstance(v, dict) else None

    def _sse_write(self, event: Dict[str, Any]) -> None:
        data = _sse_frame(event)
        self.wfile.write(("%X\r\n" % len(data)).encode("ascii"))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _sse_end(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _serve_ui(self, path: str) -> bool:
        """提供网页版外壳（免鉴权）。命中返回 True。"""
        rel = UI_ROUTES.get(path)
        if rel is None and path.startswith("/ui/"):
            rel = path[len("/ui/"):]
        if rel is None and path.startswith("/icons/"):
            rel = path[len("/"):]
        if rel is None:
            return False
        # manifest 动态生成：名字取自 config.api.app_name（改配置就换名）
        if rel == "manifest.webmanifest":
            return self._send_manifest()
        full = os.path.normpath(os.path.join(WEBUI_DIR, rel))
        if not full.startswith(WEBUI_DIR) or not os.path.isfile(full):
            self._send_json(404, _error("UI_NOT_FOUND", "没有这个静态资源：%s" % path))
            return True
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError as e:
            self._send_json(500, _error("UI_READ_ERROR", str(e)))
            return True
        if rel == "index.html":
            data = data.replace(b"__APP_NAME__", str(self.server.app_name).encode("utf-8"))
        ctype = MIME.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        return True

    def _send_manifest(self) -> bool:
        """按 config.api.app_name 生成 manifest（静态文件只当模板）。"""
        name = str(getattr(self.server, "app_name", None) or "myagent")
        mf = {"name": name, "short_name": name, "description": "本地自建智能体的内核（网页版）",
              "start_url": "/", "scope": "/", "display": "standalone",
              "background_color": "#0e1015", "theme_color": "#111318", "lang": "zh-CN",
              "icons": [
                  {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                  {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                  {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]}
        body = json.dumps(mf, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", MIME[".webmanifest"])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        return True

    # ---------- GET ----------
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in UI_ROUTES or path.startswith("/ui/") or path.startswith("/icons/"):
            if self._serve_ui(path):
                return
        if not self._authorized():
            return self._unauthorized()
        ctx = self.server.ctx
        if path == "/v1/health":
            loop = ctx["loop"]
            return self._send_json(200, {"ok": True, "v": 1, "data": {
                "status": "ok",
                "provider": getattr(loop.provider, "name", None),
                "model": getattr(loop.provider, "model", None),
                "tools": len(ctx["tools"].names()) if ctx.get("tools") else 0,
                "adapters": list_adapters(),
                "app_name": getattr(self.server, "app_name", "myagent"),
                "issuer": getattr(loop, "default_issuer", "owner"),
                "subjects": (ctx.get("subjects").known() if ctx.get("subjects") is not None else {}),
                "endpoints": ["POST /v1/chat", "GET /v1/health", "GET /v1/tools", "GET /v1/sessions"],
                "ts": int(time.time() * 1000)}})
        if path == "/v1/tools":
            tools = ctx["tools"].list() if ctx.get("tools") else []
            return self._send_json(200, {"ok": True, "v": 1, "data": {"tools": [
                {"name": t.get("name"), "permission": t.get("permission"),
                 "source": t.get("source"), "description": t.get("description")} for t in tools]}})
        if path == "/v1/subjects":
            subs = ctx.get("subjects")
            return self._send_json(200, {"ok": True, "v": 1, "data": {
                "default_issuer": getattr(subs, "default_issuer", "owner"),
                "subjects": (subs.known() if subs is not None else {})}, "error": None, "meta": {}})
        if path == "/v1/sessions":
            store = ctx["store"]
            return self._send_json(200, {"ok": True, "v": 1, "data": {
                "scope": self.server.session_scope,
                "routes": store.list_routes(limit=50),
                "sessions": store.list_sessions(limit=50)}})
        return self._send_json(404, _error("API_NOT_FOUND", "未知端点：%s" % path))

    # ---------- POST ----------
    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return self._unauthorized()
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/v1/chat":
            return self._chat()
        if path == "/v1/cancel":
            tok = getattr(self.server, "current_token", None)
            if tok is not None:
                try:
                    tok.cancel("api_cancel")
                except Exception:  # noqa: BLE001
                    pass
            return self._send_json(200, {"ok": True, "v": 1, "data": {"cancelled": tok is not None},
                                         "error": None, "meta": {}})
        return self._send_json(404, _error("API_NOT_FOUND", "未知端点：%s" % path))

    # ---------- 对话（SSE） ----------
    def _chat(self) -> None:
        body = self._read_json()
        if body is None:
            return self._send_json(400, _error("API_BAD_REQUEST", "请求体不是合法 JSON"))

        channel = str(body.get("channel") or "webhook")
        adapter = self.server.adapters.get(channel) or get_adapter(channel)
        if adapter is None:
            return self._send_json(400, _error("API_UNKNOWN_CHANNEL", "未知渠道：%s" % channel))
        try:
            msg = adapter.translate_in(body) or {}
        except Exception as e:  # noqa: BLE001
            return self._send_json(400, _error("API_BAD_MESSAGE", "渠道消息翻译失败：%s" % e))
        text = sanitize(str(msg.get("text") or msg.get("input") or "")).strip()
        if not text:
            return self._send_json(400, _error("API_EMPTY_MESSAGE", "缺少 text"))

        ctx = self.server.ctx
        store, loop = ctx["store"], ctx["loop"]
        user_id = str(msg.get("user_id") or body.get("user_id") or "default")
        session_id = body.get("session_id") or msg.get("session_id")
        if not session_id:
            try:
                session_id = store.route_session(channel, user_id,
                                                 scope=self.server.session_scope)
            except Exception as e:  # noqa: BLE001
                return self._send_json(500, _error("API_SESSION_ERROR", str(e)))

        # 同一内核实例一次只跑一条（内核内部工具并行由 M5 负责）；先拿到锁再开流。
        self.server.run_lock.acquire()
        unsub = None
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()

            q: "queue.Queue" = queue.Queue()
            unsub = ctx["bus"].subscribe(q.put)
            trace_id = new_trace_id()
            token = CancelToken()
            self.server.current_token = token        # 供 POST /v1/cancel 用
            result: Dict[str, Any] = {}
            task = {"input": text, "session_id": session_id, "channel": channel,
                    "user_id": user_id}

            def work() -> None:
                try:
                    result["r"] = loop.run(task, {"trace_id": trace_id, "cancel_token": token})
                except Exception as e:  # noqa: BLE001
                    result["e"] = e

            th = threading.Thread(target=work, daemon=True, name="api-run")
            th.start()
            broken = False
            while th.is_alive() or not q.empty():
                try:
                    ev = q.get(timeout=0.05)
                except queue.Empty:
                    continue
                if ev.get("type") == "final":
                    continue  # 统一由结尾的增强 final 收口
                try:
                    self._sse_write(ev)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    broken = True
                    token.cancel("client_disconnect")
                    break
            th.join(timeout=60)

            r = result.get("r")
            if r is None and result.get("e") is not None:
                r = {"ok": False, "data": None, "meta": {},
                     "error": {"code": "API_RUN_ERROR", "message": str(result["e"])}}
            r = r or {}
            d = r.get("data") or {}
            final_ev = {"type": "final", "ts": int(time.time() * 1000), "payload": {
                "status": d.get("status"), "phase": d.get("phase"), "answer": d.get("answer"),
                "steps": d.get("steps"), "usage": d.get("usage"), "cost": d.get("cost"),
                "session_id": d.get("session_id") or session_id,
                "trace_id": (r.get("meta") or {}).get("trace_id") or trace_id,
                "ok": bool(r.get("ok")), "error": r.get("error")}}
            try:                                     # 渠道出参由适配器纯翻译
                final_ev["reply"] = adapter.translate_out(final_ev)
            except Exception:  # noqa: BLE001
                pass
            if not broken:
                try:
                    self._sse_write(final_ev)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
            self._sse_end()
        finally:
            if unsub is not None:
                try:
                    unsub()
                except Exception:  # noqa: BLE001
                    pass
            self.server.run_lock.release()
            self.server.current_token = None


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: Tuple[str, int], ctx: Dict[str, Any], tokens: List[str],
                 session_scope: str = "user", adapters: Optional[Dict[str, Any]] = None,
                 generated_token: Optional[str] = None, logger=None, app_name: str = "myagent"):
        super().__init__(addr, ApiHandler)
        self.ctx = ctx
        self.tokens = set(tokens)
        self.session_scope = session_scope
        self.adapters = adapters if adapters is not None else {
            n: get_adapter(n) for n in list_adapters()}
        self.generated_token = generated_token
        self.app_name = app_name
        self.logger = logger
        self.run_lock = threading.Lock()
        self.current_token = None       # 当前在跑的那次调用的取消令牌（/v1/cancel 用）


def create_server(cfg: Optional[Dict[str, Any]] = None, ctx: Optional[Dict[str, Any]] = None,
                  host: Optional[str] = None, port: Optional[int] = None) -> ApiServer:
    """造好但不起：方便自检在后台线程里起、跑完就关。"""
    cfg = cfg or load_config()
    api = dict(cfg.get("api") or {})
    ctx = ctx if ctx is not None else build_agent(cfg)
    if host is None:
        host = str(api.get("host") or "127.0.0.1")
    if port is None:
        port = int(api.get("port") or 8765)
    tokens = resolve_tokens(cfg)
    generated = None
    if not tokens:                       # 没配就生成临时 token（打印出来，不进代码）
        generated = secrets.token_urlsafe(18)
        tokens = [generated]
    return ApiServer((host, int(port)), ctx=ctx, tokens=tokens,
                     session_scope=str(api.get("session_scope") or "user"),
                     generated_token=generated, logger=ctx.get("logger"),
                     app_name=str(api.get("app_name") or "myagent"))


def _headless_ask(prompt: Dict[str, Any]) -> str:
    """API 没有真人可问：需确认的操作一律拒绝（安全优先）。"""
    return "deny_once"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="myagent HTTP API（与 CLI 共用内核）")
    ap.add_argument("--host", help="监听地址（默认取 config.api.host）")
    ap.add_argument("--port", type=int, help="监听端口（默认取 config.api.port）")
    ap.add_argument("--token", help="额外放行一个 Bearer token（追加到白名单）")
    ap.add_argument("-P", "--provider", help="provider 名（默认取配置）")
    ap.add_argument("--max-steps", type=int, default=8)
    args = ap.parse_args(argv)

    cfg = load_config()
    ctx = build_agent(cfg, provider_name=args.provider, max_steps=args.max_steps,
                      ask_callback=_headless_ask)
    httpd = create_server(cfg=cfg, ctx=ctx, host=args.host, port=args.port)
    if args.token:
        httpd.tokens.add(args.token)
    addr = "%s:%s" % httpd.server_address[:2]
    loop = ctx["loop"]
    print("myagent API · provider=%s/%s · %s" % (loop.provider.name, loop.provider.model, addr))
    if httpd.generated_token:
        print("未配置 token，已生成临时 token：%s" % httpd.generated_token)
    print("网页版：浏览器打开 http://%s  （显示名：%s）" % (addr, getattr(httpd, "app_name", "myagent")))
    print("端点：POST /v1/chat (SSE) /v1/cancel  GET /v1/health /v1/tools /v1/sessions /v1/subjects")
    print("示例：curl -N -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' "
          "-d '{\"user_id\":\"default\",\"text\":\"你好\"}' http://%s/v1/chat" % addr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
