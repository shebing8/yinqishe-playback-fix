#!/usr/bin/env python3
"""扫一批 videoId，看服务端在什么条件下才发 1 前缀（正片）authKey。"""
from __future__ import annotations

import json
import sys

import api_probe as ap

HOST = sys.argv[1]
IDS = sys.argv[2].split(",")
token = ap.current_token()
show_raw = len(sys.argv) > 3

for vid in IDS:
    try:
        rotated, body = ap.get(HOST, token, f"/api/video/getVideoById?videoId={vid}")
        inner, err = ap.decrypt(token, body)
    except Exception as e:
        print(f"{vid} ERR {e}", flush=True)
        continue
    if not inner:
        print(f"{vid} 无明文 {str(err)[:80]}", flush=True)
        continue
    ak = inner.get("authKey") or ""
    m = ap.KEY_RE.match(ak)
    flag = m.group(3) if m else "?"
    keep = ("canWatch", "price", "forceAd", "reasonType", "previewDir",
            "previewVttUrl", "playTime", "videoMark", "size", "height")
    row = {k: inner.get(k) for k in keep}
    print(f"{vid} flag={flag} {json.dumps(row, ensure_ascii=False)[:300]}", flush=True)
    if show_raw:
        print("   keys:", ",".join(sorted(inner.keys())), flush=True)
