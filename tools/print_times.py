#!/usr/bin/env python
"""被 tools/setup_tasks.ps1 调用的小工具：把 config.yaml 里的采集时刻打出来。

为什么不直接在 PowerShell 里用 ``python -c "..."``：
两种语言的引号嵌套极易出错（PowerShell 会把 Python 代码当成表达式解析），
而且一旦报错信息会指向错误的行号，很难查。拆成独立文件最省事。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar.config import cfg          # noqa: E402


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    times = [str(t).strip() for t in (cfg.get("schedule.times", []) or [])]
    times = [t for t in times if t]
    print(" ".join(times))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
