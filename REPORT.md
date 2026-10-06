# yinqishe_app 在线播放修复 — 交付报告

工作区 `D:\shuaji\14` ｜ 设备 OPPO PGEM10（adb 序列号 `55434d6`，Android/arm64，已 root）
目标包名 `com.example.yinqishe_app`（Flutter/Dart AOT + libmpv 播放器）
生成时间 2026-10-06 ｜ 报告对应的最终安装包 `gadget_pack/gadget_full_signed.apk`

---

## 1. 结论

原始诉求「在线播放卡顿」实际是**三个独立缺陷**，都已修复并在设备上实测通过：

| # | 症状 | 根因 | 修复手段 | 状态 |
|---|---|---|---|---|
| A | 播放几秒就冻结/反复缓冲 | app 把 mpv 前向缓冲压到 `demuxer-readahead-secs≈1s`，分片一慢就 underrun | gadget hook `libmpv.so`，按 handle 强设 readahead=30s / 96MiB，并拦截 app 自己的窄参数 | ✅ 实测 |
| B | 只能看 12 秒，之后停在 12s | 服务端签发的 `auth_key` 第三段首位 flag=0 → CDN `authPath` 只返回 12s 试看列表 | gadget 拦 Dart 层的 `authKey` 字符串，原地等长替换成合法签名的 1 前缀 key，并按 4 分钟周期现签续期 | ✅ 实测 |
| C | 竖屏视频进全屏被强制横屏，画面缩成中间一条、上下大片黑边 | media_kit_video 的全屏默认无条件请求横屏族方向（`Activity.setRequestedOrientation(11)`），不看视频宽高 | Java 层 hook `setRequestedOrientation(int)`：横屏族请求（0/6/8/11）先经 mpv 读当前视频宽高（按 rotate 修正），竖屏改写为同语义竖屏族（1/7/9/12），横屏与读不到宽高时原样放行 | ✅ 实测 |

交付物：**一个可直接安装的重量级 APK**（`gadget_pack/gadget_full_signed.apk`，66.0 MiB），内置 frida-gadget + 常驻 JS，装机后无需 PC、无需 frida-server、无需手动注入即可长期工作。

「一直不停看」也不会退化成 12 秒：额度与换号节流两份状态都落在 app 私有目录（`gadget_bak.txt` / `gadget_mint.txt`），冷启动读回、窗口过期后自动续号，**不需要重启或重装 app**（实测见 §3 与 §5#12）。

---

## 2. 实现链路（怎么做出来的）

### 2.1 逆向基线

```
base.apk (md5 23511d32…)                     ← 原始安装包
  └─ lib/arm64-v8a/libapp.so                 ← Dart AOT 快照
       blutter 反汇编 → blutter_output/       (asm/ objs.txt pp.txt blutter_frida.js)
```

从 `blutter_output/` 得到的关键符号（`libapp.so` 内偏移，运行时随 `libapp.so` 基址相加）：

| 偏移 | 符号 | 用途 |
|---|---|---|
| `0x4f6b30` | `StorageService._read` | 返回值里能捞到 app 当前会话的 JWT（`aut`） |
| `0x4f6a1c` | `MapExt.asString` | `key=="authKey"` 的那次调用返回 authKey 字符串 → 替换点 |
| `0x641310` | authPath URL 构造 | 从 app 自己拼出的 URL 学**当前 API 域名**（域名按版本轮换） |

### 2.2 静态补丁（前置层，只读输入）

`flutter_static_patch.py` 在原始 APK 上做指令级替换（每处都先校验原字节，不匹配就抛错），产出 `static_patch/patched_signed.apk`（md5 433b1082…，**本目录约定只读、不再改动**）：

- `0x6383A8` `isVIP: mov x0,x16` → 返回 true
- `0x640344` `updateVideoDetail: canWatchFull := true`
- `0x6BD2A4` `isForbiddenVip` 的条件跳转 → NOP
- `0x70B49C` `onInit` 里 `showAds` 调用改指向 dismiss 动作；`0x707BBC`/`0x700864` 首页广告入口与计数器清零

