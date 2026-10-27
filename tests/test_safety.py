"""内容安全层测试。

这一层存在的原因很具体：榜单前排一度全是 DRM 破解、CVE 漏洞利用和越狱工具 ——
它们同时满足「涨星快 + 中文资料少」，打分算法会主动把它们顶上去。
所以必须在打分**之前**硬拦截。
"""
from __future__ import annotations

import pytest

from radar import safety


class TestScan:
    @pytest.mark.parametrize("text", [
        "一个整理本地文件的小工具",
        "A fast JSON parser written in Rust",
        "自托管笔记应用，支持 Markdown",
        "",
        None,
    ])
    def test_benign_text_produces_no_hits(self, text):
        assert safety.scan(text) == []

    @pytest.mark.parametrize("text,category", [
        ("shadowsocks 客户端配置", "evasion"),
        ("免费节点订阅分享", "evasion"),
        ("porn aggregator", "adult"),
        ("casino 博彩平台源码", "gambling"),
        ("论文降重工具", "academic"),
        ("phone tracker 手机定位追踪", "privacy"),
    ])
    def test_known_risk_categories_are_caught(self, text, category):
        hits = safety.scan(text)
        assert category in {h["category"] for h in hits}, f"{text!r} 应当命中 {category}"

    def test_non_software_is_rejected(self):
        """"涨星再快也做不成一键部署教程" —— 清单类必须先被拦掉。"""
        hits = safety.scan("a curated list of awesome tools")
        assert "not_software" in {h["category"] for h in hits}

    def test_hits_carry_severity_and_matched_text(self):
        hits = safety.scan("shadowsocks 客户端")
        assert hits, "应当有命中"
        h = hits[0]
        assert h["severity"] in ("high", "medium")
        assert h["label"] and h["matched"]

    def test_multiple_texts_are_combined(self):
        """仓库名和描述分开传，也要能拼起来判断。"""
        hits = safety.scan("my-casino-app", "一个博彩平台")
        cats = {h["category"] for h in hits}
        assert "gambling" in cats


class TestCheckRepo:
    def test_clean_repo_passes(self):
        repo = {"full_name": "acme/json-tool", "name": "json-tool",
                "description": "A fast JSON parser", "topics": ["json", "parser"]}
        assert safety.check_repo(repo) == []

    def test_topics_list_is_flattened_and_scanned(self):
        repo = {"full_name": "x/y", "description": "无害简介",
                "topics": ["shadowsocks", "proxy"]}
        hits = safety.check_repo(repo)
        assert "evasion" in {h["category"] for h in hits}

    def test_readme_is_scanned(self):
        repo = {"full_name": "x/y", "description": "innocent"}
        assert safety.check_repo(repo, readme="使用前请准备 梯子 或 vpn") != []

    def test_readme_is_truncated_not_exploded(self):
        """超长 README 只取前一段，避免正则在大文本上耗时失控。"""
        repo = {"full_name": "x/y"}
        assert safety.check_repo(repo, readme="正常内容" * 50000) == []


class TestSeverity:
    def test_no_hits(self):
        assert safety.worst_severity([]) is None

    def test_medium_only(self):
        assert safety.worst_severity([{"severity": "medium"}]) == "medium"

    def test_high_wins_over_medium(self):
        hits = [{"severity": "medium"}, {"severity": "high"}]
        assert safety.worst_severity(hits) == "high"

    def test_reason_text_mentions_label_and_match(self):
        hits = safety.scan("shadowsocks")
        txt = safety.reason_text(hits)
        assert txt.startswith("内容安全拦截")
        assert "命中" in txt
