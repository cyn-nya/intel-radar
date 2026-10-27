"""AI 卡片后处理测试。

这里测的全是**小模型真实退化行为**的修补逻辑：
- qwen3:0.6b 会卡进复读循环，输出「开发者、开发者、开发者…」直到 token 耗尽
- 小模型会把提示词里的 JSON 骨架当示例值抄出来
- 偶尔把字段返回成嵌套对象/数组，一条就能让整张卡崩掉
"""
from __future__ import annotations

from radar import enrich


class TestDedupeRepeats:
    def test_consecutive_duplicates_collapsed(self):
        assert enrich._dedupe_repeats("A、A、A、B") == "A、B"

    def test_distinct_items_untouched(self):
        s = "开发者、开发者社区、开发者社区成员"
        assert enrich._dedupe_repeats(s) == s

    def test_whole_string_repetition_collapses_to_one(self):
        """整段就是同一个短语反复 —— 只留一份，别把 token 预算全烧在复读上。"""
        out = enrich._dedupe_repeats("开发者、" * 4)
        assert len(out) < 10

    def test_comma_variants_all_handled(self):
        for sep in "、，,；;":
            assert enrich._dedupe_repeats(f"A{sep}A{sep}B").count("A") == 1

    def test_empty_input(self):
        assert enrich._dedupe_repeats("") == ""


class TestClean:
    def test_placeholder_skeleton_is_stripped(self):
        """模型把骨架里的 <...> 占位符照抄出来了，必须清掉。"""
        assert enrich._clean("<这里写一句话>") == ""
        assert "<填入>" not in enrich._clean("正常内容 <填入你的理由>")

    def test_dict_is_flattened_to_string(self):
        """字段被返回成嵌套对象时拍平，否则整张卡会崩。"""
        out = enrich._clean({"a": "第一点", "b": "第二点"})
        assert "第一点" in out and "第二点" in out
        assert isinstance(out, str)

    def test_list_is_flattened_to_string(self):
        out = enrich._clean(["要点一", "要点二"])
        assert "要点一" in out and "要点二" in out
        assert isinstance(out, str)

    def test_none_and_empty_become_empty_string(self):
        assert enrich._clean(None) == ""
        assert enrich._clean("") == ""
        assert enrich._clean("   ") == ""

    def test_trailing_dangling_punctuation_removed(self):
        assert enrich._clean("这是一句话，") == "这是一句话"

    def test_repeated_punctuation_collapsed(self):
        assert enrich._clean("什么？？？") == "什么？"

    def test_empty_parens_removed(self):
        assert "()" not in enrich._clean("说明（）")

    def test_normal_text_survives_readably(self):
        s = "一个本地文件整理工具，支持批量重命名"
        assert enrich._clean(s) == s


class TestRuleBasedFallback:
    """AI 完全不可用时的降级产物 —— 它必须始终可用，否则整条流水线断了。"""

    REPO = {"description": "A self-hosted note taking app"}

    def test_always_returns_all_card_fields(self):
        card = enrich._rule_based(self.repo(), {"len": 3000, "good": {}, "bad": {}})
        for key in ("summary_zh", "audience", "deploy_note", "monetize", "risk"):
            assert key in card and isinstance(card[key], str) and card[key]

    def test_marked_as_rule_fallback(self):
        card = enrich._rule_based(self.repo(), {"len": 100, "good": {}, "bad": {}})
        assert card["_fallback"] == "rule"

    def test_docker_signal_shows_up_in_deploy_note(self):
        card = enrich._rule_based(self.repo(), {"len": 3000, "good": {"docker": True}, "bad": {}})
        assert "Docker" in card["deploy_note"]

    def test_no_deploy_signals_says_so_instead_of_lying(self):
        card = enrich._rule_based(self.repo(), {"len": 3000, "good": {}, "bad": {}})
        assert "人工" in card["deploy_note"]

    def test_gpu_and_api_key_risks_are_surfaced(self):
        card = enrich._rule_based(
            self.repo(), {"len": 3000, "good": {}, "bad": {"gpu": True, "api_key": True}})
        assert "GPU" in card["risk"] and "API Key" in card["risk"]

    def test_existing_chinese_readme_is_flagged_as_low_gap(self):
        card = enrich._rule_based(self.repo(), {"len": 3000, "good": {}, "bad": {},
                                                "has_cjk": True})
        assert "信息差" in card["risk"]

    def test_missing_description_does_not_crash(self):
        card = enrich._rule_based({}, {"len": 10, "good": {}, "bad": {}})
        assert "无简介" in card["summary_zh"]

    @staticmethod
    def repo():
        return {"description": "A self-hosted note taking app"}
