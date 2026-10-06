# creds/ —— 本地凭据目录（内容全部被 `.gitignore` 排除）

取证脚本 `api_probe.py` / `mint_probe.py` / `quota_probe.py` 用**相对路径 `creds/<文件>`** 读凭据，
缺失时直接抛 `SystemExit: 缺少 creds/<文件>`。克隆仓库后这个目录只有本说明，需要自己填。

| 文件 | 内容 | 怎么拿到 |
|---|---|---|
| `token.txt` | app 当前会话的 JWT（请求头 `aut`） | 装了 gadget 后看设备日志：`adb shell su -c 'cat /data/data/com.example.yinqishe_app/gadget_play.log'`，`StorageService._read` 那行带出 token；或 `python ui_probe.py log 60` |
| `device_id.txt` | 与该 token 配套的 `deviceId`（40 hex） | 同一份日志的 `deviceId=` 字段 |
| `aut_token.txt` | token 的一次快照，便于对照轮换 | 手工保存 |
| `new_traveler_token.txt` `new_traveler_dev.txt` | 一次性游客账号的 JWT + deviceId（`mint_probe.py` / `quota_probe.py` 的入口凭据） | 用 `token.txt` 调 `POST /api/user/traveler` 开一个，或直接从 `mint_token.txt` 复制 |
| `mint_token.txt` `mint_dev.txt` | `mint_probe.py` 的运行输出 | 跑一次 `python mint_probe.py https://<当前 API 域名>` 自动写入 |
| `free_key.txt` | 一个已知 1 前缀（正片）`auth_key` 样本，用来核对签名结构 | 从日志 `[替换] … 正片 key` 行取 |
| `bak_traveler.txt` | 设备侧 `gadget_bak.txt` 的副本（`deviceId\nJWT`，即 gadget 当前用的备用游客） | 见 REPORT §7 |
| `mint_window.txt` | 设备侧 `gadget_mint.txt` 的副本（每行一个 mint 时刻的 epoch） | 见 REPORT §7 |

注意：

- 游客账号的 `freeWatches=50` **只在真实观看时消耗**，`getVideoById` 现签不扣；`quota_probe.py` / `mint_probe.py` 每跑一次都会真开一个号，跑之前想清楚是否必要。
- JWT 有效期取决于服务端；失效表现是现签连续返回非 200，重新取 `token.txt` 即可。
- **不要把这里的任何文件提交**：`.gitignore` 用 `/creds/*` + `!creds/README.md` 兜住，`git add creds/` 也只会带上本说明。
