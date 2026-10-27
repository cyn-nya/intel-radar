"""电源感知：让采集任务学会"看电量做事"。

笔记本场景下的策略：
* 没插电          -> 跳过采集（看板照样能开）
* 电量低于阈值    -> 跳过
* 读不到电池(台式机/虚拟机) -> 视为一直插电，正常跑
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("radar.power")

try:
    import psutil
except Exception:                                     # pragma: no cover
    psutil = None                                      # type: ignore[assignment]


def power_state() -> dict[str, Any]:
    """返回当前电源状态。读不到电池时 available=False，调用方按"不受限"处理。"""
    if psutil is None:
        return {"available": False, "percent": None, "plugged": None, "reason": "psutil 不可用"}
    try:
        bat = psutil.sensors_battery()
    except Exception as exc:
        return {"available": False, "percent": None, "plugged": None, "reason": str(exc)}
    if bat is None:
        return {"available": False, "percent": None, "plugged": None, "reason": "未检测到电池"}
    return {
        "available": True,
        "percent": round(float(bat.percent), 1),
        "plugged": bool(bat.power_plugged),
        "reason": "",
    }


def should_defer(power_cfg: dict[str, Any] | None) -> tuple[bool, str]:
    """要不要因为电源原因推迟这次采集？

    返回 ``(defer, reason)``。``defer=True`` 时调用方应跳过采集并记下待补跑标记。
    """
    if not power_cfg or not power_cfg.get("enabled", True):
        return False, ""

    st = power_state()
    if not st["available"]:
        return False, f"电源信息不可用({st['reason']})，按不受限处理"

    if power_cfg.get("require_ac", True) and not st["plugged"]:
        return True, f"当前未插电（{st['percent']}%），跳过采集以省电"

    floor = float(power_cfg.get("min_battery", 0) or 0)
    if floor and st["percent"] is not None and st["percent"] < floor:
        return True, f"电量 {st['percent']}% 低于阈值 {floor}%，跳过采集"

    return False, f"电源正常（{st['percent']}%，{'已插电' if st['plugged'] else '电池'}）"
