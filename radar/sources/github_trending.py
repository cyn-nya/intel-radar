"""GitHub 数据源。

为什么不用爬 github.com/trending？
--------------------------------
本机实测 ``github.com`` 网页不可达，但 **``api.github.com`` 完全可用**。
所以我们不去爬 Trending 页面，而是：

1. 用官方 Search API 拉候选池（新仓库、按星排序、按 topic）；
2. 每次运行把每个仓库的 star 数写进 ``snapshots`` 表；
3. 下次运行时比对历史快照，**自己算出真实星标增速**。

这比 Trending 页面更好：可以自定义窗口/语言/topic，增速是真实增量而不是"相对位置"，
而且不违反任何规则、不会被封。
"""
from __future__ import annotations

import base64
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Iterable

import httpx

from ..config import cfg

log = logging.getLogger("radar.source.github")

API = "https://api.github.com"

# 默认扫描的 topic（可被 config 里的 open_source.discover.topics 覆盖）
# 刻意只留高信号的一小把：未认证时 search 只有 10 次/分钟，topic 越多跑得越慢。
DEFAULT_TOPICS = [
    "llm", "ai-agent", "mcp", "rag", "automation",
    "self-hosted", "developer-tools", "cli", "productivity", "no-code",
]


class RateLimited(Exception):
    pass


class GitHubClient:
    def __init__(self) -> None:
        self.token = cfg.secret("GITHUB_TOKEN")
        self._core_left: int | None = None
        self._search_left: int | None = None
        self.calls = 0

    # ------------------------------------------------------------------
    @property
    def authed(self) -> bool:
        return bool(self.token)

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28",
             "User-Agent": "intel-radar/1.0"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _request(self, method: str, url: str, *, accept: str | None = None,
                 params: dict[str, Any] | None = None, retries: int = 2) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                with httpx.Client(timeout=25, follow_redirects=True) as cli:
                    r = cli.request(method, url, headers=self._headers(accept or "application/vnd.github+json"),
                                    params=params)
                self.calls += 1
                if r.status_code in (403, 429) and "rate limit" in r.text.lower():
                    reset = r.headers.get("x-ratelimit-reset")
                    wait = max(0, int(reset) - int(time.time())) if reset else 60
                    raise RateLimited(f"触及速率限制，需等 {wait}s（{r.text[:120]}）")
                if r.status_code >= 500 and attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if "x-ratelimit-remaining" in r.headers:
                    # 必须区分是哪个"资源桶"的配额。
                    # Search API 的响应头返回的是**搜索**剩余次数（30/分），
                    # 早期版本不分青红皂白写进 _core_left，于是搜索完最后一轮
                    # 把核心配额覆盖成了 29 —— 结果 5000 次的 token 被当成 29 次用：
                    # 回访刷新被跳过、README 只抓了 (29-8)//2 = 10 份。
                    resource = (r.headers.get("x-ratelimit-resource") or "").lower()
                    if not resource:
                        resource = "search" if "/search/" in url else "core"
                    try:
                        val = int(r.headers["x-ratelimit-remaining"])
                    except ValueError:
                        val = None
                    if val is not None:
                        if resource == "search":
                            self._search_left = val
                        elif resource == "core":
                            self._core_left = val
                        # graphql / integration_manifest 等其它桶直接忽略
                return r
            except RateLimited:
                raise
            except Exception as exc:
                last = exc
                if attempt < retries:
                    time.sleep(1.2 * (attempt + 1))
        raise RuntimeError(f"GitHub 请求失败: {url} -> {last}")

    # ------------------------------------------------------------------
    def rate_limit(self) -> dict[str, Any]:
        r = self._request("GET", f"{API}/rate_limit")
        if r.status_code != 200:
            return {}
        res = r.json().get("resources", {})
        core, search = res.get("core", {}), res.get("search", {})
        self._core_left = core.get("remaining")
        self._search_left = search.get("remaining")
        return {
            "authed": self.authed,
            "core_limit": core.get("limit"), "core_left": core.get("remaining"),
            "search_limit": search.get("limit"), "search_left": search.get("remaining"),
            "reset_in": max(0, int(core.get("reset", 0)) - int(time.time())),
        }

    def core_budget(self) -> int:
        """还剩多少次核心 API 调用（未知时保守返回 0）。"""
        if self._core_left is None:
            try:
                self.rate_limit()
            except Exception:
                return 0
        return int(self._core_left or 0)

    def search_left(self) -> int:
        if self._search_left is None:
            try:
                self.rate_limit()
            except Exception:
                return 0
        return int(self._search_left or 0)

    # ------------------------------------------------------------------
    def search_repos(self, query: str, per_page: int = 30,
                     sort: str = "stars") -> list[dict[str, Any]]:
        r = self._request("GET", f"{API}/search/repositories",
                          params={"q": query, "sort": sort, "order": "desc",
                                  "per_page": min(per_page, 100)})
        if r.status_code == 403:
            raise RateLimited("search API 被限流")
        if r.status_code != 200:
            log.warning("search 失败 %s: %s", query, r.text[:200])
            return []
        if "x-ratelimit-remaining" in r.headers:
            try:
                self._search_left = int(r.headers["x-ratelimit-remaining"])
            except ValueError:
                pass
        return r.json().get("items") or []

    def get_repo(self, full_name: str) -> dict[str, Any] | None:
        r = self._request("GET", f"{API}/repos/{full_name}")
        if r.status_code != 200:
            return None
        return r.json()

    def list_root(self, full_name: str) -> list[dict[str, Any]]:
        r = self._request("GET", f"{API}/repos/{full_name}/contents/")
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []

    def get_readme(self, full_name: str) -> str:
        r = self._request("GET", f"{API}/repos/{full_name}/readme",
                          accept="application/vnd.github.raw")
        if r.status_code != 200:
            return ""
        text = r.text
        # 少数情况下返回 JSON（base64 编码）
        if text.lstrip().startswith("{") and '"content"' in text[:400]:
            try:
                import json as _json
                payload = _json.loads(text)
                return base64.b64decode(payload.get("content", "")).decode("utf-8", "ignore")
            except Exception:
                pass
        return text

    def get_repo_bundle(self, full_name: str) -> dict[str, Any]:
        """一次拿 README + 目录清单（用于判断中文 README 变体）。"""
        readme = self.get_readme(full_name)
        variants: list[str] = []
        if cfg.get("open_source.cn_gap.check_readme_variants", True):
            try:
                for entry in self.list_root(full_name):
                    name = str(entry.get("name", ""))
                    low = name.lower()
                    if low.startswith("readme") and any(
                        k in low for k in ("zh", "cn", "chinese", "tw", "中文")
                    ):
                        variants.append(name)
                    if name in ("docs", "doc", "i18n") and entry.get("type") == "dir":
                        variants.append(f"{name}/ (目录)")
            except Exception as exc:
                log.debug("列目录失败 %s: %s", full_name, exc)
        return {"readme": readme, "variants": variants}