这一层解决的是本地弹窗/入口拦截，**不解决 12 秒问题**（12s 是服务端 CDN 侧判定，必须走 2.4 的动态替换）。

### 2.3 gadget 注入方式

`gadget_pack.py`：
1. 用 LIEF 给 `libflutter.so` 的 `DT_NEEDED` 加 `libgadget.so`（so 启动即加载 gadget）；
2. 把 `libgadget.so` 的 SONAME 对齐成 `libgadget.so`，避免 linker 重复载入；
3. 写入 `libgadget.config.so`（JSON）：`{"interaction":{"type":"script","path":"libgadget.script.so","on-change":"reload"}}`；
4. 写入 `libgadget.script.so` = 编译好的 JS bundle；
5. zipalign + apksigner（`static_patch/patch.keystore`）重签。

派生的 ELF 结果按输入 sha256 缓存在 `gadget_pack/.cache/`，重复打包不再跑 LIEF。

### 2.4 JS 逻辑（三份源，同一个 bundle）

`gadget/entry.js` = `frida-java-bridge` + `require('./full_play.js')` + `require('./mpv_tune.js')` + `require('./orient_fix.js')`。

**`full_play.js`（12 秒 → 正片）**
- `auth_key` 格式（CDN A 型签名）：`^(\d{10})-([0-9a-f]{32})-([01])(\d{8})-([0-9a-f]{32})$`
  - 第三段首位 flag：`0`=12 秒试看，`1`=完整正片；flag 由**账号额度**决定，且 **key 不绑定 path**；
  - flag 被算进签名，本地改一位必 403 → 只能换整张合法签名的 key；
  - 首段是**签发时刻**（不是过期时刻），有效窗口约 10 分钟。
- 三条 key 来源，按优先级：① 被动缓存 app 见过的 1 前缀 key；② 用 app 当前 token 向 `/api/video/getVideoById` 现签免费片（`videoId 20787 / 20954`，`price=0`）；③ 额度耗尽时 `POST /api/user/traveler` 开一个带全新 `freeWatches=50` 的游客来签。
- 替换动作写在 `MapExt.asString` 的 `onLeave`：仅当 `key=="authKey"`、正则匹配、flag=0、且手里的 1 前缀 key 长度相同且存活（`KEY_TTL=420s`）时，`writeStr` 原地覆写 Dart 字符串（区分 `OneByteString` cid 94 / `TwoByteString` cid 95）。
- 客户端签名细节（Java 桥里现做）：`t = ms+"000"`、`s = md5(t[3:16])`、头 `aut`/`deviceId`；响应 `encData` 用**发出时那个 token** 的 `token[2:18]` 做 AES-CBC(key=iv) 解密 —— 因为 `refresh-authorization` 可能已把 token 轮换掉。
- 续期与自愈：`REFRESH_MS=240000`（4 分钟）定时现签。两份状态**落盘持久化**，所以长时间连续观看**不需要重启进程**：
  - `gadget_bak.txt`（备用游客 `dev\ntoken`）：`loadBak()` 冷启动读回，不重复开号；`saveBak()` 在每次 mint 和 token 轮换后写回；
  - `gadget_mint.txt`（每行一个 mint 时刻的 epoch）：`recentMints()` 只统计 `MINT_WINDOW=1800s` 内的行，满 `MINT_WINDOW_MAX=6` 就打印「换号节流：30 分钟内已换 N 个，等下一轮」并放弃本轮，`noteMint()` 在 mint 成功后追加并把窗口外的行剪掉。节流窗口跨进程生效（旧版按进程 `MINT_MAX=4` 计数，重启即清零，≈200 部就要手动重启，已废弃）。
  - 折算：每个游客 50 部 → 30 分钟最多 6 个号 = 300 部/30 分钟的续号余量，正常手速远远用不完，也不会刷爆服务端。

