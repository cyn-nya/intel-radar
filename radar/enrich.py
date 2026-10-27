"""AI 加工：把一个仓库变成一张可以直接开工的「选题卡」。

产出五个字段（对应你那条变现链路）::

    summary_zh   这工具到底解决什么问题（中文，说人话）
    audience     什么人会需要它 / 谁愿意付钱
    deploy_note  部署要点与环境要求（写教程时要交代的前置条件）
    monetize     变现角度：能做成什么、卖点在哪、定价区间参考
    risk         风险与不适合谁（避免虚假承诺）

**AI 不可用时不会崩**，会退化成基于 README 信号的规则模板 —— 信息量少一些，但照样能用。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from .config import cfg
from .llm import router
from .scoring import analyze_readme

log = logging.getLogger("radar.enrich")

SYSTEM = """你是一个"开源选题情报分析师"，服务对象是想把开源工具做成中文教程/部署服务来变现的个人开发者。

你的判断必须**务实、具体、可执行**，并且**拒绝画大饼**：
- 不写"前景广阔""潜力巨大"这类空话；
- 不确定就说不确定，不要编造功能；
- 变现建议要落到"卖给谁、卖什么、大概多少钱"；
- 明确指出这项目**不适合谁**、有什么坑。

**每个字段都要写得紧凑**：这是给人快速扫一眼的简报，不是调研报告。
写长了不仅没人看，还会因为超出长度限制导致整段 JSON 作废。

只输出 JSON，不要输出任何解释性文字。"""

USER_TMPL = """请分析下面这个 GitHub 仓库，输出 JSON。

仓库: {full_name}
⭐ 星标: {stars}（近 {window} 天增加约 {delta} 颗）
语言: {language}
简介: {description}
主页: {homepage}
License: {license}
Topics: {topics}

README 摘要（截断）:
---
{readme}
---

请严格按下面的格式输出 JSON（尖括号里是填写要求，**不要把它们抄进结果**）:

{{
  "summary_zh": "<60字内：它解决什么问题、怎么用，讲人话>",
  "audience": "<60字内：具体到职业/处境，谁会需要它、谁愿意为部署教程付钱>",
  "deploy_note": "<90字内：系统与运行时要求、有没有 Docker 或一键脚本、最容易卡在哪>",
  "monetize": "<90字内：能做成什么形态（教程/一键包/代部署/订阅）、卖点、定价区间；没有变现价值就直说>",
  "risk": "<80字内：合规问题、维护活跃度、不适合谁>"
}}

