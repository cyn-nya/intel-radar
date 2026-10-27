#!/usr/bin/env python
"""情报雷达 · 命令行入口

    python run.py serve      启动看板（默认）
    python run.py once       立刻跑一次采集（不看电池）
    python run.py auto       跑一次采集，但遵守电池守卫（适合放进计划任务）
    python run.py diagnose   一键体检：AI / GitHub / Bing / 通知通道都测一遍
    python run.py status     打印当前状态
    python run.py config     打印当前生效配置
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import sys
from pathlib import Path

# Windows 控制台默认可能是 cp950/cp936，中文会直接崩。强制 UTF-8 输出。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from radar import db, scheduler                       # noqa: E402
from radar.config import cfg                          # noqa: E402
from radar.llm import router                          # noqa: E402
from radar.notify import active_channels              # noqa: E402
from radar.power import power_state                   # noqa: E402

LOG_PATH = Path(__file__).resolve().parent / "data" / "radar.log"


def _setup_logging(verbose: bool = False) -> None:
    """把日志同时写到控制台和 data/radar.log。

    为什么必须有文件日志：计划任务用 ``pythonw.exe`` 静默运行（pythonw 根本没有
    stdout），没有这个的话定时采集出任何问题都查不到任何痕迹。
    """
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(logging.DEBUG if verbose else logging.INFO)
        root.addHandler(fh)
    except Exception:
        pass       # 日志文件建不出来也不能让程序起不来

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(sh)

    # httpx 会在 INFO 级别打印每一次 HTTP 请求，采集一轮就是几百行，
    # 把真正有用的信息（哪一步失败、降级到什么引擎）全冲没了。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _p(title: str) -> None:
    print(f"\n=== {title} ===")


def cmd_serve(args: argparse.Namespace) -> int:
    from radar.webapp import serve

    serve(open_browser=not args.no_browser)
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    from radar.pipeline import run_open_source

    # --auto 就是计划任务用的入口，记录成 task 才能在运行历史里一眼分清
    # "这是到点自动跑的" 还是 "我自己双击跑的"。
    kind = "task" if getattr(args, "auto", False) else "cli"
    result = run_open_source(respect_power=args.auto, trigger=kind)
    _p("结果")
    print(json.dumps({k: v for k, v in result.items() if k != "stats"},
                     ensure_ascii=False, indent=2))
    stats = result.get("stats") or {}
    _p("统计")
    for k in ("discovered", "unique_discovered", "refreshed", "readmes",
              "scored", "rejected", "blocked_unsafe", "enriched", "notified",
              "digest_items"):
        if k in stats:
            print(f"  {k:20s} {stats[k]}")
    if stats.get("enrich_models"):
        print(f"  {'AI 模型':20s} {', '.join(stats['enrich_models'])}")
    for note in stats.get("notes") or []:
        print(f"  ⚠ {note}")
    return 0 if result.get("ok") else 1


def cmd_diagnose(args: argparse.Namespace) -> int:
    db.init_db()
    _p("环境")
    print(f"  数据目录      {cfg.data_dir}")
    print(f"  数据库        {cfg.db_path}")
    p = power_state()
    print(f"  电源          " + (f"电池 {p['percent']}%，{'已插电' if p['plugged'] else '使用电池'}"
                                  if p["available"] else f"未检测到电池（{p['reason']}）"))

    _p("AI 引擎")
    probe = router.probe()
    print(f"  结论          {probe.get('summary')}")
    print(f"  本地 Ollama   {'✅' if probe['local'].get('ok') else '❌'} {probe['local'].get('reason')}")
    print(f"  DeepSeek API  {'✅' if probe['api'].get('ok') else '❌'} {probe['api'].get('reason')}")
    if probe["local"].get("available_models"):
        print(f"  本地已有模型  {', '.join(probe['local']['available_models'])}")

    _p("GitHub")
    try:
        from radar.sources.github_trending import GitHubClient
        cli = GitHubClient()
        rl = cli.rate_limit()
        print(f"  认证          {'✅ 已配置 token' if rl['authed'] else '❌ 未配置（限 60 次/小时）'}")
        print(f"  核心配额      {rl['core_left']}/{rl['core_limit']}（{rl['reset_in']}s 后重置）")
        print(f"  搜索配额      {rl['search_left']}/{rl['search_limit']}")
    except Exception as exc:
        print(f"  ❌ 不可用: {exc}")

    _p("中文稀缺度探测")
    try:
        from radar.probes import bing_count
        n = bing_count("github trending 教程")
        print(f"  Bing          {'✅ 返回 ' + str(n) + ' 条' if n is not None else '❌ 不可用（退化为仅看 README）'}")
    except Exception as exc:
        print(f"  Bing          ❌ {exc}")

    _p("通知渠道")
    chans = active_channels()
    print(f"  已启用        {'、'.join(chans) if chans else '无'}")
    print("  （用看板上的「发送测试通知」验证真实连通性）")

    _p("密钥")
    for name in ("LLM_API_KEY", "DEEPSEEK_API_KEY", "GITHUB_TOKEN",
                 "WECOM_WEBHOOK", "SMTP_PASSWORD", "TELEGRAM_BOT_TOKEN"):
        got = cfg.secret(name)
        mark = "已设置" if got else "未设置"
        if name == "DEEPSEEK_API_KEY" and got and cfg.secret("LLM_API_KEY"):
            mark = "已设置（被 LLM_API_KEY 覆盖）"
        print(f"  {name:22s} {mark}")
    _p("日志")
    print(f"  文件          {LOG_PATH}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    db.init_db()
    _p("仓库池")
    print(json.dumps(db.repo_stats(), ensure_ascii=False, indent=2))
    _p("调度")
    print(json.dumps(scheduler.status(), ensure_ascii=False, indent=2))
    _p("最近运行")
    for r in db.recent_runs(8):
        print(f"  #{r['id']:<4} {r['started_at']}  {r['status']:<9} "
              f"{json.dumps(r.get('stats') or {}, ensure_ascii=False)[:110]}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    print(json.dumps(cfg.dashboard_view(), ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="情报雷达", add_help=True)
    ap.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="启动网页面板")
    s.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("once", help="立刻跑一次采集（忽略电池守卫）")
    s.add_argument("--auto", action="store_true", help="遵守电池守卫")
    s.set_defaults(func=cmd_once)

    s = sub.add_parser("auto", help="跑一次采集，遵守电池守卫")
    s.set_defaults(func=lambda a: cmd_once(argparse.Namespace(auto=True)))

    sub.add_parser("diagnose", help="一键体检").set_defaults(func=cmd_diagnose)
    sub.add_parser("status", help="打印状态").set_defaults(func=cmd_status)
    sub.add_parser("config", help="打印生效配置").set_defaults(func=cmd_config)

    args = ap.parse_args()
    _setup_logging(verbose=bool(getattr(args, "verbose", False)))
    if not getattr(args, "func", None):
        args = argparse.Namespace(cmd="serve", no_browser=False, func=cmd_serve)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
