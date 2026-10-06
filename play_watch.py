#!/usr/bin/env python3
"""长播监控：每 interval 秒唤出控制条读播放位置，对比墙钟时间。
位置落后墙钟的累计量就是卡顿/冻结的时长，无截图。"""
from __future__ import annotations

import sys
import time

import ui_probe as up


def main(minutes=12.0, interval=45.0):
    t0 = time.time()
    p0 = None
    last = None
    n = int(minutes * 60 / interval)
    for i in range(n):
        up.sh(up.DEV + ["shell", "input", "tap", "540", "400"])
        time.sleep(0.8)
        pos = None
        for lab, _, _ in up.nodes():
            m = up.TIME.search(lab)
            if m:
                g = [int(x) for x in m.groups() if x is not None]
                if len(g) == 4:                      # mm:ss / mm:ss
                    pos = g[0] * 60 + g[1]
                    tot = g[2] * 60 + g[3]
                else:                                # hh:mm:ss / hh:mm:ss
                    pos = g[0] * 3600 + g[1] * 60 + g[2]
                    tot = g[3] * 3600 + g[4] * 60 + g[5]
                break
        wall = time.time() - t0
        if pos is None:
            print(f"[{wall:7.1f}s] 无进度文本", flush=True)
        else:
            if p0 is None:
                p0, base_wall = pos, wall
            drift = (wall - base_wall) - (pos - p0)   # >0 表示播放比墙钟慢
            step = None if last is None else pos - last
            print(f"[{wall:7.1f}s] pos={pos}s 步进={step} 累计落后={drift:.1f}s "
                  f"总时长={tot}s", flush=True)
            last = pos
        time.sleep(max(0.0, interval - 3.0))
    print("采样结束", flush=True)


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 12.0,
         float(sys.argv[2]) if len(sys.argv) > 2 else 45.0)
