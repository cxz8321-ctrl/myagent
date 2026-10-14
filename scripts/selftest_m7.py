#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M7 入口层自检（规格书 3.8）：HTTP API / SSE / 鉴权 / 渠道适配器 / 会话路由。

**全程不调外部模型**：用假内核（FakeLoop）+ 假 provider，直接在后台线程起真 HTTP 服务。
覆盖：
  ① HTTP API：健康 / 工具 / 会话端点 + Bearer 鉴权（401/200）
  ② SSE 流式：`data: {json}\\n\\n` 可解析，结尾一个 final
  ③ CLI 与 API 共用内核：同一个 `agent.factory.build_agent` 装配，服务用注入的同一实例
  ④ 渠道适配器：只做翻译、无副作用；加新渠道内核零改动
  ⑤ 会话路由：同一用户 HTTP 与 CLI 落到同一 session_id（记忆连续），并持久化到 sqlite
跑法：.venv/bin/python scripts/selftest_m7.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent.factory as factory                              # noqa: E402
import entrypoints.api as api_mod                            # noqa: E402
import entrypoints.cli as cli_mod                            # noqa: E402
from agent.events import EventBus                            # noqa: E402
from entrypoints.channels import ChannelAdapter, get_adapter, list_adapters  # noqa: E402
from memory import MemoryStore                               # noqa: E402
from providers.base import envelope                          # noqa: E402
from runtime.config import load_config                       # noqa: E402
from skills import builtin as builtin_tools                  # noqa: E402
from skills.registry import ToolRegistry                     # noqa: E402

TMP = tempfile.mkdtemp(prefix="m7test-")
DB = os.path.join(TMP, "m7.db")
TOKEN = "secret-token"
PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("✓" if cond else "✗", name,
                         ("  — " + str(extra)) if (extra and not cond) else ""))


# ---------------- 假内核（不调模型） ----------------
class FakeProvider:
    name = "fake"
    model = "fake-1"

    def health(self):
        return envelope(True, {"provider": self.name, "model": self.model})


class FakeLoop:
    """最小内核替身：发 EventBus 事件 + 落一条会话 + 回信封。"""

    def __init__(self, bus, store):
        self.bus = bus
        self.store = store
        self.provider = FakeProvider()
        self.tools = None

    def run(self, task, ctx=None):
        ctx = ctx or {}
        sid = task.get("session_id") or "s-fake"
        tid = ctx.get("trace_id")
        self.store.ensure_session(sid, channel=task.get("channel", "cli"),
                                  user_id=task.get("user_id", "default"))
        self.store.append(sid, {"role": "user", "content": task.get("input", "")})
        self.bus.emit("thought", {"text": "想一下"}, trace_id=tid)
        self.bus.emit("tool_call", {"name": "calc", "arguments": {"expr": "1+1"}}, trace_id=tid)
        self.bus.emit("tool_result", {"ok": True, "content": "2", "meta": {}}, trace_id=tid)
        self.bus.emit("step", {"step": 1, "action": "final"}, trace_id=tid)
        answer = "收到：" + str(task.get("input", ""))
        self.store.append(sid, {"role": "assistant", "content": answer})
        self.bus.emit("final", {"status": "done", "answer": answer, "phase": "done",
                                "cost": {"total": 0.0}}, trace_id=tid)
        return envelope(True, data={"status": "done", "termination": "done", "phase": "done",
                                    "answer": answer, "steps": 1,
                                    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                                    "cost": {"total": 0.0}, "session_id": sid,
                                    "trace_id": tid}, meta={"trace_id": tid})


# ---------------- HTTP 小工具（标准库） ----------------
BASE = ""


def http_get(path, token=None):
    req = urllib.request.Request(BASE + path)
    if token is not None:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw}


