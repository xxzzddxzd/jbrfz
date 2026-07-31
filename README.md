# jbrfz

Cookie Run: Crumble tooling.

## iosver

Python 号池 bot：`gen` 养号 / `inv` 邀请 / `list` 查看。

```bash
cd iosver
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./main.py gen 5
./main.py inv <mid|url> -c 3
./main.py list --unused --ready
```

详见 [iosver/USAGE.md](iosver/USAGE.md)。
