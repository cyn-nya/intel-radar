"""配置加载：config.yaml + .env + 环境变量。

用法::

    from radar.config import cfg
    cfg.get("llm.local.model", "qwen3:8b")
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent      # .../radar/
CONFIG_PATH = ROOT / "config.yaml"
ENV_PATH = ROOT / ".env"
OVERRIDES_PATH = ROOT / "data" / "overrides.yaml"  # 看板里改的设置存这里，不污染 config.yaml

DEFAULTS: dict[str, Any] = {
    "app": {"host": "127.0.0.1", "port": 8787, "data_dir": "data", "open_browser": True},
    "llm": {
        "mode": "hybrid",
        "prefer": "api",
        "local": {"enabled": True, "base_url": "http://127.0.0.1:11434", "model": "qwen3:8b",
                  "disable_thinking": True, "timeout": 420, "max_readme_chars": 2500},
        "api": {
            "enabled": True, "base_url": "https://llmapi.paratera.com",
            "model": "DeepSeek-V4.1-Flash", "key_env": "",
            "timeout": 120, "retries": 2,
            "max_readme_chars": 6000, "price_in": 1.0, "price_out": 3.0,
        },
        "cost_guard": {"daily_limit_cny": 3.0, "max_bulk_items": 25},
    },
    "open_source": {
        "enabled": True,
        "discover": {
            "new_repos": {"days": 45, "min_stars": 150, "per_query": 30},
            "refresh_tracked": True,
            "refresh_limit": 120,
        },
        "filters": {
            "min_stars": 150, "max_stars": 8000, "max_age_days": 400,
            "exclude_archived": True, "exclude_forks": True,
            "exclude_keywords": [], "languages": [], "require_any_keyword": [],
            "block_non_software": True,
        },
        "scoring": {
            "weight_momentum": 0.42, "weight_cn_gap": 0.33, "weight_deployability": 0.25,
            "momentum_windows": [1, 3, 7], "min_span_hours": 6,
            "penalty_gpu": 0.25, "penalty_enterprise": 0.20,
            "penalty_api_key": 0.15, "penalty_hype": 0.30,
        },
        "cn_gap": {
            "check_readme_cjk": True, "check_readme_variants": True,
            "search_cn_ecosystem": False, "bing_probe": True, "bing_per_item": 1,
        },
        "enrich": {
            "top_n": 12, "reenrich_days": 7,
            "fields": ["summary_zh", "audience", "deploy_note", "monetize", "risk"],
        },
        "deliver": {
            "notify_min_score": 0.82, "notify_top_n": 5,
            "daily_digest": True, "digest_hour": 9, "desktop_notify": True,
        },
    },
    "notify": {
        "desktop": {"enabled": True},
        "wecom": {"enabled": False, "webhook": ""},
        "email": {
            "enabled": False, "smtp_host": "smtp.qq.com", "smtp_port": 465,
            "use_ssl": True, "username": "", "password": "", "to": [],
        },
        "telegram": {"enabled": False, "bot_token": "", "chat_id": "", "proxy": ""},
    },
    "schedule": {
        "enabled": True, "times": ["09:10", "21:10"],
        "power_guard": {"enabled": True, "require_ac": True, "min_battery": 35},
        "catch_up_on_start": True,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_env_file(path: Path = ENV_PATH) -> None:
    """把 .env 里的键值读进 os.environ（不覆盖已有的真实环境变量）。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and val and key not in os.environ:
            os.environ[key] = val


class Config:
    def __init__(self) -> None:
        self.reload()

    def reload(self) -> None:
        load_env_file()
        layers: list[dict[str, Any]] = []
        for path in (CONFIG_PATH, OVERRIDES_PATH):
            if not path.exists():
                layers.append({})
                continue
            try:
                layers.append(yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {})
            except Exception as exc:                      # 配置写坏了不要让平台起不来
                print(f"[config] {path.name} 解析失败，忽略这一层: {exc}")
                layers.append({})
        self._data = _deep_merge(_deep_merge(DEFAULTS, layers[0]), layers[1])

    # ------------------------------------------------------------------
    def set_overrides(self, updates: dict[str, Any]) -> dict[str, Any]:
        """把看板改的设置写进 data/overrides.yaml（dotted key -> value）。"""
        current: dict[str, Any] = {}
        if OVERRIDES_PATH.exists():
            try:
                current = yaml.safe_load(OVERRIDES_PATH.read_text(encoding="utf-8-sig")) or {}
            except Exception:
                current = {}
        for dotted, value in updates.items():
            parts = dotted.split(".")
            node = current
            for p in parts[:-1]:
                if not isinstance(node.get(p), dict):
                    node[p] = {}
                node = node[p]
            node[parts[-1]] = value
        OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
        OVERRIDES_PATH.write_text(
            yaml.safe_dump(current, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        self.reload()
        return current

    def clear_overrides(self) -> None:
        if OVERRIDES_PATH.exists():
            OVERRIDES_PATH.unlink()
        self.reload()

    # ------------------------------------------------------------------
    def dashboard_view(self) -> dict[str, Any]:
        """给看板看的配置视图（含哪些字段可改）。"""
        return {
            "app": {"host": self.get("app.host"), "port": self.get("app.port")},
            "llm": self.get("llm"),
            "open_source": {
                "discover": self.get("open_source.discover"),
                "filters": self.get("open_source.filters"),
                "scoring": {k: v for k, v in (self.get("open_source.scoring") or {}).items()
                            if not k.startswith("_")},
                "cn_gap": self.get("open_source.cn_gap"),
                "enrich": self.get("open_source.enrich"),
                "deliver": self.get("open_source.deliver"),
            },
            "notify": {
                "desktop": self.get("notify.desktop"),
                "wecom": {**{k: v for k, v in (self.get("notify.wecom") or {}).items() if k != "webhook"},
                          "webhook": "***" if (self.get("notify.wecom.webhook") or "") else ""},
                "email": {**{k: v for k, v in (self.get("notify.email") or {}).items()
                             if k not in ("password",)},
                          "password": "***" if (self.get("notify.email.password") or "") else ""},
                "telegram": {**{k: v for k, v in (self.get("notify.telegram") or {}).items()
                                if k not in ("bot_token",)},
                             "bot_token": "***" if (self.get("notify.telegram.bot_token") or "") else ""},
            },
            "schedule": self.get("schedule"),
            "secrets_present": {
                "DEEPSEEK_API_KEY": bool(self.secret("DEEPSEEK_API_KEY")),
                "GITHUB_TOKEN": bool(self.secret("GITHUB_TOKEN")),
                "WECOM_WEBHOOK": bool(self.secret("WECOM_WEBHOOK")),
                "SMTP_PASSWORD": bool(self.secret("SMTP_PASSWORD")),
                "TELEGRAM_BOT_TOKEN": bool(self.secret("TELEGRAM_BOT_TOKEN")),
            },
        }

    # ---------------------------------------------------------------
    def raw(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # --- 便捷属性 ---------------------------------------------------
    @property
    def data_dir(self) -> Path:
        d = Path(str(self.get("app.data_dir", "data")))
        if not d.is_absolute():
            d = ROOT / d
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def db_path(self) -> Path:
        return self.data_dir / "radar.db"

    # --- 密钥 -------------------------------------------------------
    @staticmethod
    def secret(name: str) -> str:
        return (os.environ.get(name) or "").strip()


cfg = Config()
