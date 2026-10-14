# -*- coding: utf-8 -*-
"""evals 层：任务集 / runner / 打分器 / 回归报告（M4）。

模块：
    evals.loader  任务集加载（ASSISTANTML/JSON → 统一 task）
    evals.runner  批量运行 + 轨迹落盘 + CLI
    evals.scorer  规则判分 + 可选模型判分
    evals.report  基线对比与回归报告
    evals.tools   评测专用工具（危险级 run_command，仅评测时挂上）
"""
