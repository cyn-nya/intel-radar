"""打分引擎测试。

这里覆盖的都是**真实踩过坑**的逻辑，不是凑覆盖率：
- 短跨度外推会算出「1691 星/天」这种荒谬数字（仓库总共才 2203 星）
- README 抓不到时「可部署性」如果算成 0，会让一批仓库分数一起塌底
- 绝对增速阈值在行情好时会让一大片仓库顶格，排名失去区分度
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from radar import scoring


def _snap(hours_ago: float, stars: int) -> dict:
    """造一条快照记录（history 约定：最新在前，但函数内部会自己排序）。"""
    return {
        "stars": stars,
        "captured_at": (datetime.now() - timedelta(hours=hours_ago)).isoformat(),
    }


# ----------------------------------------------------------------------
# 增速：为什么必须有 min_span_hours
# ----------------------------------------------------------------------
class TestSnapshotsVelocity:
    def test_no_history_returns_zeros(self):
        v = scoring.snapshots_velocity([], 100, [1, 7])
        assert v["rate_per_day"] == 0.0
        assert v["window"] is None
        assert v["span_hours"] is None

    def test_span_too_short_refuses_to_extrapolate(self):
        """核心回归：30 分钟的差分绝不能被放大成日增速。"""
        v = scoring.snapshots_velocity([_snap(0.5, 2200)], 2203, [1, 7])
        assert v["reason"] == "span_too_short"
        assert v["rate_per_day"] == 0.0, "跨度不足时必须拒绝给出增速，而不是硬算"
        assert v["estimated"] is True

    def test_span_just_over_threshold_is_marked_extrapolated(self):
        """超过 min_span_hours 但还填不满一个窗口 → 允许外推，但必须标注是估算。"""
        v = scoring.snapshots_velocity([_snap(12, 1000)], 1100, [1, 7], min_span_hours=6.0)
        assert v["reason"] == "span_extrapolated"
        assert v["estimated"] is True
        assert v["window"] is None
        assert v["rate_per_day"] == pytest.approx(200.0, rel=0.05)   # 100 星 / 0.5 天

    def test_min_span_threshold_is_configurable(self):
        """同一份数据，阈值放低就能算 —— 证明这条闸门真的受配置控制。"""
        hist = [_snap(3, 1000)]
        assert scoring.snapshots_velocity(hist, 1300, [1, 7], min_span_hours=6.0)["reason"] \
            == "span_too_short"
        assert scoring.snapshots_velocity(hist, 1300, [1, 7], min_span_hours=1.0)["reason"] \
            == "span_extrapolated"

    def test_full_window_uses_measured_rate(self):
        """历史足够长时要走真实窗口，并明确标注 estimated=False。"""
        hist = [_snap(24 * 8, 1000), _snap(24 * 2, 1800)]
        v = scoring.snapshots_velocity(hist, 2000, [1, 7])
        assert v["window"] == 7
        assert v["delta"] == pytest.approx(1000.0)
        assert v["rate_per_day"] == pytest.approx(1000 / 7, rel=0.02)
        assert v["estimated"] is False, "实测值必须和估算值区分开，前端要据此标注"

    def test_malformed_history_entries_are_skipped(self):
        hist = [{"stars": "不是数字", "captured_at": "不是时间"}, _snap(30, 500)]
        v = scoring.snapshots_velocity(hist, 600, [1, 7])
        assert v["span_hours"] is not None, "坏记录必须被跳过，而不是让整个函数炸掉"


# ----------------------------------------------------------------------
# 相对排名：绝对阈值会让一片仓库顶格
# ----------------------------------------------------------------------
class TestMomentumPercentiles:
    def test_empty(self):
        assert scoring.momentum_percentiles({}) == {}

    def test_single_item_gets_midpoint(self):
        assert scoring.momentum_percentiles({42: 7.0}) == {42: 0.5}

    def test_strictly_increasing_spread(self):
        out = scoring.momentum_percentiles({1: 10.0, 2: 20.0, 3: 30.0})
        assert out[1] == 0.0 and out[2] == 0.5 and out[3] == 1.0

    def test_output_always_uses_full_range(self):
        """哪怕所有值都很接近，也要拉开 0..1 —— 这正是绝对阈值做不到的。"""
        out = scoring.momentum_percentiles({1: 100.0, 2: 100.1, 3: 100.2})
        assert out[1] == 0.0 and out[3] == 1.0


# ----------------------------------------------------------------------
# 可部署性：缺失 ≠ 0
# ----------------------------------------------------------------------
class TestDeployability:
    def test_missing_readme_is_none_not_zero(self):
        """关键：抓不到 README 是「不知道」，不是「很难部署」。"""
        assert scoring.compute_deployability({}) is None
        assert scoring.compute_deployability({"len": 0}) is None

    def test_docker_and_oneclick_raise_score(self):
        s = scoring.compute_deployability(
            {"len": 5000, "good": {"docker": True, "oneclick": True}})
        assert s == pytest.approx(0.40, abs=0.01)

    def test_library_only_and_short_readme_penalised(self):
        s = scoring.compute_deployability(
            {"len": 300, "good": {"docker": True, "oneclick": True},
             "bad": {"library_only": True}})
        assert s == 0.0, "扣分要夹到 0，不能出现负分"

    def test_score_is_clamped_to_unit_interval(self):
        s = scoring.compute_deployability({
            "len": 9000,
            "good": {k: True for k in ("docker", "oneclick", "release", "onecmd", "webui", "quickstart")},
        })
        assert s == 1.0


# ----------------------------------------------------------------------
# 受众规模
# ----------------------------------------------------------------------
class TestDemandFactor:
    def test_zero_stars(self):
        assert scoring.demand_factor({"stars": 0}) == 0.0

    def test_saturates_at_5000_stars(self):
        assert scoring.demand_factor({"stars": 5000}) == pytest.approx(1.0, abs=0.01)
        assert scoring.demand_factor({"stars": 50000}) == 1.0

    def test_monotonic(self):
        vals = [scoring.demand_factor({"stars": s}) for s in (10, 100, 1000, 4000)]
        assert vals == sorted(vals)


# ----------------------------------------------------------------------
# 中文探测
# ----------------------------------------------------------------------
class TestCjk:
    @pytest.mark.parametrize("text,expected", [
        ("hello world", False), ("", False), (None, False),
        ("中文说明", True), ("mixed 中文 text", True),
    ])
    def test_has_cjk(self, text, expected):
        assert scoring.has_cjk(text) is expected

    def test_cjk_ratio_bounds(self):
        assert scoring.cjk_ratio("中文中文中文") == pytest.approx(1.0)
        assert scoring.cjk_ratio("abcdef") == 0.0
        assert 0.0 < scoring.cjk_ratio("中文abcd") < 1.0


# ----------------------------------------------------------------------
# 硬过滤
# ----------------------------------------------------------------------
class TestHardFilter:
    NODE = {"min_stars": 50, "max_stars": 40000}

    def test_below_floor(self):
        assert "低于下限" in scoring.hard_filter({"stars": 10}, self.NODE)

    def test_above_ceiling(self):
        """星标太高说明中文教程早泛滥了，信息差没了。"""
        assert "超过上限" in scoring.hard_filter({"stars": 99999}, self.NODE)

    def test_archived(self):
        r = {"stars": 500, "archived": True}
        assert scoring.hard_filter(r, self.NODE) == "仓库已归档"

    def test_fork(self):
        r = {"stars": 500, "is_fork": True}
        assert scoring.hard_filter(r, self.NODE) == "是 fork"

    def test_too_old(self):
        node = dict(self.NODE, max_age_days=365)
        r = {"stars": 500,
             "created_at": (datetime.now() - timedelta(days=800)).isoformat()}
        assert "超出窗口" in scoring.hard_filter(r, node)

    def test_young_healthy_repo_passes(self):
        r = {"stars": 800, "archived": False, "is_fork": False,
             "description": "A self-hosted note taking application",
             "created_at": (datetime.now() - timedelta(days=30)).isoformat()}
        assert scoring.hard_filter(r, self.NODE) is None

    def test_missing_description_rejected(self):
        """没简介 = 判断不了这工具干嘛的 = 没法做成选题。"""
        r = {"stars": 800, "description": ""}
        assert "没有简介" in scoring.hard_filter(r, self.NODE)
        r2 = {"stars": 800, "description": "太短"}
        assert "没有简介" in scoring.hard_filter(r2, self.NODE)

    def test_exclude_keywords(self):
        node = dict(self.NODE, exclude_keywords=["blockchain", "web3"])
        r = {"stars": 800, "description": "A blockchain based social network"}
        assert "命中排除词" in scoring.hard_filter(r, node)

    def test_language_whitelist(self):
        node = dict(self.NODE, languages=["Python", "TypeScript"])
        r = {"stars": 800, "description": "A self-hosted note app", "language": "COBOL"}
        assert "不在白名单" in scoring.hard_filter(r, node)

    def test_require_any_keyword(self):
        node = dict(self.NODE, require_any_keyword=["llm", "agent"])
        r = {"stars": 800, "description": "A self-hosted note taking application"}
        assert "必需关键词" in scoring.hard_filter(r, node)