# ----------------------------------------------------------------------
#  归一化：把 API 原始对象变成我们要的扁平结构
# ----------------------------------------------------------------------
def normalize(item: dict[str, Any]) -> dict[str, Any]:
    lic = item.get("license") or {}
    return {
        "repo_id": item.get("id"),
        "full_name": item.get("full_name"),
        "owner": (item.get("owner") or {}).get("login"),
        "name": item.get("name"),
        "description": item.get("description"),
        "html_url": item.get("html_url"),
        "homepage": item.get("homepage"),
        "language": item.get("language"),
        "topics": item.get("topics") or [],
        "license": lic.get("spdx_id") if isinstance(lic, dict) else None,
        "stars": int(item.get("stargazers_count") or 0),
        "forks": int(item.get("forks_count") or 0),
        "open_issues": int(item.get("open_issues_count") or 0),
        "watchers": int(item.get("watchers_count") or 0),
        "size_kb": int(item.get("size") or 0),
        "created_at": item.get("created_at"),
        "pushed_at": item.get("pushed_at"),
        "archived": bool(item.get("archived")),
        "is_fork": bool(item.get("fork")),
    }


# ----------------------------------------------------------------------
#  候选池发现
# ----------------------------------------------------------------------
def build_queries(cfg_node: dict[str, Any]) -> list[dict[str, Any]]:
    """构造搜索查询。每条查询算一次 search API 调用。"""
    new = cfg_node.get("new_repos", {}) or {}
    days = int(new.get("days", 45))
    min_stars = int(new.get("min_stars", 150))
    per = int(new.get("per_query", 30))

    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    week = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    month = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    queries: list[dict[str, Any]] = [
        {"label": f"近{days}天新仓库·按星", "q": f"created:>{since} stars:>{min_stars}", "per_page": per},
        {"label": "本周新爆款", "q": f"created:>{week} stars:>{max(30, min_stars // 4)}", "per_page": per},
        {"label": "近30天活跃·中等星标",
         "q": f"created:>{month} stars:{min_stars}..{max(min_stars * 20, 4000)} pushed:>{week}",
         "per_page": per},
    ]

    topics = cfg_node.get("topics") or DEFAULT_TOPICS
    for t in topics:
        queries.append({
            "label": f"topic:{t}",
            "q": f"topic:{t} created:>{since} stars:>{max(20, min_stars // 3)}",
            "per_page": min(per, 20),
        })
    return queries