**`mpv_tune.js`（卡顿）**
- 覆写目标：`demuxer-readahead-secs=30`、`demuxer-max-bytes=100663296`（96MiB）、`demuxer-max-back-bytes=8388608`（8MiB）、`network-timeout=15`。
- 三管齐下（v4，前两版都踩过坑）：① `mpv_wait_event` 里**按 handle** 首次出现即 `mpv_set_property_string`；② 拦 `mpv_set_property_string` / `mpv_set_option_string` 的入参，把 app 下发的窄缓冲值就地换成目标；③ 每 1s `mpv_get_property` 回读，偏差 >2% 再设一遍。
- 教训（已写进项目记忆）：v2 只对首次看到的 handle 生效，app 每个视频新建 handle → 仍是 1s；v3 发现 `mpv_initialize` 之前 set_property 返回 rc=0 但不生效，所以必须有回读纠偏。
- 每 10s 打一条 `stat duration=… cache=…`；`duration>20` 即可判定拿到的是正片列表（试看只有 12s）。
- 顺带职责：`mpv_wait_event` 的 `onEnter` 里持续刷新 `globalThis.__mpvCur = {ptr: handle, ts: now}`，给 `orient_fix.js` 提供「当前播放 handle」。

**`orient_fix.js`（全屏方向自适应）**
- 根因：media_kit_video 的 `defaultEnterNativeFullscreen` 无条件 `SystemChrome.setPreferredOrientations([landscapeLeft, landscapeRight])` → PlatformPlugin → `Activity.setRequestedOrientation(11)`；竖屏片进全屏被横过来，画面只占中间一条。退出全屏走 `setPreferredOrientations([])` → `-1`（UNSPECIFIED），与横竖屏族不重叠，不需要处理。
- 做法：Java 层拦 `Activity.setRequestedOrientation(int)`，只看横屏族 `0/6/8/11`（`SCREEN_ORIENTATION_*` landscape 家族），按映射 `{0:1, 6:7, 8:9, 11:12}` 改写成同语义竖屏族；其余值原样放行。
- 宽高来源：`mpv_get_property` 读当前 handle 的 `width` / `height`（先 INT64 后 STRING 两种格式都试），`video-params/rotate` 为 90/270 时交换；handle 用 `__mpvCur`，仅认 5s 内的刷新，读不到再用 30s 内的缓存兜底，再没有就放行 —— 宁可漏修也不误转。
- 日志：判词去重（同一片反复进出全屏只记一次），写到 `gadget_play.log`（文件行不带 `[orient]` 前缀，grep 用 `方向`）。

### 2.5 构建与安装命令

```bash
# 1) 编译 JS → gadget 可直加载的单文件 bundle
python gadget/build_script.py            # 输出 gadget/full_play.script.js
#    （内部 npx frida-compile entry.js -B iife -S -T none，再前置 blutter_frida.js helpers）

# 2) 打进 APK 并重签
python gadget_pack.py gadget/full_play.script.js gadget_full_signed.apk

# 3) 装机（禁用 PC frida，见第 6 节）
adb -s 55434d6 install -r gadget_pack/gadget_full_signed.apk

# 4) 无截图验证
python ui_probe.py progress 5 40         # 位置 vs 墙钟
python fps_meter.py 60                   # SurfaceFlinger 出帧节奏
python play_watch.py 12 45               # 长播漂移监控
MSYS_NO_PATHCONV=1 adb -s 55434d6 shell su -c 'tail -40 /data/data/com.example.yinqishe_app/gadget_play.log'
```

