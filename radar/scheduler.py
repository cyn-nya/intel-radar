"""调度：为笔记本设计的低功耗调度器。

和普通 cron 的区别：

1. **电源守卫** —— 没插电就跳过采集（看板照样能开），并留下 ``pending_catchup`` 标记；
2. **错过补跑** —— 启动时发现"今天该跑还没跑"，立刻补一次；
3. **不常驻也没关系** —— 即使服务从没开着，双击「跑一次」也能手动补齐。
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timedelta
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import db
from .config import cfg
from .power import power_state, should_defer

log = logging.getLogger("radar.scheduler")

_scheduler: BackgroundScheduler | None = None


def _parse_hhmm(s: str) -> dtime | None:
    try:
        h, m = str(s).split(":")
        return dtime(int(h), int(m))
    except Exception:
        return None


def scheduled_times() -> list[dtime]:
    out = []
    for s in (cfg.get("schedule.times", []) or []):
        t = _parse_hhmm(s)
        if t:
            out.append(t)
    return sorted(out)


# ----------------------------------------------------------------------
def _job_open_source(trigger: str = "schedule") -> None:
    from .pipeline import run_open_source

    log.info("触发开源雷达（%s）", trigger)
    try:
        result = run_open_source(respect_power=True, trigger=trigger)
        log.info("开源雷达结束: %s", {k: v for k, v in result.items() if k != "stats"})
    except Exception:
        log.exception("定时任务异常")


def missed_today() -> bool:
    """今天是否有已过点但没跑的场次。"""
    last_day = db.kv_get("last_open_source_day")
    if last_day == db.today():
        return False
    now = datetime.now().time()
    for t in scheduled_times():
        if now >= t:
            return True
    # 没有任何场次已过点：如果配置了场次且今天还没跑，也不算错过
    return False


def catch_up(reason: str = "startup") -> dict[str, Any]:
    """开机/启动时的补跑。"""
    if not cfg.get("schedule.catch_up_on_start", True):
        return {"ran": False, "reason": "补跑已关闭"}
    if not missed_today():
        return {"ran": False, "reason": "今天没有错过的场次"}

    defer, why = should_defer(cfg.get("schedule.power_guard", {}) or {})
    if defer:
        log.info("补跑被电源守卫拦下：%s", why)
        return {"ran": False, "reason": why, "deferred": True}

    log.info("补跑今天错过的采集（%s）", reason)
    _job_open_source(trigger=f"catchup:{reason}")
    return {"ran": True, "reason": reason}


def start() -> BackgroundScheduler | None:
    """启动后台调度器。"""
    global _scheduler
    if not cfg.get("schedule.enabled", True):
        log.info("调度已在配置中关闭")
        return None
    if _scheduler is not None:
        return _scheduler

    sched = BackgroundScheduler(timezone="Asia/Shanghai", daemon=True)
    times = scheduled_times()
    for i, t in enumerate(times):
        sched.add_job(
            _job_open_source,
            CronTrigger(hour=t.hour, minute=t.minute),
            id=f"open_source_{i}",
            name=f"开源雷达 {t.strftime('%H:%M')}",
            kwargs={"trigger": "schedule"},
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
    sched.start()
    _scheduler = sched
    log.info("调度已启动，每天 %s 各跑一次", ", ".join(t.strftime("%H:%M") for t in times))
    return sched


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None


def jobs() -> list[dict[str, Any]]:
    if _scheduler is None:
        return []
    out = []
    for j in _scheduler.get_jobs():
        nxt = getattr(j, "next_run_time", None)
        out.append({
            "id": j.id, "name": j.name,
            "next_run": nxt.isoformat(timespec="seconds") if nxt else None,
        })
    return out


def status() -> dict[str, Any]:
    st = power_state()
    defer, why = should_defer(cfg.get("schedule.power_guard", {}) or {})
    last = db.kv_get("last_open_source_run")
    return {
        "enabled": bool(cfg.get("schedule.enabled", True)),
        "times": [t.strftime("%H:%M") for t in scheduled_times()],
        "running": _scheduler is not None,
        "jobs": jobs(),
        "power": st,
        "power_note": why,
        "will_defer": defer,
        "last_run": last,
        "last_run_day": db.kv_get("last_open_source_day"),
        "missed_today": missed_today(),
        "pending_catchup": db.kv_get("pending_catchup") == "1",
        "today": db.today(),
    }
