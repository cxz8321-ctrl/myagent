#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一个极小的 MCP server（stdio），供自检与示例用。

实现了 initialize / tools/list / tools/call 三件事，暴露三个工具：
  - echo(text)              只读（annotations.readOnlyHint）
  - add(a, b)               写入（无 annotations → 按 server 默认级别）
  - delete_thing(name)      危险（annotations.destructiveHint）
"""
import json
import sys

VERSION = "2024-11-05"

TOOLS = [
    {"name": "echo", "description": "原样回显 text", "inputSchema": {
        "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        "annotations": {"readOnlyHint": True}},
    {"name": "add", "description": "把 a 与 b 相加", "inputSchema": {
        "type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
        "required": ["a", "b"]}},
    {"name": "delete_thing", "description": "删除一个东西（示例，不可逆）", "inputSchema": {
        "type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        "annotations": {"destructiveHint": True}},
]


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def result(rid, res):
    send({"jsonrpc": "2.0", "id": rid, "result": res})


def error(rid, code, msg):
    send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}})


def text_result(text):
    return {"content": [{"type": "text", "text": text}], "isError": False}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            result(rid, {"protocolVersion": VERSION, "capabilities": {"tools": {"listChanged": False}},
                         "serverInfo": {"name": "test-mcp", "version": "0.1.0"}})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            result(rid, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                result(rid, text_result("echo: %s" % args.get("text", "")))
            elif name == "add":
                try:
                    result(rid, text_result(str(float(args.get("a", 0)) + float(args.get("b", 0)))))
                except (TypeError, ValueError):
                    result(rid, {"content": [{"type": "text", "text": "参数不是数字"}], "isError": True})
            elif name == "delete_thing":
                result(rid, text_result("已删除（假的）：%s" % args.get("name", "")))
            else:
                error(rid, -32602, "未知工具：%s" % name)
        elif method == "ping":
            result(rid, {})
        elif rid is not None:
            error(rid, -32601, "方法未实现：%s" % method)


if __name__ == "__main__":
    main()