复现校验：`python gadget/build_script.py` 现在产出 `gadget/full_play.script.js` = **922 558 B**，md5 `8be5ad61336e358c2115c56c08682e98`，与设备上 `/data/app/…/lib/arm64/libgadget.script.so` **逐字一致**（2026-10-06 14:13 方向自适应版复核）。构建脚本自报的 `helpers + bundle` 两段只是编译阶段计数，与最终文件长度不是一一对应，核对以最终文件的 md5 为准；上一版（无 `orient_fix`）是 917 798 B / `5c043309…`。

---

## 3. 实测证据（全程未用截图）

取证方式只有三种：`uiautomator dump` 的 XML 文本（Flutter 页把播放时间/时长放在 `content-desc`）、`logcat` 解码统计与 SurfaceFlinger 帧时间戳、gadget 自身日志（`gadget_play.log` / `gadget_mpv.log`）。

**B：拿正片**
- `04:11:15 [替换] 12秒 key -> 正片 key（-01250709 -> -11251774）`
- 28:41 的视频从 `00:13/28:41` 连续播到 `23:19/28:41`（位置 1399s / 墙钟 1721s），`步进=63s / 墙钟 62.9s`、`累计落后 -0.0s`
- 另测得 `02:46/05:30`、`02:28/28:41`、`05:57/30:04` 均越过 12s 并持续推进
- 无 403：`定时续期` 每 4 分钟全部 `状态码 200` + 1 前缀 key，跨过 ~10 分钟签名窗口仍连续
- 最新一条（清理后复查）：`04:42:39 补签开始 … GET /api/video/getVideoById?videoId=20 状态码 200 … 已现签 1 前缀 key（游客 sub=12517741）… goodKey=yes`

**A：不卡**
- 同一安装包 60s 采样：`平均 49.1 fps`、`低于 24fps 的 0.5s 桶 = 0/119`、帧间隔 `中位 16.5ms / p90 33.1ms / 最大 50ms`、`>120ms 掉帧 0 次`
- 缓冲对照（医馆笑传 20787，720p h264 30fps，约 0.65MB/s）：修复前 demuxer 缓冲中位 **2.5s**、15% 采样 <1s、2 次 underrun、8 次进度不前进；修复后中位 **101s**、underrun **0**

**C：竖屏全屏不转横（8be5ad61 版实测）**
- hook 装载即生效：`06:14:13 方向自适应 hook 已装（竖屏视频全屏不再转横）`（gadget_play.log，行无 `[orient]` 前缀）
- 竖屏片（544x720）进全屏：`06:14:36 方向请求 11 但视频 544x720 是竖屏 -> 改为 12`；此后 `dumpsys window displays` 始终 `mRotation=0`，全屏控件布局铺满 1080x2376（全屏 ImageView 落在屏幕右下 `[912,2268][1056,2376]`，与嵌入态的 `[912,620][1056,728]` 同构），播放持续推进（16:56 → 18:00）
- 横屏片（1280x720）进全屏：`06:29:40 方向请求 11，视频 1280x720 横屏，放行` —— 放行分支不动原请求，横竖交由系统按请求处理
- 切回竖屏片再进全屏：`06:30:07 方向请求 11 但视频 544x720 是竖屏 -> 改为 12`，`mRotation` 仍为 0
- 退全屏 `setRequestedOrientation(-1)` 不产生日志（设计如此）；验证驱动全程 `uiautomator dump` + `dumpsys`，无截图；最后由用户真机复核「竖屏竖屏全屏、横屏横屏全屏」确认通过

