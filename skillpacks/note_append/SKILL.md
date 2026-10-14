---
name: note_append
description: 往随记文件里追加一行（写入级，需要确认）。参数 text 必填，file 可选。
triggers: [记一下, 备忘, 随记]
permission: write
timeout_ms: 10000
entry: scripts/run.py
tags: [notes]
parameters:
  type: object
  properties:
    text:
      type: string
      description: 要追加的一行内容
    file:
      type: string
      description: 目标文件（默认 data/notes.md）
  required: [text]
---

# note_append —— 追加随记

写入级技能：过闸时要确认（本次允许 / 永久允许 / 拒绝），且受沙盒写根约束
（默认只允许写 `data/` 与 `/tmp`）。
