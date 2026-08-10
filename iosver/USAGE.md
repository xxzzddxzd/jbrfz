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
guild public      公开公会：直接加入后批量签到、研究、捐赠并退出
guild private     审批公会：引导临时会长接管、邀请账号入会捐赠并退出
guild init/status/fill/daily/maintain
                  常驻公会：初始化、同步状态、补位、执行每日公会动作、维护
guild support      常驻公会：仅执行支援中心动作
guild private return [ID]
                  列出待交还会长任务，或按数据库 ID 交还原会长
guild joblist     查看 SQLite 中的全部 private 任务清单
list              查看号池
```

```bash
./main.py gen 10
./main.py inv GNWPX5251 -c 3
./main.py daily
./main.py guild public --gname 'ahhhha' --gmname 'absdbld' --count 20 --totalcount 200
./main.py guild private --gname 'ahhhha' --gmname 'absdbld' \
  --count 20 --totalcount 200
python main.py guild private return
python main.py guild private return 1
python main.py guild joblist
./main.py list --unused --ready

# 常驻公会（按公会等级自动读取容量，保留 2 个空位）
python main.py guild --gname 'ahhhha' init --gmname 'absdbld'
python main.py guild --gname 'ahhhha' status
python main.py guild --gname 'ahhhha' fill
python main.py guild --gname 'ahhhha' daily
python main.py guild --gname 'ahhhha' support
python main.py guild --gname 'ahhhha' maintain
```

### 常驻公会管理

常驻模式与旧的 `public/private` 临时进会流程相互独立。`init` 会搜索并缓存
公会 ID、会长、加入方式、等级和成员容量，默认把容量减去 2 作为常驻账号目标；
容量按当前客户端的公会等级表读取，`--capacity` 只作为未知等级或旧版本数据库的兜底。
`init` 只初始化配置，不选择代理会长，也不发送邀请。

```bash
python main.py guild --gname 'ahhhha' init --gmname 'absdbld'
python main.py guild --gname 'ahhhha' status
python main.py guild --gname 'ahhhha' fill       # 只补充缺少的常驻账号
python main.py guild --gname 'ahhhha' fill --reserve-slots 0  # 用受控账号填满剩余席位
python main.py guild --gname 'ahhhha' daily      # 签到、免费研究、钻石捐赠、支援
python main.py guild --gname 'ahhhha' support    # 只执行支援中心；终端单行显示进度
python main.py guild --gname 'ahhhha' maintain   # status → fill → daily
# 需要完整结构化结果时加 --json；默认终端只显示简明摘要
```

- `--gname` 支持已初始化公会名称的唯一前缀；`--gmname` 只在 `init` 用于校验会长。
- `status` 会在线同步成员名单和等级；SQLite 中保存成员 MID、名称/等级快照、角色、槽位、
  最近签到/捐赠/支援时间以及当天动作结果。
- `fill` 先刷新当前公会成员，再只处理缺少的常驻槽位，不退出已有成员；每天最多提交
  50 个入会申请，并同时检查实际容量，避免外部成员导致超员。公开公会直接加入；
  审批制公会由各候选账号自己提交申请，命令返回 `next_action=approve_applications`，
  由用户在手机同意后重跑 `fill`/`maintain`。申请状态不会冒充已入会成员。
- `daily` 每次先同步手机上最新的公会成员状态，再对每个常驻账号每天只执行一次；重复执行会读取 `guild_daily_actions` 并跳过已完成账号。
  公会等级升级后的免费次数、钻石余额和支援请求都会写回 SQLite；付费捐赠在下一次
  单次成本超过 300 钻石前停止。
- `support` 只执行支援中心，支援列表只查询一次，后续账号直接提交支援；达到支援上限
  即停止。它不会触发登录奖励、邮箱、碎屑副本、签到或研究；同一天已成功支援的请求
  会从 `guild_support_actions` 去重。执行期间会在 stderr 显示账号处理数、累计成功/失败数
  和当前账号的进度条；`--quiet` 可关闭。
- `maintain` 遇到容量不足、招募 50 人上限、待审批申请或缺少账号时不会等待，直接返回
  `state` 与 `next_action`，按提示处理后重跑同一命令。

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

SOP：重新登录 → `SignUp` 每日登录同步 → 刷新并领取放置奖励 → 补领每日 1 次免费放置加成 → 补领每日 3 次放置广告加成 → 查看邮箱并全部领取普通附件 → 未达到每日上限时领取邮箱广告奖励 → 执行 1 次碎屑副本（已完成当天则跳过）。

- 账号必须 `ready=1`、`next_stage>30`、`invalid=0`；`used` 和公会冷却均不影响选择。
- 命令不限制账号数量，会扫描所有符合条件的账号。
- `accounts.daily_state_json` 按 `Asia/Shanghai` 自然日保存各 action 的版本、状态和完成时间；再次执行只补跑缺失、失败或版本变化的 action。每项成功后立即写库，因此后续步骤失败不会丢失前面的进度。
- 当前 action 为 `stage_offline`、`stage_bonus_free`、`stage_bonus_ad`、`mail_claim_all`、`mail_ad`、`crumble_dungeon`。以后新增 action 或提高其版本号，当天已跑过旧 SOP 的账号也会自动进入补跑队列。
- `accounts.daily` 继续保存当前整套 SOP 全部完成的汇总时间，但不再作为唯一跳过条件。旧数据库执行命令时会自动增加 JSON 列；同日旧 `daily` 记录会映射为前五项已完成，并补跑新增的碎屑副本。
- 放置奖励先调用 `AdventureService/ReceiveStageAutoProductionRewards` 的 `OFFLINE_STACK` 刷新服务端累计值；存在可领取累计奖励时，再以 `OFFLINE` 领取。成员函数同时暴露可选的全局/当前关卡未上报击杀数；daily 没有本地战斗增量，因此不发送这两个可选字段。
- 放置加成使用 `AdventureService/ReceiveStageBonusAutoProductionRewards`。免费请求为空；广告请求携带 10101 广告 ID `1246517436`。`SignUp` 中的当日免费计数和广告计数用于只补领剩余次数，免费上限 1 次、广告上限 3 次。
- 每个账号的 `stage_rewards.offline` 记录累计时长、待领取/已领取奖励和请求次数；`stage_rewards.bonus` 记录免费及广告计数前后值、实际领取次数、奖励类型与奖励汇总。
- 每个账号的 `mailbox` 记录信件总数、可领取数、实际领取数、附件汇总及领取前后钻石；普通邮件只批量领取“未领取且有附件”的服务端邮件。
- `mailbox.advertisement` 记录广告 ID、每日计数前后值、请求/成功次数和响应奖励。10101 的信箱广告 ID 为 `1673636113`、每日上限 1 次、配置奖励 1000 钻石；已达到每日上限时不会请求。
- 广告领取使用 `CrumbleService/ReceiveMailAdvertisementReward`。成员函数暴露 `advertisement_data_id` 和可选 `skip_count`；SOP 按正常客户端行为不发送 `skip_count`。
- 碎屑副本使用 `DungeonService/StartCrumbleDungeonBattle` 和 `FinishCrumbleDungeonBattle`，读取 `SignUp` 中账号最近使用的队伍；服务端提示当天已完成时按成功跳过处理。
- 最新资源密钥、登录令牌和钻石余额会回写 SQLite。`totals` 汇总登录、放置奖励、放置免费/广告加成、普通邮件、邮箱广告、碎屑副本及钻石变化。

### `guild joblist`

```bash
python main.py guild joblist
python main.py guild joblist --db /path/to/accounts.db
```

- 只读取 SQLite，不登录账号，也不调用游戏接口。
- `jobs` 输出全部状态的 private 任务，不只限于待退还任务；其中包含稳定的数据库 `id`、公会名称和 ID、代理会长、原会长、当前/目标/剩余有效次数、账号状态汇总和错误信息。
- `return_pending=true` 表示流程已经达到目标、正在等待交还；无论当前 job 状态为何，`return_command` 都可用于显式要求交还会长。
- `status_counts` 按状态汇总任务数，便于区分 `awaiting_donors`、`awaiting_recruitment_reset`、`awaiting_master_return` 和 `complete`。

### `guild public`

```bash
./main.py guild public --gname 'ahhhha' --gmname 'absdbld' --count 20 --totalcount 200
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--gname` | **是** | 公会名称 |
| `--gmname` | **是** | 会长名称，用于防止同名公会误匹配 |
| `--count` | **是** | 每个账号的免费+钻石有效研究次数上限，暴击按倍率累计 |
| `--totalcount` | 否 | 跨账号累计的免费+付费有效研究次数；省略时最多处理 50 个账号 |
| `--db` | 否 | sqlite，默认 `data/accounts.db` |

SOP：登录鉴权 → 直接加入公开公会 → 领取公会签到奖励 → 优先执行当前公会等级剩余的免费研究 → 继续钻石捐赠，单账号有效次数达到 `--count` 后退出并切换账号。

- 仅接受搜索结果 `join_method=immediate` 的公开公会；审批制公会会停止并提示使用 `guild private`。
- 账号必须 `ready=1`、`next_stage>30`、`invalid=0`；`used` 不影响选择。
- 普通免费研究、普通钻石研究均按 1 次计入 `--count` 和 `--totalcount`；发生暴击时按服务端返回的实际贡献增量同时计入两者，当前一次暴击按 3 次计。
- 10101 的免费研究上限由公会等级决定：1–3 级 3 次、4–6 级 4 次、7–9 级 5 次、10–11 级 6 次、12–13 级 7 次、14–15 级 8 次。
- 每次免费或钻石研究后都读取服务端返回的公会等级和今日已用次数；若本次研究使公会升级并新增免费次数，会先把新增的免费次数用完，再继续钻石捐赠。
- 钻石捐赠没有从 10101 客户端发现固定每日次数上限；27 是价格表最后一档而不是次数上限，第 27 次及以后均按 10000 钻石。单账号实际钻石 RPC 次数取决于免费次数和暴击结果，不再等于 `--count`。
- 每次研究动作完成后检查该账号有效次数；达到 `--count` 后立即退出公会并切换下一个账号。由于暴击倍率在响应后才知道，单账号最终值可能略高于 `--count`。
- 每次动作完成后检查全局有效次数，达到 `--totalcount` 后立即停止当前账号的后续研究并退出；由于暴击结果在响应后才知道，最终值最多可能超过目标一个暴击带来的增量差。
- 省略 `--totalcount` 时不设置跨账号有效次数目标：每个账号仍按 `--count` 执行，最多处理 50 个成功入会账号；若服务端提前拒绝入会则立即结束。
- 单个账号钻石不足时会提前停止其付费捐赠、退出公会并继续下一个可用账号。
- 所有账号累计达到 `--totalcount` 或可用账号全部尝试完后结束。
- `accounts.guild` 保留为成功退出时间的兼容字段；明确的最近加入、退出时间分别保存于 `guild_joined_at`、`guild_left_at`。
- 首次目标搜索会展示公会摘要并要求确认。
- 确认结果写入 `guild_targets`；相同公会名和会长名后续直接复用 `guild_id`。
- 最终 JSON 的 `count` 是每账号有效次数上限，`requested_totalcount` 是目标，`totalcount` 是实际累计有效次数，`account_count` 是成功完成流程的账号数。
- `totals.free_research_count` 与 `totals.donation_count` 是实际 RPC 动作数；`totals.effective_research_count` 包含暴击倍率；`totals.diamond_spent` 是总钻石消耗。
- 每个账号的 `guild_progress` 记录公会等级、经验、成员贡献、研究点、免费上限/已用/剩余次数、今日钻石捐赠次数及下一次钻石价格；退出前会再次读取公会详情。
- 顶层 `guild.level_before/after/change` 和 `experience_before/after/gained` 汇总整个批次的公会变化。
- `guild` 只做登录鉴权，不执行 `SignUp` 每日同步，也不调用邮箱附件或广告奖励接口；这些动作只属于 `daily`。
- 捐赠停止响应中的 `Owned amount` 用于记录最终钻石，并以“最终钻石 + 本次消耗”计算捐赠前余额后回写 SQLite。

#### `guild private`

```bash
./main.py guild private \
  --gname 'ahhhha' --gmname 'absdbld' \
  --count 20 --totalcount 200