填写要求：summary_zh 不要直译英文简介。monetize 要落到"卖给谁、卖什么、多少钱"。
risk 必须真正指出劝退点，宁可说"不确定"也不要编造。
**严格遵守每个字段的字数上限，全文合计不超过 400 字。**"""


# --- 输出清洗 ---------------------------------------------------------
# 小模型（尤其是 0.6b~4b 这种）经常把提示词里的"填写要求"原样抄进结果字段，
# 比如在 monetize 末尾附一句「若项目本身无变现价值，直接说明」。
# 这种句子如果被用户直接贴进商品页会很尴尬，所以在这里统一清掉。
_ECHO_PATTERNS = [
    # 注意交替顺序：长的放前面，否则 `说|说明` 会先匹配到 `说`，留下一个"明"
    r"若(这|该)?项目(本身)?(其实)?(并)?无(明显)?变现价值[，,。；;]?\s*(请)?直接(说明|注明|说)[。.！!]?",
    r"若(这|该)?项目(本身)?(其实)?(并)?无(明显)?变现价值[，,。；;]?",
    r"不要(直接)?(抄|直译)英文简介[，,]?\s*(要)?(讲人话|用中文)[。.！!]?",
    r"(请)?(具体)?落到[「\"']?卖给谁[」\"']?[、,，]?卖什么[、,，]?(大概)?多少钱[。.！!]?",
    r"宁可说[「\"']?不确定[」\"']?也不要编造[。.！!]?",
    r"必须真正指出劝退点[。.！!]?",
    r"待人工判断[：:]?",
]


def _dedupe_repeats(t: str) -> str:
    """清掉小模型常见的"复读"退化。

    qwen3:0.6b 这类小模型偶尔会卡在循环里，输出
    「开发者、开发者社区、开发者社区成员、开发者、开发者、开发者…」直到把
    token 预算耗光，然后 JSON 被截断、整张卡降级成规则模板。
    这里把连续重复的项去掉，至少保住已经生成出来的部分。
    """
    # 整段就是同一个短语反复：直接取一份
    whole = re.match(r"^(.{2,24}?)\1{2,}$", t)
    if whole:
        return whole.group(1)
    parts = re.split(r"([、，,；;])", t)
    out: list[str] = []
    prev: str | None = None
    for p in parts:
        if p and p in "、，,；;":
            # 分隔符只有在前后都有内容时才保留
            if out and out[-1] not in "、，,；;":
                out.append(p)
            continue
        key = p.strip()
        if not key:
            continue
        if key == prev:
            continue
        prev = key
        out.append(p)
    res = "".join(out).strip("、，,；; ")
    return re.sub(r"([、，,；;])\1+", r"\1", res)


def _clean(text: Any) -> str:
    """清掉提示词回显、占位符残留和多余标点。

    小模型偶尔会把字段返回成嵌套对象/数组，这里一并拍平成字符串，
    否则一条奇怪输出就能让整张卡崩掉。
    """
    if isinstance(text, dict):
        text = "；".join(str(v) for v in text.values() if v)
    elif isinstance(text, (list, tuple, set)):
        text = "；".join(str(v) for v in text if v)
    t = str(text or "").strip()
    for pat in _ECHO_PATTERNS:
        t = re.sub(pat, "", t, flags=re.I)
    # 模型偶尔会把骨架里的 <...> 占位符照抄出来
    t = re.sub(r"<\s*[^<>\n]{0,90}\s*>", "", t)
    t = re.sub(r"[（(]\s*[)）]", "", t)          # 空的括号
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\s*([，,。；;！!？?])\s*(?=[，,。；;！!？?])", "", t)   # 连续标点
    t = _dedupe_repeats(t)
    t = t.strip()
    t = t.strip("，,。；;、:：")                  # 结尾悬空标点
    return t


def _rule_based(repo: dict[str, Any], readme_info: dict[str, Any]) -> dict[str, str]:
    """AI 不可用时的降级模板：基于 README 信号拼一份"能用但朴素"的卡片。"""
    good = readme_info.get("good") or {}
    bad = readme_info.get("bad") or {}

    deploy_bits = []
    for key, label in (("docker", "提供 Docker/Docker Compose"),
                       ("oneclick", "有一键安装脚本"),
                       ("release", "有现成安装包/Release 二进制"),
                       ("onecmd", "支持包管理器一行命令安装"),
                       ("webui", "带 Web 界面"),
                       ("quickstart", "README 有明确的快速开始章节")):
        if good.get(key):
            deploy_bits.append(label)
    deploy = "；".join(deploy_bits) if deploy_bits else "README 未给出明显的部署线索，需要人工读一遍"

    risks = []
    if bad.get("gpu"):
        risks.append("需要 GPU/CUDA，受众大幅收窄")
    if bad.get("enterprise"):
        risks.append("涉及 K8s/企业级设施，普通用户部署成本高")
    if bad.get("api_key"):
        risks.append("需要自备商业 API Key，会产生持续费用")
    if bad.get("library_only"):
        risks.append("更像纯库/SDK，不适合做「一键部署」类教程")
    if readme_info.get("has_cjk"):
        risks.append("README 已含中文，信息差较小")

    desc = repo.get("description") or "（无简介）"
    return {
        "summary_zh": f"{desc}（⚠️ 未启用 AI，此处为原始英文简介）",
        "audience": "待人工判断：需先确认这个工具的目标用户是不是非技术人群。",
        "deploy_note": deploy,
        "monetize": "待人工判断：建议先本地跑通一遍，看卡点有多少、值不值得写成教程。",
        "risk": "；".join(risks) if risks else "未发现明显劝退信号，但仍需人工核实维护活跃度。",
        "_fallback": "rule",
    }


def _readme_cap() -> int:
    """喂给模型的 README 字符上限，按**实际会先用的那个引擎**决定。

    本地小模型在 CPU 上推理时，6000 字的 README 会让每次调用慢到超时；
    API 则完全吃得下。写死一个值必然顾此失彼。
    """
    api_c = cfg.get("llm.api", {}) or {}
    local_c = cfg.get("llm.local", {}) or {}
    try:
        chain = router.available("bulk")
    except Exception:
        chain = []
    if chain and chain[0] == "api":
        return int(api_c.get("max_readme_chars", 6000))
    return int(local_c.get("max_readme_chars", 2500))


def make_card(repo: dict[str, Any], readme_text: str | None = None,
              task: str = "bulk", force_rule: bool = False) -> tuple[dict[str, Any], str]:
    """生成选题卡。返回 ``(fields, model_label)``。"""
    readme = readme_text if readme_text is not None else (repo.get("readme_text") or "")
    readme_info = analyze_readme(readme)

    if force_rule or not router.available(task):
        return _rule_based(repo, readme_info), "rule-based"

    detail = {}
    try:
        import json as _json
        detail = _json.loads(repo.get("score_detail") or "{}")
    except Exception:
        detail = {}

    prompt = USER_TMPL.format(
        full_name=repo.get("full_name"),
        stars=repo.get("stars"),
        window=detail.get("momentum_window") or "—",
        delta=int(detail.get("momentum_delta") or 0),
        language=repo.get("language") or "未知",
        description=(repo.get("description") or "（无）")[:400],
        homepage=repo.get("homepage") or "（无）",
        license=repo.get("license") or "未知",
        topics=", ".join((repo.get("topics") or [])[:15]) if isinstance(repo.get("topics"), list)
               else str(repo.get("topics") or ""),
        readme=(readme or "（未取到 README）")[:_readme_cap()],
    )

    # max_tokens 是安全网，不是长度控制手段：实测未约束时模型会把 5 个字段各写 400 字，
    # 撞上 1400 的预算后 JSON 被截断、整张卡作废。真正的约束在提示词的字数上限里。
    res = router.chat(SYSTEM, prompt, task=task, json_mode=True, max_tokens=2200, temperature=0.35)
    if res is None:
        return _rule_based(repo, readme_info), "rule-based"

    data = res.json()
    if not isinstance(data, dict):
        # 小模型有时会陷入复读、把 JSON 截断。降到近乎贪心再试一次，
        # 比直接退回信息量很低的规则模板划算。
        log.warning("AI 返回不是合法 JSON，用低温度重试一次: %s", (res.text or "")[:150])
        retry = router.chat(SYSTEM, prompt, task=task, json_mode=True,
                            max_tokens=2200, temperature=0.05)
        if retry is not None:
            data2 = retry.json()
            if isinstance(data2, dict):
                res, data = retry, data2
    if not isinstance(data, dict):
        log.warning("重试后仍不是合法 JSON，降级到规则模板: %s", (res.text or "")[:200])
        return _rule_based(repo, readme_info), "rule-based"

    fields = {
        "summary_zh": _clean(data.get("summary_zh")),
        "audience": _clean(data.get("audience")),
        "deploy_note": _clean(data.get("deploy_note")),
        "monetize": _clean(data.get("monetize")),
        "risk": _clean(data.get("risk")),
    }
    if not fields["summary_zh"]:
        return _rule_based(repo, readme_info), "rule-based"
    return fields, f"{res.provider}:{res.model}"
