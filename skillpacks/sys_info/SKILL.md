---
name: sys_info
description: 报本机配置与状态（只读）：CPU 型号/核数、内核版本、发行版、内存、负载、磁盘余量、GPU 线索。想问"这台机器什么配置/还剩多少"就用它，不用猜。
triggers: [系统, 配置, 负载, 内存, 磁盘, 状态, CPU, 内核, 发行版, 还剩多少, 什么配置]
permission: read
timeout_ms: 15000
entry: scripts/run.py
tags: [ops]
parameters:
  type: object
  properties:
    path:
      type: string
      description: 要看磁盘余量的目录，默认当前目录
  required: []
---

# sys_info —— 本机体检

只读工具，直接返回结果（不只是给命令）：

1. **CPU**：型号、核数、主频（读 `/proc/cpuinfo`）
2. **内核 / 架构**：`platform.release()` / `machine()`
3. **发行版**：`/etc/os-release` 的 PRETTY_NAME
4. **内存**：总量 / 可用 / 占用率 / swap（`/proc/meminfo`）
5. **负载**：1/5/15 分钟（`/proc/loadavg`）
6. **磁盘**：目标目录所在分区的总量与可用（`os.statvfs`）
7. **GPU 线索**：`/proc/driver/nvidia/version`（WSL 里通常没有，会如实说明）

读不到的会明确写"读不到"，不编造。返回体里 `meta.detail` 是结构化原始数据。

脚本从 stdin 收 `{"args": {...}, "ctx": {...}}`，向 stdout 回一行 JSON。