```

| 参数 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `--gname` | **是** | — | 公会名称 |
| `--gmname` | **是** | — | 首次确认时的原会长名称 |
| `--master-mid` | 否 | 自动复用/选择 | 显式指定临时接管会长并负责发送邀请的自控账号 A |
| `--count` | **是** | — | 每个 B 的免费+钻石有效研究次数上限，暴击按倍率累计 |
| `--totalcount` | 否 | — | 所有 B 的免费+付费有效研究总目标；省略时最多处理 50 个 B |
| `--confirm` | 否 | false | 新参数与未完成任务不一致时，确认更新原任务的 `count/totalcount` 并继续 |
| `--db` | 否 | `data/accounts.db` | sqlite |

状态流程：A 发送入会申请 → 等待原会长人工审批 → 等待原会长人工将会长转给 A → A 逐个邀请 B → B 接受邀请后直接入会 → 复用与 public 相同的签到/研究/捐赠/退出 SOP。

- 同一自然日内同一组参数对应一个固定任务；反复执行同一命令会读取原 job 和累计进度，
  不会从零开始。跨到下个自然日后会新建当日批次，历史 job 保留在任务清单中。
- 未完成任务默认不允许改变 `--count/--totalcount`；确需调整目标时，用新参数重跑并增加 `--confirm`。程序会保留既有累计进度，只更新任务参数后继续。
- 审批公会每天最多邀请 50 个 B。命令启动时要求 `--totalcount <= --count × 50`；不满足时会在登录、搜索或更新任务前立即退出，并提示当前最大值以及达成目标所需的最小 `--count`。
- `--totalcount` 是当日批次目标：同一自然日重复执行已完成任务会保持幂等；下个
  `Asia/Shanghai` 自然日重跑相同命令时，会保留历史任务并新建当日批次，重新输出当前
  需要的会长转移步骤。
- 省略 `--totalcount` 时，private 任务以 50 个成功账号为批次上限；服务端提前返回无法继续招募时，本批次直接进入 `awaiting_master_return`，不等待次日重置。
- `guild_private_jobs.paid_count_per_account` 是兼容旧 SQLite 保留的字段名；当前保存的是单账号有效次数上限，旧数据库无需迁移即可继续使用。
- 省略 `--master-mid` 时，程序优先复用该公会已有 job 中的 A；没有 job 时，从满足执行条件的账号中选择钻石余额最低者，并将其 MID 固定写入 `guild_private_jobs.controller_mid`。以后重跑同一命令仍使用该账号。
- 复用 A 前会再次检查账号是否仍在目标公会或已结束 24 小时入会冷却；若上一个批次刚创建但 A 仍在冷却，程序会自动换用其他可用代理，否则直接返回冷却时间，不再发送必然失败的入会请求。
- 自动选择会排除原会长、其他进行中 private job 的代理会长和捐赠账号；候选账号须有登录凭据，并满足 `ready=1`、`invalid=0`、`next_stage>30`、当前不在公会且已结束退会冷却。
- 若该公会存在多个进行中的 private job，省略参数无法唯一确定 A，命令会输出候选 MID 并提示显式传入 `--master-mid`。
- 公会切换公开/审批模式后，若缓存的 `join_method` 与当前命令冲突，程序会按已确认的 `guild_id` 在线重新搜索并更新缓存，不需要手工修改 SQLite，也不会再次要求确认公会。
- 每次 JSON 都包含 `state`、当前/目标/剩余进度和 `next_action`，用于说明当前卡点及下一步操作。
- 未达到 `--totalcount` 且暂时没有可用 B 时，状态保持为 `awaiting_donors`；补充符合条件的账号或等待 24 小时冷却结束后，重跑同一命令继续。
- 只有达到目标后才进入 `awaiting_master_return`；同一自然日内任务已完成时重跑只返回 `target_already_complete`，下个自然日会创建新的每日批次。
- 每次只邀请并处理一个 B，退出后才处理下一个，避免占满公会或遗留批量邀请。
- 同一个 B 在一个 private job 内只使用一次，避免冷却结束后被同一任务重复计算。
- `--count`、`--totalcount`、暴击计数、动态免费次数和钻石统计与 public 完全一致。
- 命令不等待人工操作：发现需要审批或委任时立即输出 `next_action` 并退出；操作完成后重跑同一命令继续。
- private 批次结束后状态为 `awaiting_master_return`，JSON 会输出 `original_master_mid`。批次本身不会自动交还会长；可在手机上手动交还，也可使用下面的 `guild private return` 明确执行。
- A 只负责审批流程和邀请，不参与捐赠，也不会自动退出公会。
- `TransferGuildMaster`、`ApplyGuild`、邀请和接受邀请等接口均作为 `Guild` 成员函数提供；申请、邀请和接受邀请由 private 流程内部调用，最终交还只能通过明确的 `private return ID` 或手机操作触发。
- `GetGuildMembers` 和 `BanishGuildMember` 仅作为 `Guild` 成员函数提供，不暴露 CLI。成员查询会解析名称、等级、MID、角色、加入/活跃时间、战力和贡献值；踢人响应会解析当日踢人计数。

常见状态：

| `state` | 含义 / 操作 |
|---------|-------------|
| `awaiting_application_approval` | 手机批准 A 入会，然后将会长委任给 A |
| `awaiting_master_transfer` | A 已入会；手机将会长委任给 A |
| `awaiting_donors` | 目标未达到；补充可用 B 或等待冷却后重跑原命令 |
| `awaiting_master_return` | 目标已达到；执行 `guild private return ID` |
| `complete` | 目标完成且会长已交还，无需操作 |

#### `guild private return [ID]`

```bash
# 列出所有等待交还会长的任务
python main.py guild private return

