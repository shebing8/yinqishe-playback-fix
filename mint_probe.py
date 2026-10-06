#!/usr/bin/env python3
"""mint 一个新游客账号，确认它立刻能签出 1 前缀 key，并核对 auth_key 第一段是签发时刻还是过期时刻。"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
import uuid

import api_probe as ap

if len(sys.argv) < 2:
    raise SystemExit("用法: python mint_probe.py https://<当前 API 域名>\n"
                     "域名由 app 自己拼出的 authPath 决定，按版本轮换；取法见 README「取当前 API 域名」")
HOST = sys.argv[1].rstrip("/")
LIVE = ap.cred("new_traveler_token.txt")


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


dev = uuid.uuid4().hex + uuid.uuid4().hex[:8]
rot, body = post(HOST, LIVE, dev, "/api/user/traveler", "{}")
obj = json.loads(body)
print("mint 状态", obj.get("code"), obj.get("msg"))
inner, err = ap.decrypt(LIVE, body)
if err:
    print("外层", json.dumps(obj, ensure_ascii=False)[:200])
    sys.exit(1)
tok = inner.get("token") or rot or LIVE
pay = inner.get("payload") or inner
sub = json.loads(ap.base64.b64decode(tok.split(".")[1] + "===")).get("sub")
print("新账号 sub =", sub)
for k in ("userId", "remainingWatches", "freeWatches", "coins", "gold", "id"):
    if k in inner:
        print(f"  {k} = {inner[k]}")
    elif isinstance(pay, dict) and k in pay:
        print(f"  payload.{k} = {pay[k]}")
open("creds/mint_token.txt", "w", encoding="utf-8").write(tok)
open("creds/mint_dev.txt", "w", encoding="utf-8").write(dev)

now = int(time.time())
for vid in sys.argv[2].split(",") if len(sys.argv) > 2 else ["20787", "20954"]:
    _, rb = ap.get(HOST, tok, f"/api/video/getVideoById?videoId={vid}")
    d, e = ap.decrypt(tok, rb)
    if e:
        print(vid, "无明文", json.dumps(json.loads(rb), ensure_ascii=False)[:120])
        continue
    ak = d.get("authKey") or ""
    m = ap.KEY_RE.match(ak)
    print(f"{vid} flag={m.group(3) if m else '?'} canWatch={d.get('canWatch')} "
          f"reason={d.get('reasonType')} price={d.get('price')} "
          f"f1={ak[:10]} f1-now={(int(ak[:10]) - now) if m else '-'} "
          f"uid尾={ak[43:52] if len(ak)>52 else ak}")
