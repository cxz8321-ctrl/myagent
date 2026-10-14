#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sys_info 技能脚本：报本机情况（真实读取，读不到就如实说读不到）。

返回：负载 / 内存 / 磁盘 / CPU / 内核 / 发行版 / 虚拟化 等。
只读 `/proc`、`/etc/os-release`（沙盒已放行这些只读根）。
"""
import json
import os
import platform
import sys


def _read(path, limit=20000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return None


def cpu_info():
    txt = _read("/proc/cpuinfo") or ""
    model, cores, mhz = None, 0, None
    for line in txt.splitlines():
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if k == "model name" and not model:
            model = v
        elif k == "processor":
            cores += 1
        elif k == "cpu MHz" and not mhz:
            mhz = v
    return {"model": model, "cores": cores or os.cpu_count(),
            "mhz": (round(float(mhz)) if mhz else None)}


def mem_info():
    txt = _read("/proc/meminfo") or ""
    kv = {}
    for line in txt.splitlines():
        k, _, v = line.partition(":")
        kv[k.strip()] = v.strip()
    try:
        total_kb = int(kv.get("MemTotal", "0 kB").split()[0])
        avail_kb = int(kv.get("MemAvailable", "0 kB").split()[0])
        swap_kb = int(kv.get("SwapTotal", "0 kB").split()[0])
    except (ValueError, IndexError):
        return None
    return {"total_mb": total_kb // 1024, "available_mb": avail_kb // 1024,
            "used_pct": round(100 * (1 - avail_kb / total_kb), 1) if total_kb else None,
            "swap_mb": swap_kb // 1024}


def os_info():
    txt = _read("/etc/os-release") or ""
    kv = {}
    for line in txt.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            kv[k.strip()] = v.strip().strip('"')
    return {"distro": kv.get("PRETTY_NAME") or kv.get("NAME"),
            "version": kv.get("VERSION_ID"),
            "kernel": platform.release(),
            "arch": platform.machine(),
            "hostname": platform.node()}


def gpu_info():
    """WSL 里 GPU 线索：/proc/driver/nvidia/version（有独显驱动时才有）。"""
    v = _read("/proc/driver/nvidia/version")
    if v:
        return {"nvidia_driver": v.strip().splitlines()[0][:120]}
    return {"note": "没有 NVIDIA 驱动文件；WSL 里核显/独显信息一般要回 Windows 侧看"}


def main():
    try:
        req = json.loads(sys.stdin.readline() or "{}")
    except ValueError:
        req = {}
    args = req.get("args") or {}
    path = os.path.expanduser(str(args.get("path") or ".")) or "."

    out = {"cpu": cpu_info(), "memory": mem_info(), "os": os_info(), "gpu": gpu_info()}
    try:
        with open("/proc/loadavg") as f:
            out["load"] = f.read().split()[:3]
    except OSError:
        out["load"] = None
    try:
        st = os.statvfs(os.path.abspath(path))
        out["disk"] = {"path": os.path.abspath(path),
                       "total_gb": round(st.f_frsize * st.f_blocks / 2 ** 30, 1),
                       "available_gb": round(st.f_frsize * st.f_bavail / 2 ** 30, 1)}
    except OSError:
        out["disk"] = None

    c, m, o = out["cpu"] or {}, out["memory"] or {}, out["os"] or {}
    text = (
        "CPU：%s（%s 核%s）\n内核：%s（%s）\n发行版：%s\n内存：总 %s MB / 可用 %s MB（用 %s%%）\n"
        "负载(1/5/15)：%s\n磁盘：%s 可用 %s GB / 共 %s GB"
        % (c.get("model"), c.get("cores"), ("，%.0f MHz" % c["mhz"]) if c.get("mhz") else "",
           o.get("kernel"), o.get("arch"), o.get("distro"),
           m.get("total_mb"), m.get("available_mb"), m.get("used_pct"),
           "/".join(out.get("load") or []) or "读不到",
           (out.get("disk") or {}).get("path"), (out.get("disk") or {}).get("available_gb"),
           (out.get("disk") or {}).get("total_gb"))
    )
    print(json.dumps({"ok": True, "content": text, "meta": {"detail": out}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
