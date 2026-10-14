#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成网页版（PWA）需要的图标 PNG —— 纯标准库，不装 Pillow。

产物：entrypoints/webui/icons/icon-192.png、icon-512.png
跑法：.venv/bin/python scripts/make_icons.py
"""
from __future__ import annotations

import os
import struct
import zlib

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "entrypoints", "webui", "icons")

BG = (17, 19, 24)          # #111318
BG2 = (26, 32, 46)         # 圆角块内层
ACC = (110, 168, 254)      # #6ea8fe
ACC2 = (139, 92, 246)      # #8b5cf6


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def write_png(path: str, size: int, rows: list) -> None:
    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    png = (b"\x89PNG\r\n\x1a\n"
           + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
           + _chunk(b"IDAT", zlib.compress(raw, 9))
           + _chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def make(size: int) -> list:
    cx = cy = size / 2.0
    r_round = size * 0.22          # 圆角半径
    r_bubble = size * 0.285        # 对话泡半径
    r_tail = size * 0.085
    tx, ty = cx + size * 0.235, cy + size * 0.245     # 泡尾（小圆）
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            px, py = x + 0.5, y + 0.5
            # 圆角矩形背景
            dx = max(abs(px - cx) - (size / 2 - r_round), 0)
            dy = max(abs(py - cy) - (size / 2 - r_round), 0)
            in_card = (dx * dx + dy * dy) <= r_round * r_round
            if not in_card:
                row += bytes((0, 0, 0, 0))
                continue
            # 背景（上浅下深的微渐变）
            t = py / size
            bg = tuple(int(BG[i] + (BG2[i] - BG[i]) * (1 - t) * 0.9) for i in range(3))
            # 对话泡
            d1 = ((px - cx) ** 2 + (py - cy) ** 2) <= r_bubble ** 2
            d2 = ((px - tx) ** 2 + (py - ty) ** 2) <= r_tail ** 2
            if d1 or d2:
                g = py / size
                col = tuple(int(ACC[i] + (ACC2[i] - ACC[i]) * g) for i in range(3))
                row += bytes((col[0], col[1], col[2], 255))
            else:
                row += bytes((bg[0], bg[1], bg[2], 255))
        rows.append(row)
    return rows


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    for size in (192, 512):
        p = os.path.join(OUT, "icon-%d.png" % size)
        write_png(p, size, make(size))
        print("写好了：%s（%d 字节）" % (p, os.path.getsize(p)))


if __name__ == "__main__":
    main()
