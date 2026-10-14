#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""note_append 技能脚本：把一行文字追加到目标文件。"""
import json
import os
import sys
import time


def main():
    try:
        req = json.loads(sys.stdin.readline() or "{}")
    except ValueError:
        req = {}
    args = req.get("args") or {}
    text = str(args.get("text") or "").strip()
    if not text:
        print(json.dumps({"ok": False, "content": "text 必填"}, ensure_ascii=False))
        return
    path = os.path.expanduser(str(args.get("file") or "data/notes.md"))
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    with open(path, "a", encoding="utf-8") as f:
        f.write("- [%s] %s\n" % (stamp, text))
    print(json.dumps({"ok": True, "content": "已追加到 %s" % path,
                      "meta": {"artifacts": [path]}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
