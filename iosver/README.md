# Crumble Bot (`iosver`)

号池：养号 + 邀请。

```bash
cd iosver
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python -m crumble_bot gen 5
.venv/bin/python -m crumble_bot inv GNWPX5251 -c 3
.venv/bin/python -m crumble_bot daily
.venv/bin/python -m crumble_bot guild --gname 'ahhhha' --gmname 'absdbld' --count 20 --totalcount 200
.venv/bin/python -m crumble_bot list --unused --ready
```

完整说明：[USAGE.md](./USAGE.md)

入口任选其一：

```bash
./main.py gen 5
./main.py inv GNWPX5251 -c 3
./main.py daily
./main.py guild --gname 'ahhhha' --gmname 'absdbld' --count 20 --totalcount 200
./main.py list --unused --ready

# 或
.venv/bin/python -m crumble_bot gen 5
```


共 5 个命令：`gen` / `inv` / `daily` / `guild` / `list`。