**自愈 / 持久化（13:01 持久化版实测）**
- 冷启动读回：新进程 `05:01:47 读回备用游客 sub=12517741` → 立刻 `状态码 200` 现签，`gadget_bak.txt` 跨进程有效。
- 节流窗口跨进程生效：把 `gadget_bak.txt` 临时改名、并写入 6 条**窗口内**时间戳后重启 →`05:08:10 免费片 20787/20954 对 app 号（sub=12507099）只给 0 前缀，配额已尽` → `05:08:10 换号节流：30 分钟内已换 6 个，等下一轮` → 不再打 `POST /api/user/traveler`（证明读盘 + 窗口过滤 + 拒签都走通）。
- 窗口老化 + 自动续号：把同一文件的 6 条时间戳改成 `now-2000`（超出 1800s）后重启 → `05:08:42 api POST /api/user/traveler 状态码 200` → `已开新游客 sub=12518246 额度=50/50` → `已现签 1 前缀 key … -11251824`，无需任何人工干预。
- 写盘：mint 成功后 `gadget_mint.txt` 只剩 1 行 `1791263322.438`（6 条过期行被 `noteMint()` 剪掉），`gadget_bak.txt` 同步被新游客覆盖（`saveBak()`）。
- 换号后的正片连续性：`05:09:49 [替换] 12秒 key -> 正片 key（-01250709 -> -11251824）`，同一进程内位置 `44s → 90s`、墙钟 `48.8s → 94.8s`、`步进=46 / 累计落后=0.0s`、`总时长=808s`（13:28 全片）。
- 测试用的临时改名文件已清理，`gadget_bak.txt` / `gadget_mint.txt` 现值同步到 `creds/bak_traveler.txt`、`creds/mint_window.txt`。

---

## 4. 产物清单（工作区现存，874 MB）

| 路径 | 说明 | 大小 / md5 |
|---|---|---|
| `gadget_pack/gadget_full_signed.apk` | **最终交付包**，可直接 `install -r` | 69 264 767 B ｜ `711ac31f…` |
| `gadget_pack/gadget_full_signed.apk.idsig` | apksigner 增量签名旁挂文件 | 558 596 B |
| `gadget/full_play.script.js` | 编译后的常驻脚本（= 设备上的 `libgadget.script.so`） | 922 558 B ｜ `8be5ad61…` |
| `gadget/full_play.js` `gadget/mpv_tune.js` `gadget/orient_fix.js` `gadget/entry.js` | 脚本源（三条修复线） | — |
| `gadget/build_script.py` `gadget/libgadget.so` `gadget/node_modules` | 构建工具链（frida-compile 19 + frida-java-bridge） | libgadget `b36c995b…` |
| `gadget_pack.py` `gadget_pack/.cache/` | APK 组装器 + ELF 派生缓存 | — |
| `static_patch/` | 静态补丁层与基线产物（**只读**） | patched_signed `433b1082…` |
| `base.apk` | 原始安装包 | `23511d32…` |
| `blutter_output/` `blutter/` | Dart AOT 反汇编与工具（偏移溯源） | 99M / 268M |
| `so/` | 提取出的原生 so 备查 | 12M |
| `api_probe.py` `key_scan.py` `mint_probe.py` `quota_probe.py` | 服务端侧取证（flag 语义、额度、现签） | — |
| `ui_probe.py` `fps_meter.py` `play_watch.py` | 无截图验证三件套 | — |
| `flutter_static_patch.py` | 静态补丁生成器（含 keytool 自签） | — |
| `creds/` | 取证脚本要的 10 个小凭据文本（token / traveler / deviceId / free_key / 设备侧 `gadget_bak.txt`、`gadget_mint.txt` 副本），14 KB | — |

`apk_ad_patch.py`（通用广告 SDK 移除流水线）与本项目链路无关，未纳入仓库。

### 4.1 发布范围（GitHub 仓库）

仓库只收**源码 + 脚本 + 文档 + 环境声明**：`README.md`、`.gitignore`、`.gitattributes`、`requirements.txt`、`LICENSE`、本 `REPORT.md`、`creds/README.md`、`gadget/{entry,full_play,mpv_tune,orient_fix}.js`、`gadget/build_script.py`、`gadget/package.json`、`gadget/package-lock.json`（锁 frida-compile 19.1.0 / frida-java-bridge 7.0.13，`npm ci` 用）、`gadget_pack.py`、`flutter_static_patch.py`、四个取证脚本与三个验证脚本。

