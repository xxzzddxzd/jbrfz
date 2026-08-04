#!/usr/bin/env python3
"""iosver 主入口：承接 gen / inv / daily / guild / list 参数。

用法:
  ./main.py gen 5
  ./main.py inv GNWPX5251 -c 3
  ./main.py daily
  ./main.py guild --gname ahhhha --gmname absdbld --count 20 --totalcount 200
  ./main.py list --unused --ready
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def _ensure_venv_python() -> None:
    """若存在 .venv 且当前不是 venv 解释器，则切换到 .venv 重新执行。"""
    venv_py = _ROOT / ".venv" / "bin" / "python"
    if not venv_py.exists():
        return
    # already inside this venv?
    if Path(sys.prefix).resolve() == (_ROOT / ".venv").resolve():
        return
    if os.environ.get("CRUMBLE_BOT_MAIN_REEXEC") == "1":
        return
    env = os.environ.copy()
    env["CRUMBLE_BOT_MAIN_REEXEC"] = "1"
    os.execve(str(venv_py), [str(venv_py), str(Path(__file__).resolve()), *sys.argv[1:]], env)


def _bootstrap_path() -> None:
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))


def main() -> int:
    _ensure_venv_python()
    _bootstrap_path()
    from crumble_bot.cli import main as cli_main

    return int(cli_main())


if __name__ == "__main__":
    raise SystemExit(main())
