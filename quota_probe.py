#!/usr/bin/env python3
"""测一件事：反复 getVideoById 现签是否消耗 remainingWatches（用一次性新游客，烧到掉 0 前缀为止）。"""
from __future__ import annotations

import json
import time
import urllib.request
import uuid

import api_probe as ap

argv = __import__("sys").argv
if len(argv) < 2:
    raise SystemExit("用法: python quota_probe.py https://<当前 API 域名> [videoId] [次数]\n"
                     "注意：每跑一次会真开一个游客账号，见 README「凭据与账号额度」")
HOST = argv[1].rstrip("/")
VID = argv[2] if len(argv) > 2 else "20787"
LIMIT = int(argv[3]) if len(argv) > 3 else 60

BASE = ap.cred("new_traveler_token.txt")


def post(host, token, dev, path, body):
    t = str(int(time.time() * 1000)) + "000"
    s = ap.hashlib.md5(t[3:16].encode()).hexdigest()
    req = urllib.request.Request(host + path, data=body.encode(), method="POST", headers={
        "accept": "application/json;charset=UTF-8",
        "content-type": "application/json",
        "User-Agent": "okhttp/4.12.0",
        "t": t, "s": s, "aut": token, "deviceId": dev,
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.headers.get("refresh-authorization"), r.read()


rot, body = post(HOST, BASE, uuid.uuid4().hex + uuid.uuid4().hex[:8], "/api/user/traveler", "{}")
inner, err = ap.decrypt(BASE, body)
if err:
    print("mint 失败", json.dumps(json.loads(body), ensure_ascii=False)[:200])
    raise SystemExit(1)
tok = inner.get("token") or rot or BASE
sub = json.loads(ap.base64.b64decode(tok.split(".")[1] + "===")).get("sub")
print("新游客 sub =", sub, "额度 =", inner.get("remainingWatches"), "/", inner.get("freeWatches"))

first = None
for i in range(1, LIMIT + 1):
    _, rb = ap.get(HOST, tok, f"/api/video/getVideoById?videoId={VID}")
    d, e = ap.decrypt(tok, rb)
    if e:
        print(i, "无明文", rb[:120])
        break
    ak = d.get("authKey") or ""
    m = ap.KEY_RE.match(ak)
    flag = m.group(3) if m else "?"
    if first is None:
        first = ak
    print(f"{i:3d} flag={flag} canWatch={d.get('canWatch')} ts={ak[:10]} 同首次={ak == first}", flush=True)
    if flag == "0":
        print("=> 第", i, "次现签开始掉到 0 前缀：说明 getVideoById 会烧额度")
        break
    time.sleep(0.4)
else:
    print(f"=> {LIMIT} 次现签后仍是 1 前缀：getVideoById 不烧额度")