被 `.gitignore` 排除的三类：**目标 app 与第三方二进制**（`base.apk`、`static_patch/`、`so/`、`blutter/`、`blutter_output/`、`gadget/libgadget.so`、`gadget/node_modules/`）、**构建产物**（`gadget/full_play.script.js`、`gadget_pack/`，按 §2.5 可复现出同一 md5）、**凭据与调试输出**（`creds/*`、`*.log`、`ui_*.xml`、图片视频）。硬编码值也已改为从 `creds/` 或命令行读取：`api_probe.py` 的 `deviceId`、`mint_probe.py` / `quota_probe.py` 的默认 API 域名、keystore 口令（`PATCH_KS_PASS`，默认值保持本地可建）。

已删除（不再需要）：`_archive_debug/`（2.1 GiB，237 项：828M 下载视频样本、9 个过期派生 APK、frida-server 及 `.xz`、全部 logcat/截图/UI dump/tombstone/extracted/workspace_patch/backup/nl/一次性探针脚本）、`yinqishe_backup.tar.gz`（1.2G）、`yinqishe_data_backup_20261006.tar.gz`（4.2G，均为设备 app 数据备份）。

---

## 5. 进展到哪一步

**已完成并实测**（任务清单 #1–#11 全部 closed，#12 为持久化追加项）：
1. gadget 挂载点与 config 加载方式确认
2. frida-gadget 17.22.1 android-arm64 获取与 SONAME 对齐
3. PC 端脚本移植进 gadget（Frida 17 无内置 Java 桥 → 必须 frida-compile 打 `frida-java-bridge`）
4. APK 重打包 + 重签
5. 装机验证（不使用 PC frida）
6. 无截图的播放量化指标体系
7. 卡顿环节定位（网络缓冲 vs 渲染路径 → 判清是 demuxer 前向缓冲）
8. readahead 拦截修复装机验证
9. 修复「兜底只作用于旧 handle」+「initialize 前设置无效」两个缺陷（v4）
10. 服务端 1 前缀权限回归的复现与判定（额度只按真实观看计）
11. 装回含 `full_play` 的正式包，正片 + 流畅双达标
12. **续号状态持久化**：`MINT_MAX=4`（按进程）换成 `gadget_mint.txt` 的 30 分钟滚动窗口（≤6 个/窗口），配合 `gadget_bak.txt` 的账号落盘 → 连续观看不再依赖重启进程；已实测「窗口内拒绝换号 / 窗口外自动换号 / 落盘剪枝」三条路径
13. **全屏方向自适应**：竖屏视频全屏保持竖屏、横屏视频全屏放行转横（`orient_fix.js`），实测 544x720 改写、1280x720 放行、切换片源后再进全屏改写三种序列，用户真机复核通过

**没完成 / 未覆盖**（都是可选加固，不影响当前可用）：
- **未做防篡改对抗**：服务端若改成把 flag 与 path/videoId 一起入签，或校验 `deviceId` 与账号绑定关系，当前替换会失效。届时需要回到现签路线（`getVideoById` 不烧额度，可继续 mint 游客）。
- **未做「卡顿时自动降级码率/切换分片源」**：只放大了缓冲，没做多源回退。
- **未做后台常驻/断线重连的显式测试**：长时间（>30 分钟）、锁屏、切后台场景只覆盖到 12 分钟级监控。
- **未做 iOS/其他设备验证**：所有偏移、ABI、签名窗口都只针对本机 arm64 + 这一版 APK。
- **额度状态没有用户可见提示**：换号已持久化、不再需要重启，但额度耗尽/被节流时只在 `gadget_play.log` 里留痕，界面上没有任何状态；要做的话得从 Dart 侧另找渲染点。
- **`API_HOST` 仍是运行时学习、未落盘**：每次冷启动要等 app 自己发出第一条 authPath 才校正；这条持久化没做（当前只落 `gadget_bak.txt` / `gadget_mint.txt` 两份状态）。

