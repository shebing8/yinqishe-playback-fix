#!/usr/bin/env python3
"""本地复现 app 的签名请求，探一把哪些 videoId 的 authKey 是 1 前缀（正片）。

只读接口，不改设备状态。token 从 creds/token.txt 读，host 从设备日志学。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.request

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

def cred(name):
    """凭据文件都在 creds/（已 gitignore），内容见 creds/README.md。"""
    try:
        return open(f"creds/{name}", encoding="utf-8").read().strip()
    except OSError:
        raise SystemExit(f"缺少 creds/{name}，先按 creds/README.md 生成")


DEV_ID = cred("device_id.txt")
KEY_RE = re.compile(r"^(\d{10})-([0-9a-f]{32})-([01])(\d{8})-([0-9a-f]{32})$")


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, shell=True).stdout


def current_token():
    t = cred("token.txt")
    if t.startswith("eyJ"):
        return t
    raise SystemExit("token.txt 不是 JWT")


def unpad(b: bytes) -> bytes:
    n = b[-1]
    return b[:-n] if 1 <= n <= 16 else b


def get(host: str, token: str, path: str):
    t = str(int(time.time() * 1000)) + "000"
    s = hashlib.md5(t[3:16].encode()).hexdigest()
    req = urllib.request.Request(host + path, headers={
        "accept": "application/json;charset=UTF-8",
        "User-Agent": "okhttp/4.12.0",
        "t": t, "s": s, "aut": token, "deviceId": DEV_ID,
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        rotated = r.headers.get("refresh-authorization")
        body = r.read()
    return rotated, body


def decrypt(token: str, body: bytes):
    obj = json.loads(body)
    enc = obj.get("encData") or obj.get("data", {}).get("encData")
    if not enc:
        return None, obj
    kb = token[2:18].encode()
    d = Cipher(algorithms.AES(kb), modes.CBC(kb)).decryptor()
    plain = unpad(d.update(base64.b64decode(enc)) + d.finalize())
    return json.loads(plain.decode()), None


def probe(host: str, token: str, vid: str):
    try:
        rotated, body = get(host, token, f"/api/video/getVideoById?videoId={vid}")
    except Exception as e:
        return f"{vid} ERR {e}"
    inner, err = None, None
    try:
        inner, err = decrypt(token, body)
    except Exception as e:
        err = f"dec {e}"
    if err:
        return f"{vid} 解密失败 {err} raw={body[:80]!r}"
    ak = inner.get("authKey")
    m = KEY_RE.match(ak or "")
    flag = m.group(3) if m else "?"
    name = (inner.get("videoName") or inner.get("name") or "")[:18]
    dur = inner.get("duration") or inner.get("videoDuration")
    return (f"{vid} flag={flag} dur={dur} {name} price={inner.get('price')} "
            f"canWatch={inner.get('canWatch')} key={(ak or '')[:12]}…{(ak or '')[43:52]}")


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "https://f6eioyu.o2nhxbb7.work"
    ids = sys.argv[2].split(",") if len(sys.argv) > 2 else ["20787", "20954"]
    token = current_token()
    print(f"host={host}")
    for vid in ids:
        print(probe(host, token, vid), flush=True)


if __name__ == "__main__":
    main()
