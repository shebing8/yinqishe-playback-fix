# yinqishe_app 在线播放修复 — frida-gadget 常驻方案

目标 app：`com.example.yinqishe_app`（Flutter / Dart AOT + libmpv 播放器）
目标设备：OPPO PGEM10（arm64，已 root），验证序列号 `55434d6`
交付形态：**一个内置 frida-gadget + 常驻 JS 的重签名 APK**。装上后不需要 PC、不需要 frida-server、不需要每次手动注入；长时间连续观看也不需要重启 app。

完整取证与实测记录见 **[REPORT.md](REPORT.md)**（根因、实测数据、踩过的坑、未完成项）。

---

## 结论：两个独立缺陷

最初的症状「在线播放卡顿」实际是两件事，都已修复并在真机实测通过：

| # | 症状 | 根因 | 修复 | 状态 |
|---|---|---|---|---|
| A | 播几秒就冻结 / 反复缓冲 | app 把 mpv 前向缓冲压到 `demuxer-readahead-secs≈1s`，分片一慢就 underrun | hook `libmpv.so`，按 handle 强设 readahead=30s / 96MiB，并拦截 app 自己下发的窄参数 | ✅ |
| B | 只能看 12 秒 | 服务端签发的 `auth_key` 第三段首位 flag=0 → CDN `authPath` 只返回 12s 试看列表 | hook Dart 层 `authKey` 字符串，原地等长替换成合法签名的 1 前缀 key，并按 4 分钟周期现签续期 | ✅ |

实测口径：**播放位置 vs 墙钟的累计漂移**（唯一可信指标）与 SurfaceFlinger 出帧节奏，全程不使用截图。
代表数据：28:41 的视频连续播到 23:19，`步进=63s / 墙钟 62.9s / 累计落后 -0.0s`；60s 采样平均 49.1 fps、`>120ms` 掉帧 0 次；demuxer 缓冲中位从 2.5s 升到 101s，underrun 从 2 次降到 0 次。

---

## 目录结构

```
.
├── README.md                  本文件
├── REPORT.md                  交付报告：根因 / 实现链路 / 实测证据 / 未完成项
├── requirements.txt           Python 依赖
├── LICENSE                    MIT
│
├── gadget/                    常驻 JS（两条修复线）
│   ├── entry.js               frida-java-bridge + require 两份脚本
│   ├── full_play.js           12 秒 → 正片：authKey 等长替换 + 现签续期 + 跨进程节流
│   ├── mpv_tune.js            卡顿：按 handle 强设 demuxer 缓冲并回读纠偏
│   ├── build_script.py        frida-compile 打包 + 前置 blutter helpers
│   └── package.json           frida-compile@19 / frida-java-bridge@7
│
├── gadget_pack.py             ELF/APK 组装器：LIEF 注入 DT_NEEDED + config + script + 重签
├── flutter_static_patch.py    前置静态补丁：libapp.so 指令级替换 + keytool 自签
│
├── api_probe.py               本地复现 app 的签名请求（t / s / aut / deviceId + encData 解密）
├── key_scan.py                扫一批 videoId，看服务端什么条件下才发 1 前缀 key
├── mint_probe.py              开一个新游客，确认它立刻能签出正片 key，核对首段语义
├── quota_probe.py             验证反复现签是否消耗观看额度
│
├── ui_probe.py                无截图驱动：adb 点击 + uiautomator 文本 + gadget 日志尾部
├── play_watch.py              长播漂移监控（位置 vs 墙钟）
├── fps_meter.py               SurfaceFlinger 出帧节奏，0.5s 分桶
│
└── creds/                     本地凭据目录（内容全部 gitignore，见 creds/README.md）
```

---

## 环境

