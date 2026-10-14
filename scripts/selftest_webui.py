#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页版（PWA）自检：静态资源、鉴权边界、路径穿越防护、取消端点。

用**假内核**起一个真 HTTP 服务（不加载模型、不调外部模型）。
跑法：.venv/bin/python scripts/selftest_webui.py
"""
from __future__ import annotations

import http.client
import json
import os
import struct
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from entrypoints.api import ApiServer             # noqa: E402

TOKEN = "selftest-token"
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("✓" if cond else "✗", name, ("  — " + str(extra)) if (extra and not cond) else ""))


class _FakeTools:
    def names(self): return ["calc", "sys_info"]
    def list(self):
        return [{"name": "calc", "permission": "read", "source": "builtin", "description": "算数"},
                {"name": "sys_info", "permission": "read", "source": "file:x", "description": "体检"}]


class _FakeProvider:
    name = "fake"
    model = "fake-1"


class _FakeLoop:
    provider = _FakeProvider()


class _FakeStore:
    def list_routes(self, limit=50): return []
    def list_sessions(self, limit=50): return [{"id": "s1", "user_id": "u", "channel": "cli",
                                                "message_count": 0}]


CTX = {"loop": _FakeLoop(), "tools": _FakeTools(), "store": _FakeStore(), "bus": None}
srv = ApiServer(("127.0.0.1", 0), CTX, [TOKEN], session_scope="user")
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def req(method, path, body=None, auth=True):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    headers = {}
    if auth:
        headers["Authorization"] = "Bearer " + TOKEN
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=headers)
    r = c.getresponse()
    raw = r.read()
    c.close()
    return r.status, dict(r.getheaders()), raw


# ================= 1. 静态资源 =================
print("\n[1] 静态资源（网页外壳，免鉴权）")
code, hdrs, body = req("GET", "/", auth=False)
check("GET / 返回 HTML", code == 200 and b"<!doctype html>" in body.lower(), code)
check("首页引了 manifest / 样式 / 脚本",
      all(k in body for k in (b"/manifest.webmanifest", b"/ui/style.css", b"/ui/app.js")))
check("首页有移动端 viewport 与 PWA meta", b'name="viewport"' in body and b"apple-mobile-web-app-capable" in body)

code, hdrs, body = req("GET", "/ui/app.js", auth=False)
check("GET /ui/app.js 是 JS", code == 200 and "javascript" in hdrs.get("Content-Type", ""), hdrs.get("Content-Type"))
check("前端会解析 SSE（data: 帧）", b"data:" in body and b"/v1/chat" in body)

code, hdrs, body = req("GET", "/ui/style.css", auth=False)
check("GET /ui/style.css 是 CSS", code == 200 and "text/css" in hdrs.get("Content-Type", ""))

code, hdrs, body = req("GET", "/manifest.webmanifest", auth=False)
mf = {}
try:
    mf = json.loads(body)
except ValueError:
    pass
check("manifest 是合法 JSON", bool(mf))
check("manifest 具备 PWA 必备字段",
      mf.get("name") and mf.get("start_url") == "/" and mf.get("display") == "standalone",
      mf)
sizes = [i.get("sizes") for i in (mf.get("icons") or [])]
check("manifest 声明 192 与 512 图标", "192x192" in sizes and "512x512" in sizes, sizes)

code, hdrs, body = req("GET", "/sw.js", auth=False)
check("Service Worker 可访问", code == 200 and b"addEventListener" in body)

for size in (192, 512):
    code, hdrs, body = req("GET", "/icons/icon-%d.png" % size, auth=False)
    ok_magic = body[:8] == b"\x89PNG\r\n\x1a\n"
    dims = struct.unpack(">II", body[16:24]) if ok_magic else (0, 0)
    check("图标 icon-%d.png 正确（PNG %dx%d）" % (size, size, size),
          code == 200 and "image/png" in hdrs.get("Content-Type", "") and ok_magic and dims == (size, size),
          (code, dims))

# ================= 2. 鉴权边界 =================
print("\n[2] 鉴权边界（外壳公开、API 要口令）")
code, _, body = req("GET", "/", auth=False)
check("静态首页免鉴权", code == 200)
code, _, _ = req("GET", "/v1/health", auth=False)
check("GET /v1/health 无口令 → 401", code == 401, code)
code, _, body = req("GET", "/v1/health", auth=True)
d = {}
try:
    d = json.loads(body).get("data") or {}
except ValueError:
    pass
check("带口令 → 200 且带内核信息", code == 200 and d.get("provider") == "fake" and d.get("tools") == 2, d)
code, _, _ = req("POST", "/v1/chat", {"text": "hi"}, auth=False)
check("POST /v1/chat 无口令 → 401", code == 401, code)

# ================= 3. 路径穿越防护 =================
print("\n[3] 路径穿越防护（用原始路径，绕开客户端归一化）")
for bad in ("/ui/../api.py", "/ui/../../config/config.yaml", "/icons/../../api.py"):
    code, _, _ = req("GET", bad, auth=False)
    check("越界路径被拒：%s" % bad, code in (401, 404), code)
code, _, _ = req("GET", "/ui/nope.js", auth=False)
check("不存在的静态资源 → 404", code == 404, code)

# ================= 4. 取消端点 =================
print("\n[4] /v1/cancel")
code, _, body = req("POST", "/v1/cancel", {}, auth=True)
d = {}
try:
    d = json.loads(body)
except ValueError:
    pass
check("没有在跑的任务时也能安全返回", code == 200 and d.get("ok") is True, d)
code, _, _ = req("POST", "/v1/cancel", {}, auth=False)
check("取消端点同样要口令", code == 401, code)

srv.shutdown()
print("\n" + "=" * 52)
print("网页版自检：%d/%d 通过" % (len(PASS), len(PASS) + len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - %s" % f)
sys.exit(1 if FAIL else 0)
