#!/usr/bin/env python3
"""把 frida-gadget 塞进已打静态补丁的 APK，并让 libflutter.so 依赖它。

原始产物 patched_signed.apk 只读，不修改；结果写到 gadget_pack/。
"""
from __future__ import annotations

import json
import os
import subprocess
import struct
import sys
import zipfile
from pathlib import Path

import lief

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "static_patch" / "patched_signed.apk"
OUT = ROOT / "gadget_pack"
GADGET = ROOT / "gadget" / "libgadget.so"
SCRIPT = ROOT / "gadget" / "full_play.script.js"
ABI = "arm64-v8a"
FLUTTER = f"lib/{ABI}/libflutter.so"
GADGET_ENTRY = f"lib/{ABI}/libgadget.so"
CONFIG_ENTRY = f"lib/{ABI}/libgadget.config.so"
SCRIPT_ENTRY = f"lib/{ABI}/libgadget.script.so"

CONFIG = {
    "interaction": {
        "type": "script",
        "path": os.environ.get("GADGET_SCRIPT_PATH", "libgadget.script.so"),
        "on-change": "reload",
    }
}


def find_tool(name, fallback):
    for cand in (os.environ.get(name.upper()), shutil_which(name), fallback):
        if cand and Path(cand).exists():
            return cand
    raise SystemExit(f"找不到工具: {name}")


def shutil_which(p):
    from shutil import which
    return which(p)


def lief_roundtrip(data: bytes, mutate) -> bytes:
    """lief 的写回只在文件路径模式下可靠，这里用临时文件中转。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.so"
        dst = Path(td) / "out.so"
        src.write_bytes(data)
        elf = lief.parse(str(src))
        mutate(elf)
        elf.write(str(dst))
        return dst.read_bytes()


def patch_flutter(data: bytes) -> bytes:
    def add(elf):
        needed = [d.name for d in elf.dynamic_entries
                  if d.tag == lief.ELF.DynamicEntry.TAG.NEEDED]
        if "libgadget.so" not in needed:
            elf.add_library("libgadget.so")
    out = lief_roundtrip(data, add)
    check = lief.parse(out)
    nd = [d.name for d in check.dynamic_entries
          if d.tag == lief.ELF.DynamicEntry.TAG.NEEDED]
    if "libgadget.so" not in nd:
        raise SystemExit(f"DT_NEEDED 写入失败: {nd}")
    return out


def set_soname(elf):
    """gadget 的 SONAME 与文件名对齐，避免 linker 重复载入。"""
    for d in elf.dynamic_entries:
        if d.tag == lief.ELF.DynamicEntry.TAG.SONAME:
            d.name = "libgadget.so"


def cached(key_bytes: bytes, name: str, build):
    """按输入字节的 sha256 缓存派生产物，避免每轮重复跑 LIEF。"""
    import hashlib
    cache = OUT / ".cache"
    cache.mkdir(exist_ok=True)
    tag = hashlib.sha256(key_bytes).hexdigest()[:16]
    p = cache / f"{name}-{tag}.so"
    if not p.exists():
        p.write_bytes(build())
    return p.read_bytes()


def main():
    OUT.mkdir(exist_ok=True)
    script_path = Path(sys.argv[1]) if len(sys.argv) > 1 else SCRIPT
    script = script_path.read_bytes()

    with zipfile.ZipFile(SRC) as zin:
        flutter_raw = zin.read(FLUTTER)
    gadget = cached(GADGET.read_bytes(), "gadget",
                    lambda: lief_roundtrip(GADGET.read_bytes(), set_soname))
    flutter = cached(flutter_raw, "flutter", lambda: patch_flutter(flutter_raw))

    tmp = OUT / "gadget.tmp.apk"
    with zipfile.ZipFile(SRC) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename in ("META-INF/MANIFEST.MF",) or \
               item.filename.startswith(("META-INF/",)) and item.filename[-4:] in (".SF", ".RSA", ".DSA"):
                continue
            data = zin.read(item.filename)
            if item.filename == FLUTTER:
                data = flutter
            zout.writestr(item.filename, data)
        zout.writestr(GADGET_ENTRY, gadget)
        zout.writestr(CONFIG_ENTRY, json.dumps(CONFIG, indent=1))
        zout.writestr(SCRIPT_ENTRY, script)

    zipalign = find_tool("zipalign", r"C:\Android\Sdk\build-tools\34.0.0\zipalign.exe")
    aligned = OUT / "gadget_aligned.apk"
    run([zipalign, "-f", "4", str(tmp), str(aligned)])

    apksigner = find_tool("apksigner", r"C:\Android\Sdk\build-tools\34.0.0\apksigner.bat")
    ks = ROOT / "static_patch" / "patch.keystore"
    ks_pass = os.environ.get("PATCH_KS_PASS", "patch123")   # 与 flutter_static_patch.py 同一口令
    final = OUT / (sys.argv[2] if len(sys.argv) > 2 else "gadget_signed.apk")
    run([apksigner, "sign", "--ks", str(ks), "--ks-pass", f"pass:{ks_pass}",
         "--key-pass", f"pass:{ks_pass}", "--out", str(final), str(aligned)])
    run([apksigner, "verify", str(final)])
    print("OK ->", final, final.stat().st_size)


def run(cmd):
    env = dict(os.environ)
    jdk = Path(r"C:\Program Files\Java\jdk-9.0.1")
    if jdk.exists():
        env["JAVA_HOME"] = str(jdk)
    r = subprocess.run(cmd, capture_output=True, text=True, shell=cmd[0].endswith(".bat"),
                       env=env)
    if r.returncode != 0:
        raise SystemExit(f"{cmd[0]} 失败:\n{r.stdout}\n{r.stderr}")
    return r.stdout


if __name__ == "__main__":
    main()