# 使用 guild_private_jobs.id 交还指定任务
python main.py guild private return 3
```

- 不带 `ID` 时只读 SQLite，列出 `status=awaiting_master_return` 的记录；输出中的 `id` 就是后续命令使用的稳定索引，不是临时列表序号。
- 带 `ID` 时登录该记录的临时会长 A，在线核对目标公会、当前会长和原会长成员资格，再调用 `TransferGuildMaster`。
- 显式提供 `ID` 就代表用户要求立即交还，不要求 job 必须是 `awaiting_master_return`；`running`、`awaiting_donors` 等状态也可以执行。
- 仍然只有当前会长等于记录中的 `controller_mid`，且 `original_master_mid` 仍在成员列表时才会执行，避免转错公会或转错人。
- 成功后再次搜索并确认会长已经变为 `original_master_mid`，随后把任务更新为 `complete`；若此前已人工交还，则不会重复委任，只补记完成状态。
- 可附加 `--db PATH`；默认数据库仍为 `data/accounts.db`。

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

`guild_targets` 保存已确认的 `gname + gmname → guild_id` 及公会摘要，避免多账号和后续运行重复搜索、重复确认。`original_master_mid` 在首次确认时固定记录原会长 MID，后续会长变化不会覆盖。

`guild_runs` 为每次账号公会流程保存一条历史，包括加入/退出时间、免费/付费动作数、免费/付费有效次数、暴击次数、钻石消耗、停止原因及错误。

`guild_private_jobs` 保存审批、等待临时会长、批量执行和等待手动交还等可恢复状态；`guild_private_accounts` 保存每个 B 的邀请 ID、接受状态和对应 `guild_run_id`。

兼容旧版 sqlite：执行 `daily`、`guild` 或其他打开账号库的指令时会自动、幂等地补齐上述字段并创建 `guild_targets`、`guild_runs`、`guild_private_jobs`、`guild_private_accounts`，已有账号和状态数据保持不变。

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
