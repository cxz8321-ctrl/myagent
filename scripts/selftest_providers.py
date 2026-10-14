#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""provider 自检：不依赖任何外部库，直接打两个 provider。

用法：python3 scripts/selftest_providers.py [provider名 ...]
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers.registry import build_all           # noqa: E402
from runtime.config import load_config             # noqa: E402
from runtime.secrets import mask                   # noqa: E402


def probe(p, ask: str = "用一句话说你是谁，并报出你的模型名。"):
    print("=" * 64)
    print("provider=%s model=%s type=%s" % (p.name, p.model, type(p).__name__))
    h = p.health().get("data") or {}
    if "has_key" in h:
        print("  key: %s (ref=%s)" % (mask(h.get("key_masked")), h.get("key_ref")))
    if "running" in h:
        print("  serve: %s  models: %s" % (h.get("running"), h.get("models")))
    t0 = time.time()
    r = p.chat([{"role": "user", "content": ask}])
    dt = time.time() - t0
    if r.get("ok"):
        d = r["data"]
        print("  ✓ %.1fs  finish=%s" % (dt, d.get("finish_reason")))
        print("  →", (d["message"].get("content") or "")[:300].replace("\n", " "))
        print("  usage:", d.get("usage"))
    else:
        e = r.get("error") or {}
        print("  ✗ %.1fs  %s: %s" % (dt, e.get("code"), e.get("message")))
        if e.get("detail"):
            print("    detail:", str(e["detail"])[:200])
    return bool(r.get("ok"))


if __name__ == "__main__":
    cfg = load_config()
    providers = build_all(cfg)
    want = sys.argv[1:] or list(providers)
    ok = {}
    for n in want:
        if n not in providers:
            print("没有这个 provider: %s（有：%s）" % (n, ", ".join(providers)))
            continue
        ok[n] = probe(providers[n])
    print("=" * 64)
    print("结果：" + "  ".join("%s=%s" % (k, "OK" if v else "FAIL") for k, v in ok.items()))
    raise SystemExit(0 if all(ok.values()) else 1)
