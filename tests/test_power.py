"""电源守卫测试。

这台机器是笔记本，经常拔电。所以「没插电就不采集」和
「电量过低就跳过」是真实需求，不是过度设计。
"""
from __future__ import annotations

import pytest

from radar import power


@pytest.fixture
def fake_power(monkeypatch):
    """把 power_state 换成可编程的假实现，避免测试依赖真机电池。"""
    def _apply(available=True, percent=80.0, plugged=True, reason=""):
        monkeypatch.setattr(power, "power_state", lambda: {
            "available": available, "percent": percent,
            "plugged": plugged, "reason": reason,
        })
    return _apply


class TestShouldDefer:
    def test_disabled_config_never_defers(self, fake_power):
        fake_power(plugged=False, percent=5.0)
        defer, _ = power.should_defer({"enabled": False})
        assert defer is False

    def test_empty_config_never_defers(self, fake_power):
        fake_power(plugged=False, percent=5.0)
        assert power.should_defer(None)[0] is False

    def test_unavailable_power_info_is_treated_as_unrestricted(self, fake_power):
        """台式机/虚拟机读不到电池 —— 必须当作一直插电，否则永远不采集。"""
        fake_power(available=False, reason="no battery")
        defer, why = power.should_defer({"require_ac": True})
        assert defer is False
        assert "不可用" in why

    def test_on_battery_defers(self, fake_power):
        fake_power(plugged=False, percent=90.0)
        defer, why = power.should_defer({"require_ac": True, "min_battery": 35})
        assert defer is True
        assert "未插电" in why

    def test_plugged_in_does_not_defer(self, fake_power):
        fake_power(plugged=True, percent=50.0)
        defer, why = power.should_defer({"require_ac": True, "min_battery": 35})
        assert defer is False
        assert "已插电" in why

    def test_low_battery_defers_even_when_plugged(self, fake_power):
        """刚插上但还没充进去 —— 仍然跳过，别把电放干。"""
        fake_power(plugged=True, percent=10.0)
        defer, why = power.should_defer({"require_ac": True, "min_battery": 35})
        assert defer is True
        assert "低于阈值" in why

    def test_require_ac_false_allows_battery_above_floor(self, fake_power):
        fake_power(plugged=False, percent=60.0)
        defer, why = power.should_defer({"require_ac": False, "min_battery": 35})
        assert defer is False
        assert "电池" in why

    def test_zero_floor_disables_battery_check(self, fake_power):
        fake_power(plugged=True, percent=1.0)
        defer, _ = power.should_defer({"require_ac": True, "min_battery": 0})
        assert defer is False


class TestPowerState:
    def test_returns_expected_shape(self):
        st = power.power_state()
        for key in ("available", "percent", "plugged", "reason"):
            assert key in st

    def test_never_raises_on_this_machine(self):
        """真机调用也不能抛异常 —— 电源信息读不到只是降级，不该让采集崩掉。"""
        st = power.power_state()
        assert isinstance(st["available"], bool)
        assert st["percent"] is None or isinstance(st["percent"], (int, float))