---

## 6. 风险与运维须知（务必遵守）

1. **禁止在这台设备上用 PC frida spawn**。已实测：spawn 会污染 zygote 的 `libstagefright` PLT，导致进视频页 SIGILL 闪退，属设备级问题；一旦污染只能 `reboot` 恢复。只用内置 gadget 的重打包 APK + `attach`/无注入。
2. **`static_patch/` 只读**。里面有 `libapp.so.original`、`patch.keystore`、`patched_signed.apk`，是重建链路唯一基线，不要覆盖。
3. **guest 账号要省着用**：换号节流是**跨进程的 30 分钟滚动窗口 ≤6 个**（`gadget_mint.txt`，不要手工清空或伪造，它是唯一的防刷爆闸门）；`quota_probe.py`/`mint_probe.py` 每次跑都会真开账号，验证前先想清楚是否必要。要单独验证节流逻辑，就用改名 `gadget_bak.txt` + 伪造窗口内时间戳的办法触发 `换号节流` 分支，验证完记得把两份文件恢复（本轮副本在 `creds/bak_traveler.txt`、`creds/mint_window.txt`）。
4. **签名窗口 ~10 分钟**：改 `KEY_TTL`（现 420s）或 `REFRESH_MS`（现 240s）时要留余量，否则替换后的 key 会在播放中途过期 → 变成 403 而不是 12s。
5. **测量陷阱**（已写进项目记忆）：唯一可信指标是「播放位置 vs 墙钟漂移」。连续点按会触发播放器 ±15s 快进手势，产生 `步进 61s / 墙钟 45s` 这种假象；`fps_meter.py` 在 `BLAST#2271` 层未建立时会回退到容器层而假报 0 帧。采样间隔 ≥40s，且期间不要并发点击。
6. **Git Bash 下 adb 参数**要 `MSYS_NO_PATHCONV=1`，否则 `/data/...` 会被改写成 Windows 路径。
7. 重装或换版本后先核对：设备 `libgadget.script.so` 的 md5 是否等于本地 `gadget/full_play.script.js`。

---

## 7. 快速自检（一条命令确认还活着）

```bash
cd /d/shuaji/14 && MSYS_NO_PATHCONV=1 adb -s 55434d6 shell \
  "su -c 'tail -6 /data/data/com.example.yinqishe_app/gadget_play.log'"
```
看到 `[替换] 12秒 key -> 正片 key` 与 `定时续期 … 状态码 200 … 已现签 1 前缀 key` 即为正常；
出现 `换号节流：30 分钟内已换 N 个，等下一轮` 说明额度耗尽且窗口内已换满 6 个 —— 不用管，窗口过期后（≤30 分钟）会自动续号；
只出现 `遇到 0 前缀 key 但还没有可用 1 前缀 key` 且没有节流行，才说明现签链路真断了（token 失效），跑一次 `python mint_probe.py <HOST>` 重新拿到有效游客 token 并更新 `creds/`。
全屏方向一眼核查：`su -c 'grep 方向 /data/data/com.example.yinqishe_app/gadget_play.log | tail -5'`，竖屏片应见 `-> 改为 12`，横屏片应见 `横屏，放行`。
状态文件与设备端脚本核查：
```bash
cd /d/shuaji/14
MSYS_NO_PATHCONV=1 adb -s 55434d6 shell "su -c 'cd /data/data/com.example.yinqishe_app && ls -l gadget_bak.txt gadget_mint.txt && cat gadget_mint.txt'"
MSYS_NO_PATHCONV=1 adb -s 55434d6 shell "su -c 'find /data/app -name libgadget.script.so -exec md5sum {} \;'"
# 期望：md5 = 8be5ad61336e358c2115c56c08682e98（= 本地 gadget/full_play.script.js）
```
