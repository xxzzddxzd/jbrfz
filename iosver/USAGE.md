# iosver 使用说明

Cookie Run: Crumble 号池 bot。仅保留 3 个命令：`gen` / `inv` / `list`。

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
| `-v` / `--verbose` | 详细日志 |

---

## 主流程

```text
gen [n]           建号 + 推 1-30 + 入 sqlite（不邀请）
inv 目标 [-c N]   取未使用号登录并邀请
list              查看号池
```

```bash
./main.py gen 10
./main.py inv GNWPX5251 -c 3
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

登录全量 + 状态：`mid, guest_secret, refresh_token, game_access_token, oven_access_token, resource_key, endpoint, email, device_json, inviter_mid, next_stage, used, ready, invalid, note, created_at, updated_at`

| 标志 | 含义 |
|------|------|
| `used` | 已用于邀请 |
| `ready` | 已打完 1–30 |
| `invalid` | 作废 |

---

## 目录

```text
iosver/
├── USAGE.md / README.md
├── requirements.txt
├── crumble_bot/           # gen / inv / list 实现
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
