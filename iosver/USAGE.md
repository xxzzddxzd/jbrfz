# iosver 使用说明

Cookie Run: Crumble 号池 bot。命令：`gen` / `inv` / `daily` / `guild` / `list`。

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
daily            批量执行每日登录、邮箱领取和邮箱广告
guild             批量执行公会签到、研究、捐赠并退出
list              查看号池
```

```bash
./main.py gen 10
./main.py inv GNWPX5251 -c 3
./main.py daily
./main.py guild --gname 'ahhhha' --gmname 'absdbld' --count 20 --totalcount 200
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

### `daily`

```bash
./main.py daily
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--db` | 否 | sqlite，默认 `data/accounts.db` |

SOP：重新登录 → `SignUp` 每日登录同步 → 查看邮箱并全部领取普通附件 → 未达到每日上限时领取邮箱广告奖励。

- 账号必须 `ready=1`、`next_stage>30`、`invalid=0`；`used` 和公会冷却均不影响选择。
- 命令不限制账号数量，会扫描所有符合条件的账号。
- `accounts.daily` 保存最近一次成功完成整套 daily SOP 的 Unix 时间；同一 `Asia/Shanghai` 自然日已经完成的账号会跳过。
- 只有整套 SOP 成功才更新时间；中途失败的账号保留为可执行状态，下一次运行会重试。
- 每个账号的 `mailbox` 记录信件总数、可领取数、实际领取数、附件汇总及领取前后钻石；普通邮件只批量领取“未领取且有附件”的服务端邮件。
- `mailbox.advertisement` 记录广告 ID、每日计数前后值、请求/成功次数和响应奖励。10101 的信箱广告 ID 为 `1673636113`、每日上限 1 次、配置奖励 1000 钻石；已达到每日上限时不会请求。
- 广告领取使用 `CrumbleService/ReceiveMailAdvertisementReward`。成员函数暴露 `advertisement_data_id` 和可选 `skip_count`；SOP 按正常客户端行为不发送 `skip_count`。
- 最新资源密钥、登录令牌和钻石余额会回写 SQLite。`totals` 汇总登录、普通邮件、广告及钻石变化。

### `guild`

```bash
./main.py guild --gname 'ahhhha' --gmname 'absdbld' --count 20 --totalcount 200
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--gname` | **是** | 公会名称 |
| `--gmname` | **是** | 会长名称，用于防止同名公会误匹配 |
| `--count` | **是** | 每个账号最多执行的钻石捐赠次数，只限制付费次数 |
| `--totalcount` | **是** | 跨账号累计的免费+付费有效研究次数 |
| `--db` | 否 | sqlite，默认 `data/accounts.db` |

SOP：登录鉴权 → 加入 → 领取公会签到奖励 → 免费研究默认 3 次 → 最多执行 `--count` 次钻石捐赠 → 读取公会变化 → 退出。

- 账号必须 `ready=1`、`next_stage>30`、`invalid=0`；`used` 不影响选择。
- 普通研究按 1 次计入 `--totalcount`；暴击按服务端返回的实际贡献增量计，当前一次暴击按 3 次计。
- 每次动作完成后检查全局有效次数，达到 `--totalcount` 后立即停止当前账号的后续研究并退出；由于暴击结果在响应后才知道，最终值最多可能超过目标一个暴击带来的增量差。
- 单个账号钻石不足时会提前停止其付费捐赠、退出公会并继续下一个可用账号。
- 所有账号累计达到 `--totalcount` 或可用账号全部尝试完后结束。
- `accounts.guild` 保留为成功退出时间的兼容字段；明确的最近加入、退出时间分别保存于 `guild_joined_at`、`guild_left_at`。
- 首次目标搜索会展示公会摘要并要求确认。
- 确认结果写入 `guild_targets`；相同公会名和会长名后续直接复用 `guild_id`。
- 最终 JSON 的 `count` 是每账号付费上限，`requested_totalcount` 是目标，`totalcount` 是实际累计有效次数，`account_count` 是成功完成流程的账号数。
- `totals.free_research_count` 与 `totals.donation_count` 是实际 RPC 动作数；`totals.effective_research_count` 包含暴击倍率；`totals.diamond_spent` 是总钻石消耗。
- 每个账号的 `guild_progress` 记录公会等级、经验、成员贡献、研究点和每日研究次数的前后值及增量；退出前会再次读取公会详情。
- 顶层 `guild.level_before/after/change` 和 `experience_before/after/gained` 汇总整个批次的公会变化。
- `guild` 只做登录鉴权，不执行 `SignUp` 每日同步，也不调用邮箱附件或广告奖励接口；这些动作只属于 `daily`。
- 捐赠停止响应中的 `Owned amount` 用于记录最终钻石，并以“最终钻石 + 本次消耗”计算捐赠前余额后回写 SQLite。

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

登录全量 + 状态：`mid, guest_secret, refresh_token, game_access_token, oven_access_token, resource_key, endpoint, email, device_json, inviter_mid, next_stage, diamond_balance, guild, daily, guild_last_id, guild_joined_at, guild_left_at, guild_free_research_total, guild_paid_research_total, guild_effective_research_total, guild_super_success_total, guild_diamond_spent_total, used, ready, invalid, note, created_at, updated_at`

| 标志 | 含义 |
|------|------|
| `used` | 已用于邀请 |
| `ready` | 已打完 1–30 |
| `invalid` | 作废 |
| `diamond_balance` | 最近一次从服务端同步的钻石余额 |
| `guild` | 最近一次成功退出公会的 Unix 时间；兼容字段，用于 24 小时冷却 |
| `guild_last_id` | 最近一次执行流程的公会 ID |
| `guild_joined_at` | 最近一次成功加入公会的 Unix 时间 |
| `guild_left_at` | 最近一次成功退出公会的 Unix 时间 |
| `guild_free_research_total` | 历史免费研究 RPC 次数 |
| `guild_paid_research_total` | 历史钻石捐赠 RPC 次数 |
| `guild_effective_research_total` | 历史有效研究次数，包含暴击倍率 |
| `guild_super_success_total` | 历史暴击次数 |
| `guild_diamond_spent_total` | 历史公会捐赠钻石消耗 |
| `daily` | 最近一次成功完成 daily SOP 的 Unix 时间；用于当天去重 |

`guild_targets` 保存已确认的 `gname + gmname → guild_id` 及公会摘要，避免多账号和后续运行重复搜索、重复确认。

`guild_runs` 为每次账号公会流程保存一条历史，包括加入/退出时间、免费/付费动作数、免费/付费有效次数、暴击次数、钻石消耗、停止原因及错误。

兼容旧版 sqlite：执行 `daily`、`guild` 或其他打开账号库的指令时会自动、幂等地补齐上述字段并创建 `guild_targets`、`guild_runs`，已有账号和状态数据保持不变。

---

## 目录

```text
iosver/
├── USAGE.md / README.md
├── requirements.txt
├── crumble_bot/           # gen / inv / daily / guild / list 实现
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
