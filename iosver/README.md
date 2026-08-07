# Crumble Bot (`iosver`)

号池：养号 + 邀请。

```bash
cd iosver
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python -m crumble_bot gen 5
.venv/bin/python -m crumble_bot inv GNWPX5251 -c 3
.venv/bin/python -m crumble_bot daily
.venv/bin/python -m crumble_bot guild public --gname 'ahhhha' --gmname 'absdbld' --count 20 --totalcount 200
.venv/bin/python -m crumble_bot guild private --gname 'ahhhha' --gmname 'absdbld' --count 20 --totalcount 200
.venv/bin/python -m crumble_bot guild private return
.venv/bin/python -m crumble_bot guild private return 1
.venv/bin/python -m crumble_bot guild joblist
.venv/bin/python -m crumble_bot guild --gname 'ahhhha' support
.venv/bin/python -m crumble_bot list --unused --ready
```

完整说明：[USAGE.md](./USAGE.md)

常驻公会的 `guild --gname <name> daily` 会为每个活跃成员执行完整的日常
登录奖励（放置/阶段奖励、邮箱及广告）、一次碎屑副本，再执行签到、研究和
支援；账号日常成功后会同步写入 `daily` 时间戳。

如果只需要支援公会成员，可执行 `guild --gname <name> support`；它只读取支援中心，
并按当天的 `guild_support_actions` 记录去重，不会触发其他日常动作。

`guild private` 是可重复执行的状态机：相同参数会继续原任务并输出当前进度、卡点和 `next_action`；未达到目标时不会提前交还会长。`--master-mid` 可省略，程序会复用已有任务的代理会长，或自动选择钻石最少的可用账号并将其固定记录到任务中。

入口任选其一：

```bash
./main.py gen 5
./main.py inv GNWPX5251 -c 3
./main.py daily
./main.py guild public --gname 'ahhhha' --gmname 'absdbld' --count 20 --totalcount 200
./main.py guild private --gname 'ahhhha' --gmname 'absdbld' --count 20 --totalcount 200
python main.py guild private return
python main.py guild private return 1
python main.py guild joblist
./main.py list --unused --ready

# 或
.venv/bin/python -m crumble_bot gen 5
```


共 5 个命令：`gen` / `inv` / `daily` / `guild` / `list`。