def http_chat(body, token=None):
    req = urllib.request.Request(BASE + "/v1/chat",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    if token is not None:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            status, raw, events = r.status, b"", []
            for line in r:
                raw += line
                s = line.strip()
                if s.startswith(b"data:"):
                    events.append(json.loads(s[5:].strip().decode("utf-8")))
            return status, events, raw
    except urllib.error.HTTPError as e:
        return e.code, [], e.read()


def main() -> int:
    global BASE
    print("M7 自检（不调外部模型）：HTTP API / SSE / 鉴权 / 渠道适配器 / 会话路由")

    bus = EventBus()
    store = MemoryStore(db_path=DB)
    tools = ToolRegistry()
    tools.register_many(builtin_tools.BUILTIN)
    loop = FakeLoop(bus, store)
    CTX = {"bus": bus, "store": store, "tools": tools, "loop": loop, "logger": None}

    cfg = {"api": {"tokens": [TOKEN], "session_scope": "user"}}
    httpd = api_mod.create_server(cfg=cfg, ctx=CTX, host="127.0.0.1", port=0)
    BASE = "http://127.0.0.1:%d" % httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()

    try:
        # ============ 1. 鉴权 ============
        print("\n[1] 鉴权：无/错 token -> 401；对 -> 200")
        code, body = http_get("/v1/health")
        check("无 token -> 401", code == 401
              and (body.get("error") or {}).get("code") == "API_UNAUTHORIZED", (code, body))
        code, _ = http_get("/v1/health", token="wrong")
        check("错 token -> 401", code == 401, code)
        code, body = http_get("/v1/health", token=TOKEN)
        check("对 token -> 200 且 health ok", code == 200
              and (body.get("data") or {}).get("status") == "ok", (code, body))
        check("health 带 provider/工具数/渠道",
              (body.get("data") or {}).get("provider") == "fake"
              and (body["data"] or {}).get("tools") == len(tools.names())
              and "webhook" in (body["data"].get("adapters") or []), body)
        os.environ["MASSISTANTGENT_API_TOKEN"] = "envtok"
        check("token 也能从环境变量读", "envtok" in api_mod.resolve_tokens({}))
        os.environ.pop("MASSISTANTGENT_API_TOKEN", None)

        print("\n[1b] 只读端点：/v1/tools、/v1/sessions")
        code, body = http_get("/v1/tools", token=TOKEN)
        names = [t["name"] for t in (body.get("data") or {}).get("tools", [])]
        check("GET /v1/tools 列出工具", code == 200 and len(names) > 0, (code, names[:5]))
        check("工具项含 name/permission/source",
              all({"name", "permission", "source"} <= set(t) for t in body["data"]["tools"]))
        code, body = http_get("/v1/sessions", token=TOKEN)
        check("GET /v1/sessions 带 sessions + routes",
              code == 200 and "sessions" in body["data"] and "routes" in body["data"], body)

        # ============ 2. SSE 流式 ============
        print("\n[2] SSE：data: {json}\\n\\n 可解析 + 结尾 final")
        code, events, raw = http_chat({"user_id": "zhe", "text": "你好呀"}, token=TOKEN)
        check("POST /v1/chat -> 200", code == 200, code)
        check("响应体是 SSE 分块（多个 data: 帧）",
              raw.count(b"data:") >= 3 and b"\n\n" in raw, raw[:120])
        lines = [l for l in raw.split(b"\n") if l.strip().startswith(b"data:")]
        check("每帧都是 `data: {json}` 且可 JSON 解析",
              len(lines) >= 3 and all(isinstance(json.loads(l.strip()[5:].decode("utf-8")), dict)
                                      for l in lines))
        types = [e.get("type") for e in events]
        check("事件序列含 thought/tool_call/tool_result/step",
              {"thought", "tool_call", "tool_result", "step"}.issubset(set(types)), types)
        fin = events[-1]
        check("结尾恰有一个 final", types and types[-1] == "final" and types.count("final") == 1,
              types)
        p = fin.get("payload") or {}
        check("final 带 status/answer/session_id/steps/usage",
              p.get("status") == "done" and "收到：你好呀" in (p.get("answer") or "")
              and p.get("session_id") and p.get("steps") == 1 and p.get("usage"), p)
        check("final 带渠道出参 reply（适配器纯翻译）",
              (fin.get("reply") or {}).get("type") == "reply"
              and fin["reply"].get("text") == p.get("answer")
              and fin["reply"].get("session_id") == p.get("session_id"), fin.get("reply"))
        sid_http = p.get("session_id")

        # ============ 3. 共用内核 ============
        print("\n[3] CLI 与 API 共用同一内核（同一 build_agent 装配）")
        check("api 与 cli 用的是同一个 build_agent",
              api_mod.build_agent is factory.build_agent and cli_mod.build_agent is factory.build_agent)
        check("服务用的就是注入的那一份内核实例",
              httpd.ctx is CTX and CTX["loop"] is loop and httpd.ctx["store"] is store)
        check("SSE 的答案确实出自这份内核", "收到：你好呀" in (p.get("answer") or ""))

        # ============ 4. 渠道适配器只做翻译 ============
        print("\n[4] 渠道适配器：只做翻译、无副作用，新渠道内核零改动")
        ad = get_adapter("webhook")
        norm = ad.translate_in({"user_id": "u1", "text": "hi"})
        check("translate_in 归一成内部消息形状", norm.get("type") == "message"
              and norm.get("channel") == "webhook" and norm.get("user_id") == "u1"
              and norm.get("text") == "hi", norm)
        before = store.count_messages(sid_http)
        ad.translate_in({"user_id": "u1", "text": "再来一条"})
        check("translate_in 无副作用（不碰记忆/内核）", store.count_messages(sid_http) == before)
        out = ad.translate_out({"type": "final",
                                "payload": {"answer": "hi", "status": "done", "session_id": "s1"}})
        check("translate_out 只做形状转换", out.get("type") == "reply" and out.get("text") == "hi"
              and out.get("session_id") == "s1", out)
        check("非 reply 事件不生成出参/仅做映射",
              ad.translate_out({"type": "whatever", "payload": {}}) is None
              and (ad.translate_out({"type": "thought", "payload": {"text": "t"}}) or {}).get("type") == "thought")
        check("适配器注册表可列出", "webhook" in list_adapters())

        class EchoAdapter(ChannelAdapter):
            name = "echo"

            def translate_in(self, raw):
                return {"type": "message", "channel": "echo",
                        "user_id": str(raw.get("from") or "x"),
                        "text": "ECHO " + str(raw.get("body") or ""),
                        "session_id": None, "meta": {}}

            def translate_out(self, event):
                p = event.get("payload") or {}
                return {"type": "reply", "channel": "echo", "text": p.get("answer") or ""}

        httpd.adapters["echo"] = EchoAdapter()          # 只加适配器，内核一行没动
        code, events, _ = http_chat({"channel": "echo", "from": "u9", "body": "ping"}, token=TOKEN)
        fp = (events[-1].get("payload") or {}) if events else {}
        check("新渠道适配器直接可用（内核零改动）",
              code == 200 and "ECHO ping" in (fp.get("answer") or "")
              and (events[-1].get("reply") or {}).get("channel") == "echo", (code, fp))
        code, _, _ = http_chat({"channel": "nope", "text": "x"}, token=TOKEN)
        check("未知渠道 -> 400", code == 400, code)

        # ============ 5. 会话路由：跨入口连续 ============
        print("\n[5] 会话路由：(channel,user_id) -> session_id，跨入口共享 + 落 sqlite")
        sid_cli = store.route_session("cli", "zhe", scope="user")
        check("HTTP 与 CLI 同一用户落到同一 session_id", sid_cli == sid_http, (sid_cli, sid_http))
        check("路由表里能查到这条映射",
              store.session_for_route("cli", "zhe", scope="user") == sid_http)
        contents = [m.get("content") for m in store.recent(sid_cli, n=20)]
        check("CLI 侧看得到 HTTP 聊过的内容（记忆连续）",
              "你好呀" in contents and any(c and "收到：你好呀" in c for c in contents), contents)
        # 不同用户互不串
        sid_other = store.route_session("cli", "other", scope="user")
        check("不同用户各自一条会话", sid_other != sid_http)
        # channel 作用域：按入口分开
        s_http_ch = store.route_session("webhook", "zhe", scope="channel")
        s_cli_ch = store.route_session("cli", "zhe", scope="channel")
        check("scope=channel 时不同入口各一条（可选口径）", s_http_ch != s_cli_ch)

        # ============ 6. 配置 ============
        print("\n[6] config 有 api 段")
        loaded = load_config()
        api = loaded.get("api") or {}
        check("api 段含 host/port/tokens/session_scope",
              all(k in api for k in ("host", "port", "tokens", "session_scope")), api)

        # 关服务后再验持久化
        httpd.shutdown()
        httpd.server_close()

        store.close()
        store2 = MemoryStore(db_path=DB)
        check("路由映射持久化到 sqlite（重开仍在）",
              store2.session_for_route("cli", "zhe", scope="user") == sid_http
              and store2.session_for_route("webhook", "zhe", scope="channel") == s_http_ch)
        check("重开后会话消息仍在（记忆连续）",
              any(m.get("content") == "你好呀" for m in store2.recent(sid_http, n=20)))
        store2.close()
    finally:
        try:
            httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass

    print("\n" + "=" * 52)
    print("M7 自检：%d/%d 通过" % (len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - %s" % f)
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
