# iosver 使用说明

Cookie Run: Crumble 号池 bot。命令：`gen` / `inv` / `guild` / `list`。

固定：
- endpoint: `https://cc-gameserver-client.live.prod.devslime.cloud:443`
- 推图: **1–30**
- resource_key: 响应头自动更新

---

## 安装

```bash
cd iosver
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

```bash
# 推荐：根目录主入口
./main.py <子命令> ...
./main.py -v gen 1

# 等价
./main.py <子命令> ...
```

| 全局 | 说明 |
|------|------|
| `-h` | 帮助 |
| `-v` / `--verbose` | DEBUG：每关点位 + HTTP 细节 |
| `-q` / `--quiet` | 仅警告/错误 + 最终 JSON |
| （默认） | INFO：建号/通关关键进度/邀请结果 |

---

## 主流程

```text
gen [n]           建号 + 推 1-30 + 入 sqlite（不邀请）
inv 目标 [-c N]   取未使用号登录并邀请
guild             批量执行公会签到、研究、捐赠并退出
list              查看号池
```

```bash
./main.py gen 10
./main.py inv GNWPX5251 -c 3
./main.py guild --gname 'ahhhha' --gmname 'absdbld' --count 20
./main.py list --unused --ready
```

---

## 命令参数

### `gen [n]`

| 参数 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `n` | 否 | 无限 | 数量；省略则直到 Ctrl+C |
| `--db` | 否 | `data/accounts.db` | sqlite |
| `--stop-on-error` | 否 | 关 | 出错退出 |

### `inv mid|url`

| 参数 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `mid\|url` | **是** | — | 邀请目标 mid 或完整链接 |
| `-c` / `--count` | 否 | `1` | 取号数量 |
| `--db` | 否 | `data/accounts.db` | sqlite |
| `--any` | 否 | 关 | 允许未 ready 号 |

- 成功：`used=1`，回填 `inviter_mid`
- 失败：`invalid=1`
- 池空：exit `2`

### `guild`

```bash
./main.py guild --gname 'ahhhha' --gmname 'absdbld' --count 20
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--gname` | **是** | 公会名称 |
| `--gmname` | **是** | 会长名称，用于防止同名公会误匹配 |
| `--count` | **是** | 需要成功完成 SOP 的账号数 |
| `--db` | 否 | sqlite，默认 `data/accounts.db` |

SOP：登录 → 加入 → 领取签到奖励 → 免费研究 3 次 → 捐赠至钻石不足 → 退出。

- 账号必须 `ready=1`、`next_stage>30`、`invalid=0`；`used` 不影响选择。
- `accounts.guild` 保存成功退出时间，24 小时内不会再次选择。
- 首次目标搜索会展示公会摘要并要求确认。
- 确认结果写入 `guild_targets`；相同公会名和会长名后续直接复用 `guild_id`。
- 账号全部处于冷却或已尝试完时结束；最终 JSON 的 `count` 是实际成功数。

### `list`

| 参数 | 默认 | 说明 |
|------|------|------|
| `--db` | `data/accounts.db` | sqlite |
| `--all` | 关 | 全部 |
| `--unused` | 关 | 未使用 |
| `--ready` | 关 | ready |
| `--limit` | `50` | 条数 |

---

## sqlite 字段

登录全量 + 状态：`mid, guest_secret, refresh_token, game_access_token, oven_access_token, resource_key, endpoint, email, device_json, inviter_mid, next_stage, diamond_balance, guild, used, ready, invalid, note, created_at, updated_at`

| 标志 | 含义 |
|------|------|
| `used` | 已用于邀请 |
| `ready` | 已打完 1–30 |
| `invalid` | 作废 |
| `diamond_balance` | 最近一次从服务端同步的钻石余额 |
| `guild` | 最近一次成功退出公会的 Unix 时间；用于 24 小时冷却 |

`guild_targets` 保存已确认的 `gname + gmname → guild_id` 及公会摘要，避免多账号和后续运行重复搜索、重复确认。

兼容旧版 sqlite：执行 `guild` 时会先自动、幂等地补齐 `invalid`、`diamond_balance`、`guild` 列并创建 `guild_targets`，已有账号和状态数据保持不变。

---

## 目录

```text
iosver/
├── USAGE.md / README.md
├── requirements.txt
├── crumble_bot/           # gen / inv / guild / list 实现
├── configs/               # 账号 yaml 快照（调试）
└── data/
    ├── accounts.db        # 号池主库
    ├── accounts/*.json
    └── samples/*.bin      # 推关模板
```

---

## 退出码

| 码 | 含义 |
|----|------|
| 0 | 成功 |
| 1 | 失败 |
| 2 | 号池空（inv） |
| 130 | Ctrl+C（gen） |
