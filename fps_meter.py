#!/usr/bin/env python3
"""视频层出帧节奏计：按 0.5s 桶统计 SurfaceView 新帧数，无截图。

用法: python fps_meter.py [时长秒] [层关键字] [--both]
只统计 actualPresentTime（第 2 列，纳秒单调时钟），丢弃明显越界的行。
"""
import os
import re
import subprocess
import sys
import time

DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
KEY = sys.argv[2] if len(sys.argv) > 2 else "BLAST"
BUCKET = 0.5


def sh(args):
    e = dict(os.environ)
    e["MSYS_NO_PATHCONV"] = "1"
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=e).stdout


def pick_layer():
    out = sh(["adb", "shell", "dumpsys", "SurfaceFlinger", "--list"])
    cands = [l.strip() for l in out.splitlines()
             if "yinqishe_app" in l and "SurfaceView" in l and "Background" not in l]
    for c in cands:
        if KEY.lower() in c.lower():
            return c
    return cands[0] if cands else None


def present_times(name):
    # 层名含括号，必须再包一层单引号交给设备端 shell
    out = sh(["adb", "shell", "dumpsys", "SurfaceFlinger", "--latency",
              "'" + name + "'"])
    ts = []
    for line in out.splitlines():
        m = re.match(r"^(\d{6,})\s+(\d{6,})\s+(\d{6,})$", line.strip())
        if m:
            v = int(m.group(2))
            if 0 < v < 10 ** 14:          # 纳秒单调时钟，越界的是占位行
                ts.append(v)
    return ts


def main():
    name = pick_layer()
    if not name:
        sys.exit("[!] 找不到视频层")
    print(f"[*] layer = {name}", flush=True)

    seen = set(present_times(name))
    buckets, cur, t_end = {}, 0.0, time.time() + DUR
    last_pull = time.time()
    while time.time() < t_end:
        new = [t for t in present_times(name) if t not in seen]
        seen.update(new)
        dt = time.time() - last_pull
        idx = int(cur / BUCKET)
        buckets[idx] = buckets.get(idx, 0) + len(new)
        cur += dt
        last_pull = time.time()
        time.sleep(0.35 - min(0.3, max(0.0, dt - 0.3)))

    frames = sum(buckets.values())
    span = cur
    print(f"[*] 采样 {span:.1f}s  新帧 {frames}  平均 {frames/span:.1f} fps")
    rate = [v / BUCKET for v in buckets.values()]
    if rate:
        low = [r for r in rate if r < 24]
        print(f"[*] 每 {BUCKET}s 桶 fps: {[round(r) for r in rate]}")
        print(f"[*] 低于 24fps 的桶: {len(low)}/{len(rate)}  最低 {min(rate):.0f}fps")
    # 相邻帧间隔
    all_ts = sorted(present_times(name))
    gaps = [(b - a) / 1e6 for a, b in zip(all_ts, all_ts[1:]) if 0 < b - a < 5e9]
    if gaps:
        gaps.sort()
        print(f"[*] 末窗帧间隔 中位 {gaps[len(gaps)//2]:.1f}ms  p90 {gaps[int(len(gaps)*.9)]:.1f}ms "
              f"最大 {gaps[-1]:.0f}ms  >120ms 共 {sum(1 for g in gaps if g > 120)} 次")


if __name__ == "__main__":
    main()
