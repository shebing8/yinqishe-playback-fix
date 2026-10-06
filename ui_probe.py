#!/usr/bin/env python3
"""无截图驱动：adb 点击 + uiautomator 文本 + gadget 日志尾部。"""
from __future__ import annotations

import html
import re
import subprocess
import sys
import time

DEV = ["adb", "-s", "55434d6"]
LOG = "/data/data/com.example.yinqishe_app/gadget_play.log"
NODE = re.compile(r"<node\b[^>]*>")
BOUNDS = re.compile(r'\bbounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')
TIME = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*/\s*(\d{1,2}):(\d{2})(?::(\d{2}))?")


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw).stdout


def attr(s, name):
    m = re.search(r'\b%s="([^"]*)"' % name, s)
    return html.unescape(m.group(1)).replace("\n", " / ").strip() if m else ""


def nodes():
    sh(DEV + ["shell", "uiautomator", "dump", "/sdcard/ui_n.xml"])
    sh(DEV + ["pull", "/sdcard/ui_n.xml", "ui_n.xml"])
    out = []
    xml = open("ui_n.xml", encoding="utf-8", errors="replace").read()
    for m in NODE.finditer(xml):
        s = m.group(0)
        b = BOUNDS.search(s)
        if not b:
            continue
        x1, y1, x2, y2 = map(int, b.groups())
        lab = attr(s, "text") or attr(s, "content-desc")
        if lab:
            out.append((lab, (x1 + x2) // 2, (y1 + y2) // 2))
    return out


def show(limit=40):
    for lab, x, y in nodes()[:limit]:
        print(f"({x},{y}) {lab[:70]}")


def tap(x, y, wait=2.0):
    sh(DEV + ["shell", "input", "tap", str(x), str(y)])
    time.sleep(wait)


def find(sub, wait=2.0, nth=0):
    hits = [n for n in nodes() if sub in n[0]]
    for i, (lab, x, y) in enumerate(hits):
        print(f"  hit{i} ({x},{y}) {lab[:60]}")
    if not hits:
        print(f"未找到包含 {sub} 的节点")
        return False
    lab, x, y = hits[min(nth, len(hits) - 1)]
    print(f"tap ({x},{y}) {lab[:60]}")
    tap(x, y, wait)
    return True


def progress(times=5, gap=6.0):
    """轮询播放进度文本，证明位置是否越过 12s。"""
    for i in range(times):
        cur = None
        for lab, _, _ in nodes():
            m = TIME.search(lab)
            if m:
                cur = (m.group(0), m.captures)
                break
            if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", lab):
                cur = (lab, None)
                break
        print(f"[{i}] {cur[0] if cur else '无进度文本'}")
        if i + 1 < times:
            time.sleep(gap)


def log(n=12, grep=None):
    txt = sh(DEV + ["shell", "su", "-c", f"tail -{n} {LOG}"])
    for line in txt.splitlines():
        if grep is None or re.search(grep, line):
            print(line)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    cmd = sys.argv[1]
    if cmd == "nodes":
        show(int(sys.argv[2]) if len(sys.argv) > 2 else 40)
    elif cmd == "tap":
        tap(int(sys.argv[2]), int(sys.argv[3]),
            float(sys.argv[4]) if len(sys.argv) > 4 else 2.0)
        show(12)
    elif cmd == "find":
        find(sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 2.0,
             int(sys.argv[4]) if len(sys.argv) > 4 else 0)
    elif cmd == "progress":
        progress(*(int(sys.argv[i]) if len(sys.argv) > i else 5
                   for i in (2, 3)))
    elif cmd == "log":
        log(int(sys.argv[2]) if len(sys.argv) > 2 else 12,
            sys.argv[3] if len(sys.argv) > 3 else None)