| 组件 | 版本 | 用途 |
|---|---|---|
| Python | 3.10+ | 全部脚本；`pip install -r requirements.txt`（`lief`、`cryptography`） |
| Node.js + npm | 20+ | `cd gadget && npm install`（`frida-compile@^19.1.0`、`frida-java-bridge@^7.0.13`） |
| frida-gadget | **17.22.1 android-arm64** | 解出 `libgadget.so` 放到 `gadget/libgadget.so`（Frida 17 无内置 Java 桥，必须自行打桥） |
| Android SDK build-tools | 34.x | `zipalign` / `apksigner`；不在 PATH 时用环境变量 `ZIPALIGN` / `APKSIGNER` 指路径 |
| JDK | 9+ | `keytool` 生成签名用 keystore，`apksigner` 需要 `JAVA_HOME` |
| [blutter](https://github.com/abyss-anchor/blutter) | — | Dart AOT 反汇编，产出 `blutter_output/blutter_frida.js`（`build_script.py` 需要它的前置 helpers） |
| adb | platform-tools | 装机与验证 |

**不入库的文件**（体积大 / 属第三方，全部在 `.gitignore` 里）：`base.apk`、`static_patch/`、`so/`、`blutter/`、`blutter_output/`、`gadget/libgadget.so`、`gadget/node_modules/`、`gadget/full_play.script.js`、`gadget_pack/`。

---

## 重建链路（从 `base.apk` 到安装包）

```bash
# 0) 自备 base.apk（从设备 pull 当前安装包）
MSYS_NO_PATHCONV=1 adb -s <SERIAL> shell pm path com.example.yinqishe_app
MSYS_NO_PATHCONV=1 adb -s <SERIAL> pull /data/app/.../base.apk base.apk
#    本项目基线 md5 23511d32…（sha256 c44387a5dfb92a98b80ca236ce0cdf55ecc8a2f5ec39613a7e3d26eb6ae38dd2）
#    flutter_static_patch.py 会先校验这个 sha256，不匹配直接抛错

# 1) Dart AOT 反汇编（拿偏移与 blutter_frida.js）
blutter -a base.apk -o blutter_output

# 2) 前置静态补丁 + 自签（首次运行自动 keytool 生成 static_patch/patch.keystore）
python flutter_static_patch.py
#    → static_patch/patched_signed.apk   md5 433b1082…（本目录约定只读，不再改动）

# 3) JS → gadget 可直加载的单文件 bundle
cd gadget && npm install && python build_script.py && cd ..
#    → gadget/full_play.script.js        917 798 B / md5 5c043309b8d4252aed1c01363ea6ea17

# 4) 打进 APK 并重签
python gadget_pack.py gadget/full_play.script.js gadget_full_signed.apk
#    → gadget_pack/gadget_full_signed.apk 69 260 671 B / md5 7f7861a3bb57a2f615fd67b5127bc927

# 5) 装机
adb -s <SERIAL> install -r gadget_pack/gadget_full_signed.apk
```

签名口令默认 `patch123`，可用环境变量 `PATCH_KS_PASS` 覆盖（`flutter_static_patch.py` 与 `gadget_pack.py` 共用同一个 keystore）。

第 3 步产物的 md5 应与设备上的一致，这是复现校验点：

```bash
MSYS_NO_PATHCONV=1 adb -s <SERIAL> shell "su -c 'find /data/app -name libgadget.script.so -exec md5sum {} \;'"
# 期望 5c043309b8d4252aed1c01363ea6ea17
```

---

## 关键逆向锚点

`libapp.so`（Dart AOT 快照）内偏移，运行时加 `libapp.so` 基址：

| 偏移 | 符号 | 用途 |
|---|---|---|
| `0x4f6b30` | `StorageService._read` | 返回值里捞 app 当前会话 JWT（`aut`），落盘值是加密的，必须现取 |
| `0x4f6a1c` | `MapExt.asString` | `key=="authKey"` 那次调用返回 authKey → 等长覆写点 |
| `0x641310` | authPath URL 构造 | 从 app 自己拼出的 URL 学**当前 API 域名**（域名按版本轮换） |

`auth_key` 是 CDN A 型签名：`^(\d{10})-([0-9a-f]{32})-([01])(\d{8})-([0-9a-f]{32})$`

- 第三段首位 = 权限位：`0` 是 12 秒试看，`1` 是完整正片；由**账号额度**决定，且 **key 不绑定 path**；
- flag 被算进 MD5，本地改一位必然 403 → 只能整张换成合法签名的 key；
- 首段是**签发时刻**（不是过期时刻），有效窗口约 10 分钟，所以脚本按 4 分钟续期。

app 的请求签名（`api_probe.py` 完整复现）：`t = ms + "000"`、`s = md5(t[3:16])`，头带 `t` / `s` / `aut` / `deviceId`；响应 `encData` 用**发出时那个 token** 的 `token[2:18]` 做 AES-CBC（key=iv）—— 因为 `refresh-authorization` 可能已经把 token 轮换掉。

---

## 装机后验证（全程无截图）

```bash
python ui_probe.py progress 5 40      # 播放位置 vs 墙钟
python fps_meter.py 60                # SurfaceFlinger 出帧节奏（0.5s 分桶）
python play_watch.py 12 45            # 长播漂移监控
MSYS_NO_PATHCONV=1 adb -s <SERIAL> shell "su -c 'tail -40 /data/data/com.example.yinqishe_app/gadget_play.log'"
```

正常日志长相：`[替换] 12秒 key -> 正片 key`、`定时续期 … 状态码 200 … 已现签 1 前缀 key`、`mpv` 侧 `readahead-secs=30 max-bytes=100663296`。

异常判读：

| 日志 | 含义 | 处理 |
|---|---|---|
| `换号节流：30 分钟内已换 N 个，等下一轮` | 额度耗尽且窗口内已换满 6 个 | 不用管，窗口滑过（≤30 分钟）自动续上 |
| `遇到 0 前缀 key 但还没有可用 1 前缀 key` 且没有节流行 | 现签链路真断了（token 失效） | 重取 `creds/token.txt`，必要时 `python mint_probe.py https://<当前 API 域名>` |
| `fps_meter.py` 报 0 帧 | `BLAST#2271` 层还没建立就回退到容器层 | 等画面出来再采样，别当失败 |

测量陷阱：连续点按会触发播放器 ±15s 快进手势，产生 `步进 61s / 墙钟 45s` 的假象；采样间隔 ≥40s，期间不要并发点击。

---

## 凭据与账号额度

`creds/` 目录内容全部 gitignore，字段与获取方式见 **[creds/README.md](creds/README.md)**。

设备侧两份状态文件让「一直看」不需要重启进程，它们也是账号防刷爆的唯一闸门：

- `/data/data/<pkg>/gadget_bak.txt` —— 备用游客 `deviceId\nJWT`，冷启动 `loadBak()` 读回，不重复开号；
- `/data/data/<pkg>/gadget_mint.txt` —— 每行一个 mint 时刻的 epoch，`recentMints()` 只统计 `MINT_WINDOW=1800s` 内的行，满 `MINT_WINDOW_MAX=6` 就拒绝本轮开号，`noteMint()` 追加时剪掉窗口外的行。

折算：每个游客 `freeWatches=50` → 30 分钟最多 6 个号 = 300 部 / 30 分钟的续号余量。额度只在真实观看时消耗，`getVideoById` 现签不扣。

仓库里唯一保留的运行时值是 `gadget/full_play.js` 顶部的两个兑底：备用 API 域名与备用 deviceId。它们只在冷启动、app 还没发出自己的 authPath 时用作初值，随后会被运行时学到的域名校正；会话 JWT 从不写进脚本。这两行保持原样，是为了让源码仍能逐字编译出 REPORT §2.5 里那个 md5 的产物。

---

## 已知的坑（本机环境）

- **不要用 PC 的 frida spawn**。在这台设备上 spawn 会污染 zygote 的 `libstagefright` PLT，视频页直接 SIGILL 闪退，唯一恢复手段是 `reboot`。本方案的全部意义就是「不依赖 PC 注入」。
- `static_patch/` 约定**只读**：里面是基线 `.so`、keystore 与前置补丁产物，不要覆盖。
- Git Bash 下 adb 的绝对设备路径要加 `MSYS_NO_PATHCONV=1`，否则 `/data/...` 会被改写成 Windows 路径。
- Windows 控制台打印中文是乱码，只是显示问题，日志文件本身是 UTF-8。

## 未完成 / 边界

- 额度状态没有用户可见提示（只有设备日志）。
- `API_HOST` 仍是运行时从 app 学到的，没有落盘；冷启动第一次现签要等 app 自己发出 authPath。
- 没有低带宽降码率回退；>30 分钟 / 锁屏 / 切后台的长时程浸泡测试没做。
- 只在 OPPO PGEM10 / Android 10 上验证过，未覆盖其他机型与 iOS。
- 如果服务端改成把 flag 与 path 绑定，本地替换会失效（当前实测**不绑定**）。

---

## 范围与合规

这是针对**自有设备、自有账号**的一次应用行为分析与缺陷修复研究：静态补丁解决本地弹窗拦截，动态替换解决服务端 CDN 试看限制，mpv 缓冲调整解决卡顿。仓库不含目标 app 的二进制、不含任何凭据、不含可复用的账号池。

脚本会向线上服务发起请求并注册游客账号，请在自己的授权范围内、按上面的节流使用。

## License

MIT — 见 [LICENSE](LICENSE)。
