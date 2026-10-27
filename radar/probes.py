"""外部稀缺度探测：中文资料到底有多少人写过。

用 Bing 的 RSS 输出（比解析 HTML 稳），失败时返回 ``None`` 表示"未知"，
而不是 0 —— 未知不该被当成"没人写过"而虚高打分。
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

import httpx

log = logging.getLogger("radar.probes")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def bing_count(query: str, timeout: int = 15) -> int | None:
    """数一数 Bing 上这个查询有多少条结果。失败返回 None（表示未知）。"""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True,
                          headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}) as cli:
            r = cli.get("https://www.bing.com/search", params={"q": query, "format": "rss"})
        if r.status_code != 200 or not r.text.strip():
            return None
        root = ET.fromstring(r.text)
        items = root.findall(".//item")
        if items:
            return len(items)
        # 没有 item 但有 channel 说明确实是"零结果"
        if root.find(".//channel") is not None:
            return 0
        return None
    except Exception as exc:
        log.debug("bing 探测失败 %r: %s", query, exc)
        return None


def cn_scarcity_probe(repo_full_name: str, extra_terms: list[str] | None = None,
                      per_item: int = 1) -> int | None:
    """探测某个项目的中文资料数量。

    查询形如 ``"owner/repo" 教程`` —— 命中越少说明中文生态越空白。
    """
    if per_item <= 0:
        return None
    name = repo_full_name.split("/")[-1]
    queries = [f'"{repo_full_name}" 教程 OR 部署 OR 中文']
    if per_item > 1:
        queries.append(f'"{name}" 安装 教程')
    for t in extra_terms or []:
        queries.append(f'"{name}" {t}')

    hits: list[int] = []
    for q in queries[:max(1, per_item)]:
        n = bing_count(q)
        if n is not None:
            hits.append(n)
    if not hits:
        return None
    return sum(hits)


# ----------------------------------------------------------------------
def cn_ecosystem_repos(client, repo_full_name: str) -> int | None:
    """用 GitHub 搜索数一数有几个"中文相关"的仓库提到它。较贵（占 search 配额）。"""
    name = repo_full_name.split("/")[-1]
    if not name or len(name) < 3:
        return None
    try:
        items = client.search_repos(f"{name} in:name,description,readme", per_page=20)
    except Exception as exc:
        log.debug("cn 生态探测失败 %s: %s", repo_full_name, exc)
        return None
    if not items:
        return 0
    cn = 0
    for it in items:
        desc = it.get("description") or ""
        if re.search(r"[\u4e00-\u9fff]", desc):
            cn += 1
    return cn
