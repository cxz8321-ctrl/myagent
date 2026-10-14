# -*- coding: utf-8 -*-
"""费用统计（M5）：token × 单价，单价按 provider 配在 config。

口径：
    cost = prompt_tokens / unit * input_price + completion_tokens / unit * output_price
    unit 默认 1_000_000（"每百万 token 多少钱"，跟大多数厂商报价口径一致）。

单价是**配置里的占位值**，实际以各厂商官网为准（config/config.yaml 的 pricing 段有注释）。
本统计只用于本地可观测，不作对账凭证。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class CostCalculator:
    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        cfg = dict(cfg or {})
        self.currency = cfg.get("currency", "CNY")
        self.unit = int(cfg.get("unit", 1_000_000)) or 1_000_000
        self.providers: Dict[str, Any] = dict(cfg.get("providers") or {})
        self.notes = cfg.get("notes") or ""

    def rates(self, provider: str) -> Dict[str, float]:
        p = self.providers.get(provider) or self.providers.get("default") or {}
        try:
            return {"input": float(p.get("input", 0.0) or 0.0),
                    "output": float(p.get("output", 0.0) or 0.0)}
        except (TypeError, ValueError):
            return {"input": 0.0, "output": 0.0}

    def compute(self, provider: str, usage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        u = usage or {}
        i = int(u.get("prompt_tokens") or 0)
        o = int(u.get("completion_tokens") or 0)
        r = self.rates(provider)
        ic = i / self.unit * r["input"]
        oc = o / self.unit * r["output"]
        return {"provider": provider, "input_tokens": i, "output_tokens": o,
                "input_rate": r["input"], "output_rate": r["output"],
                "input_cost": round(ic, 6), "output_cost": round(oc, 6),
                "total": round(ic + oc, 6), "currency": self.currency, "unit": self.unit}

    @staticmethod
    def add(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """把两次费用相加（同一 currency/unit 口径下）。"""
        a = dict(a or {})
        b = dict(b or {})
        total = float(a.get("total") or 0.0) + float(b.get("total") or 0.0)
        return {
            "input_tokens": int(a.get("input_tokens") or 0) + int(b.get("input_tokens") or 0),
            "output_tokens": int(a.get("output_tokens") or 0) + int(b.get("output_tokens") or 0),
            "total": round(total, 6),
            "currency": b.get("currency") or a.get("currency") or "CNY",
        }

    def table(self) -> Dict[str, Any]:
        """给 CLI /cost 展示的价目表。"""
        rows = []
        for name, p in self.providers.items():
            p = p or {}
            rows.append({"provider": name, "input": float(p.get("input", 0.0) or 0.0),
                         "output": float(p.get("output", 0.0) or 0.0),
                         "note": p.get("note") or p.get("notes") or ""})
        return {"currency": self.currency, "unit": self.unit,
                "unit_label": "每 %d token" % self.unit, "providers": rows, "notes": self.notes}


def format_cost(cost: Optional[Dict[str, Any]]) -> str:
    """一行可读费用，如 `￥0.001200 (in 1.0k/out 0.5k tok)`。"""
    c = cost or {}
    sym = {"CNY": "￥", "USD": "$"}.get(c.get("currency"), "")
    return "%s%.6f (in %s/out %s tok)" % (
        sym, float(c.get("total") or 0.0), _k(c.get("input_tokens")), _k(c.get("output_tokens")))


def _k(n: Any) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "-"
    if n >= 1_000_000:
        return "%.1fM" % (n / 1_000_000.0)
    if n >= 1000:
        return "%.1fk" % (n / 1000.0)
    return str(n)
