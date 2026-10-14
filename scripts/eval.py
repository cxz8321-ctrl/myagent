#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M4 评测入口：一条命令跑完全量任务集并出回归报告。

    .venv/bin/python scripts/eval.py                  # 跑全量（默认 deepseek）+ 出报告
    .venv/bin/python scripts/eval.py --report         # 不调模型，只看最近一次 run 与基线对比
    .venv/bin/python scripts/eval.py --only calc --limit 3
    .venv/bin/python scripts/eval.py --list

轨迹落盘：evals/runs/<时间戳>.json；基线：evals/baseline.json（首轮自动写）。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from evals.runner import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
