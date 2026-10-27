"""配置层测试。

重点：**密钥只从环境变量读**，绝不落进 config.yaml 或 overrides.yaml。
这是这个项目的安全底线 —— 配置文件是要提交进仓库的。
"""
from __future__ import annotations

from radar.config import Config, cfg


class TestDottedGet:
    def test_reads_nested_value(self):
        assert isinstance(cfg.get("llm.mode"), str)

    def test_missing_path_returns_default(self):
        assert cfg.get("this.path.does.not.exist", "兜底") == "兜底"
        assert cfg.get("this.path.does.not.exist") is None

    def test_default_config_is_usable_without_yaml(self):
        """config.yaml 缺失时也要有一份能跑的默认值，否则第一次克隆直接崩。"""
        c = Config()
        assert c.get("llm.mode")
        assert c.get("schedule.times")


class TestSecretIsolation:
    def test_unset_secret_returns_empty_string_not_none(self, monkeypatch):
        monkeypatch.delenv("RADAR_TEST_DEFINITELY_UNSET", raising=False)
        assert Config.secret("RADAR_TEST_DEFINITELY_UNSET") == ""

    def test_reads_from_environment(self, monkeypatch):
        monkeypatch.setenv("RADAR_TEST_SECRET", "s3cr3t-value")
        assert Config.secret("RADAR_TEST_SECRET") == "s3cr3t-value"

    def test_secret_is_not_in_config_get(self):
        """密钥不该能从普通配置读取路径拿到 —— 那种写法迟早会被写进 yaml。"""
        assert cfg.get("llm.api.key") in (None, "")
        assert "api_key" not in (cfg.get("llm.api") or {})


class TestDataPaths:
    def test_data_dir_is_inside_project(self):
        d = cfg.data_dir
        assert d.name == "data"
        assert d.parent.name == "radar"

    def test_db_path_is_in_data_dir(self):
        assert cfg.db_path.parent == cfg.data_dir
        assert cfg.db_path.name.endswith(".db")
