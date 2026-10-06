#!/usr/bin/env python3
"""Create a separately signed, statically patched APK from the local baseline.

Every write is guarded by the exact instruction bytes found in this APK.
The original APK is never modified; installation is deliberately separate.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import struct
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APK = ROOT / "base.apk"
OUT = ROOT / "static_patch"
# 本地重签用的 keystore 口令；换成自己的：set PATCH_KS_PASS=xxx
KS_PASS = os.environ.get("PATCH_KS_PASS", "patch123")
SO_PATH = "lib/arm64-v8a/libapp.so"
EXPECTED_APK_SHA256 = "c44387a5dfb92a98b80ca236ce0cdf55ecc8a2f5ec39613a7e3d26eb6ae38dd2"
NOP = 0xD503201F
RET = 0xD65F03C0
MOV_X0_XZR = 0xAA1F03E0


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def u32(data: bytearray, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]


def w32(data: bytearray, off: int, value: int) -> None:
    struct.pack_into("<I", data, off, value)


def check_exec_segment(data: bytes, addr: int, size: int) -> bool:
    # ELF64 program headers; require a PF_X PT_LOAD segment and identical VA/file offset mapping.
    phoff = struct.unpack_from("<Q", data, 0x20)[0]
    phentsize = struct.unpack_from("<H", data, 0x36)[0]
    phnum = struct.unpack_from("<H", data, 0x38)[0]
    for i in range(phnum):
        off = phoff + i * phentsize
        p_type, p_flags = struct.unpack_from("<II", data, off)
        p_offset, p_vaddr, _, p_filesz, _, _ = struct.unpack_from("<QQQQQQ", data, off + 8)
        if p_type == 1 and (p_flags & 1) and p_offset == p_vaddr:
            if p_vaddr <= addr and addr + size <= p_vaddr + p_filesz:
                return True
    return False


def patch_word(data: bytearray, off: int, expected: int, replacement: int, label: str) -> str:
    if not check_exec_segment(data, off, 4):
        raise ValueError(f"{label}: offset 0x{off:x} is outside executable PT_LOAD")
    actual = u32(data, off)
    if actual != expected:
        raise ValueError(f"{label}: signature mismatch at 0x{off:x}: expected 0x{expected:08x}, got 0x{actual:08x}")
    w32(data, off, replacement)
    return f"{label}: 0x{off:x} 0x{actual:08x} -> 0x{replacement:08x}"


def patch_pair(data: bytearray, off: int, expected_first: int, label: str) -> str:
    if not check_exec_segment(data, off, 8):
        raise ValueError(f"{label}: offset 0x{off:x} is outside executable PT_LOAD")
    actual = u32(data, off)
    if actual != expected_first or u32(data, off + 4) != 0xAA0F03FD:  # mov fp, sp follows entry stp
        raise ValueError(f"{label}: function-entry signature mismatch at 0x{off:x}")
    w32(data, off, MOV_X0_XZR)
    w32(data, off + 4, RET)
    return f"{label}: 0x{off:x} entry -> mov x0, xzr; ret"


def find_tool(name: str, candidates: list[str]) -> str:
    for c in candidates:
        if os.path.isfile(c):
            return c
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f"Required tool not found: {name}")


def run(args: list[str]) -> None:
    env = os.environ.copy()
    # The host JAVA_HOME is stale (points to a removed JDK); use the installed JDK 9 explicitly.
    if any(Path(a).name.lower() == "apksigner.bat" for a in args):
        jdk = r"C:\Program Files\Java\jdk-9.0.1"
        env["JAVA_HOME"] = jdk
        env["PATH"] = str(Path(jdk) / "bin") + os.pathsep + env.get("PATH", "")
    p = subprocess.run(args, capture_output=True, text=True, timeout=120, env=env)
    if p.returncode:
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(args)}\n{p.stdout}\n{p.stderr}")
    if p.stdout.strip():
        print(p.stdout.strip())


def main() -> None:
    if not APK.is_file():
        raise FileNotFoundError(APK)
    original = APK.read_bytes()
    if sha256(original) != EXPECTED_APK_SHA256:
        raise ValueError("base.apk SHA-256 differs from the statically analyzed baseline; refusing to patch")
    OUT.mkdir(exist_ok=True)
    with zipfile.ZipFile(APK) as zin:
        original_so = zin.read(SO_PATH)
    so = bytearray(original_so)
    if so[:4] != b"\x7fELF" or so[4] != 2 or so[5] != 1:
        raise ValueError("Expected little-endian ELF64 libapp.so")

    changes = []
    # A64 CSEL sites confirmed against the disassembly and original instruction words.
    # NOTE: UserService._isVIP (0x63835c) has ZERO callers in the AOT snapshot; it is
    # dead code. Real VIP checks read Rx<UserInfo>.value.vipType (field_cb) directly.
    # Kept for completeness, but the effective fix is the fromJson source patch below.
    changes.append(patch_word(so, 0x6383A8, 0x9A91C200, 0xAA1003E0, "isVIP: mov x0,x16 (true)"))
    # ForbiddenPage renders content only when isForbiddenVip is true (false builds the
    # lock mask) — the old patch forced false, which is why the 禁区 tab "didn't open".
    # NOTE: the getter is called inside a GetX Obx; stubbing the whole function to a
    # constant removes the Rx read and Obx throws "improper use of GetX" -> ErrorWidget
    # -> blank tab. Keep the original body (its Rx read at 0x6bd294 stays the Obx
    # dependency) and just NOP the b.ne after "cmp forbiddenVipType, #14" so the
    # function always falls into the "return true" arm.
    changes.append(patch_word(so, 0x6BD2A4, 0x54000061, NOP, "isForbiddenVip: b.ne -> NOP (always true)"))

    # UserInfo.fromJson (0x4f3a50, size 0x124c) is the single construction path for
    # UserInfo (constructor init, network refresh, cache restore). Force vipType and
    # chatVipType at the source: replace "mov x2, x0" after MapExt.asInt with
    # "movz x2, #2". BoxInt64Instr then tags it to Smi(2); every field_cb read sees 2.
    MOVZ_X2_2 = 0xD2800042
    changes.append(patch_word(so, 0x4F4524, 0xAA0003E2, MOVZ_X2_2, "UserInfo.fromJson: vipType := 2"))
    changes.append(patch_word(so, 0x4F4570, 0xAA0003E2, MOVZ_X2_2, "UserInfo.fromJson: chatVipType := 2"))

    # Per-video coin gate. updateVideoDetail copies VideoDetail.field_b (server-side
    # "may watch in full") into controller.field_5f and PlayerController.field_47:
    # - the progress callback cuts playback at 20s when field_5f is false and raises
    #   tryWatchEnd (the 3.0 gold-coin buy tip),
    # - the position listener pauses + seeks back at 20s when field_47 is false,
    # - the buy chip in the header reads field_5f as well.
    # Force the flag to true right where it is decompressed: one site unlocks all
    # three paths. MOVZ x2, #0x20 stores the compressed true pointer.
    changes.append(patch_word(so, 0x640344, 0x8B1C8042, 0x52800402, "updateVideoDetail: canWatchFull := true"))
    # The deduct/type countdown ("try N seconds" timer) is independent of the flag;
    # never start it.
    changes.append(patch_word(so, 0x70B920, 0x94000005, NOP, "getDeductType: startCountdown call -> NOP"))

    # Synchronous, display/navigation helpers only. updateIsShowedForceAd is deliberately preserved.
    for name, off in [("jumpForceAd", 0x63F8B0),
                      ("startForceAdCountDown", 0x70B668), ("kAdjump", 0x5EEA90)]:
        changes.append(patch_pair(so, off, 0xA9BF79FD, name))

    # Entrance ads on the play page: redirect the call instead of patching showAds.
    # onInit (0x70b49c) is the only caller of showAds; point it at the dismiss action
    # (0x70b59c) so the video detail loads with no ad dialog. Patching INSIDE showAds
    # ("bl dismissAction; ret") was a dead end: the bl clobbers LR, so the trailing
    # ret jumped to itself and spun 1.ui at 100% CPU (tombstone-era freeze, seen live
    # in trace_00 with pc parked at 0x70b4bc). showAds itself stays original/dead.
    changes.append(patch_word(so, 0x70B49C, 0x94000007, 0x94000040,
                              "onInit: bl showAds -> bl _showAllEntranceAdsDismissAction"))

    # This async call's return value is ignored by its caller, so the call itself can be skipped.
    # Do not NOP fecthAllAds at 0x5b1c04: its Future is immediately consumed by AwaitStub.
    changes.append(patch_word(so, 0x707BBC, 0x940000EB, NOP, "MainController.showAllEntranceAds call -> NOP"))

    # Kill the ad data source at its root. StorageService.ads (0x61ced0) already has a
    # cache-miss path returning null (0x61cf10), which every caller tolerates (fresh
    # installs hit it on first run). Jumping the entry straight to that epilogue
    # starves AdUtils/getAdInfo/getAdInfos, the launch ad, list/banner insert ads and
    # the play-page entrance ads of data, and removes the AdInfoModel.fromJson SIGSEGV
    # observed in tombstone_31 (crash chain: ctor -> getAdInfo -> ads -> fromJson).
    changes.append(patch_word(so, 0x61CED0, 0xA9BF79FD, 0xAA1603E0, "StorageService.ads -> null (mov x0, x22)"))
    changes.append(patch_word(so, 0x61CED4, 0xAA0F03FD, RET, "StorageService.ads -> ret"))

    # The launch-ad screen (LuanchAdWidget) never auto-navigates in the original app:
    # its 1s periodic Timer only decrements the counter and setState; the zero path just
    # returns, so the ad stays until the skip button is tapped. Navigation during build
    # (initState -> offHome) is illegal, so instead patch the Timer closure's zero path
    # (0x6e0340, runs on the event loop) to call offHome (0x5f9598), and start the
    # counter at 0 (0x700864, movz x0, #5 -> #0) so home is entered after ~1s.
    changes.append(patch_word(so, 0x6E0340, 0xAA1603E0, 0x97FC6496, "ad countdown zero -> bl offHome"))
    changes.append(patch_word(so, 0x700864, 0xD28000A0, 0xD2800000, "launch ad counter := 0"))

    backup = OUT / "libapp.so.original"
    backup.write_bytes(original_so)
    patched_apk = OUT / "patched.apk"
    tmp_apk = OUT / "patched.tmp.apk"
    with zipfile.ZipFile(APK, "r") as zin, zipfile.ZipFile(tmp_apk, "w") as zout:
        zout.comment = zin.comment
        for item in zin.infolist():
            payload = bytes(so) if item.filename == SO_PATH else zin.read(item.filename)
            zout.writestr(item, payload)
    os.replace(tmp_apk, patched_apk)

    zipalign = find_tool("zipalign", [r"C:\Android\Sdk\build-tools\34.0.0\zipalign.exe"])
    aligned = OUT / "patched_aligned.apk"
    run([zipalign, "-f", "4", str(patched_apk), str(aligned)])

    keytool = find_tool("keytool", [r"C:\Program Files\Java\jdk-9.0.1\bin\keytool.exe"])
    keystore = OUT / "patch.keystore"
    if not keystore.exists():
        run([keytool, "-genkeypair", "-keystore", str(keystore), "-alias", "patch",
             "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
             "-storepass", KS_PASS, "-keypass", KS_PASS,
             "-dname", "CN=Local Patch, OU=Test, O=Local, L=Local, ST=Local, C=CN", "-noprompt"])
    apksigner = find_tool("apksigner", [r"C:\Android\Sdk\build-tools\34.0.0\apksigner.bat"])
    final_apk = OUT / "patched_signed.apk"
    run([apksigner, "sign", "--ks", str(keystore), "--ks-pass", f"pass:{KS_PASS}",
         "--key-pass", f"pass:{KS_PASS}", "--out", str(final_apk), str(aligned)])
    run([apksigner, "verify", "--verbose", str(final_apk)])

    with zipfile.ZipFile(final_apk) as z:
        final_so = z.read(SO_PATH)
    for off, expected, label in [(0x6383A8, 0xAA1003E0, "isVIP"),
                                 (0x6BD2A4, NOP, "isForbiddenVip always true"),
                                 (0x640344, 0x52800402, "canWatchFull := true"),
                                 (0x70B920, NOP, "startCountdown call NOP"),
                                 (0x707BBC, NOP, "entrance ad call"),
                                 (0x4F4524, MOVZ_X2_2, "vipType := 2"),
                                 (0x4F4570, MOVZ_X2_2, "chatVipType := 2"),
                                 (0x70B49C, 0x94000040, "onInit -> dismissAction"),
                                 (0x61CED0, 0xAA1603E0, "ads -> null"),
                                 (0x61CED4, RET, "ads ret"),
                                 (0x6E0340, 0x97FC6496, "countdown zero -> offHome"),
                                 (0x700864, 0xD2800000, "ad counter := 0")]:
        got = struct.unpack_from("<I", final_so, off)[0]
        if got != expected:
            raise ValueError(f"Post-pack verification failed for {label}: 0x{got:08x}")
    print("\nPatch report:")
    print("\n".join("  " + x for x in changes))
    print(f"Original APK SHA-256: {sha256(original)}")
    print(f"Patched APK SHA-256:  {sha256(final_apk.read_bytes())}")
    print(f"Output: {final_apk}")
    print("The original APK and app installation were not modified.")


if __name__ == "__main__":
    main()
