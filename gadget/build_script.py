#!/usr/bin/env python3
"""把 blutter helpers 与 frida-compile 产物拼成 gadget 可直接加载的脚本。"""
import os
import re
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BLUTTER = ROOT.parent / "blutter_output" / "blutter_frida.js"

ENTRY = ROOT / (sys.argv[1] if len(sys.argv) > 1 else "entry.js")
BUNDLE = ROOT / (f"bundle-{ENTRY.stem}.js")
OUT_NAME = sys.argv[2] if len(sys.argv) > 2 else "full_play.script.js"

# frida-compile 会在产物头部写入「📦\n<size> entry.js\n<size> entry.js.map\n✓」标记行
npx = "npx.cmd" if os.name == "nt" else "npx"
subprocess.run([npx, "frida-compile", str(ENTRY),
                "-B", "iife", "-S", "-T", "none", "-o", str(BUNDLE)],
               cwd=ROOT, check=True, shell=False)
bundle = BUNDLE.read_text(encoding="utf-8")
if bundle.startswith("\U0001F4E6"):                  # frida-compile 的头部标记
    lines = bundle.split("\n")
    i = 1
    while i < len(lines) and re.fullmatch(r"\d+ \S+", lines[i]):
        i += 1
    bundle = "\n".join(lines[i + 1:])                # 再跳掉收尾的勾选行
if "export default" in bundle:
    raise SystemExit("bundle 仍含 ESM 导出，需用 -B iife")

full = BLUTTER.read_text(encoding="utf-8")
helpers = (
    "const ShowNullField = false;\nconst MaxDepth = 5;\n"
    "const PointerCompressedEnabled" + full.split("const PointerCompressedEnabled", 1)[1]
)
helpers = helpers.replace('xxx("remove this line and correct the hook value");', "//")

OUT = ROOT / OUT_NAME
OUT.write_text(helpers + "\n" + bundle, encoding="utf-8")
print(f"{OUT} <- helpers {len(helpers)}B + bundle {len(bundle)}B")
