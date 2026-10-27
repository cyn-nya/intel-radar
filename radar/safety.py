"""内容安全层。

为什么必须有这一层
------------------
GitHub 上"涨星快 + 中文教程少"的仓库里，有相当一部分是**漏洞利用、恶意软件、
DRM 破解、游戏外挂、翻墙工具、博彩**。这些恰好同时满足"信息差大"和"没人写中文教程"，
所以打分算法会**主动把它们排到最前面** —— 不拦的话，你按榜单做出来的东西
要么上不了架，要么直接踩线。

所以这里用一套规则做**硬拦截**（在打分之前就淘汰），理由写清楚，看板上可见。
它的目标不是"过滤一切敏感话题"，而是"别让你把违规的东西当成生意去做"。
"""
from __future__ import annotations

import re
from typing import Any

# (类别, 严重度, 说明, 正则)
RULES: list[tuple[str, str, str, str]] = [
    # ---------- 高危：直接违规 ----------
    ("exploit", "high", "漏洞利用 / 提权 / 越狱", r"""
        \bexploit\b | \b0day\b | zero[\s-]?day | \brce\b | remote\ code\ execution |
        privilege[\s-]?escalation | \blpe\b | shellcode | payload\ generator |
        cve-\d{4}-\d+ | \bpoc\b(?=.*(exploit|vuln|bypass|shell)) |
        (exploit|vuln\w*)[\s-]?poc\b | \bpoc[\s-]?(for|of)[\s-]?(cve|vuln) |
        kernel\ exploit | xnu[\s-]?kernel | 内核提权 | 提权漏洞 | 越权 | 未授权访问 |
        任意文件 | 反序列化漏洞 |
        jailbreak | rootkit | bootloader\ unlock | frp\ bypass | 激活锁 | iccid |
        (hack|exploit|bypass|crack|jailbreak|unlock|inject)\w*[\s_-]*
            (ios|iphone|ipad|android|xnu|kernel|ps[45]|switch|xbox|tpm|drm)\w* |
        (bypass|绕过)[\s-]?(auth|login|lock|signature|drm|payment|激活|授权|验证) |
        免登录 | 免授权 | 白嫖
    """),
    ("malware", "high", "恶意软件 / 攻击工具", r"""
        malware | ransomware | infostealer | stealer | keylogger | \bkeylog\b |
        \bbackdoor\b | \bbotnet\b | \bddos\b | \bphishing\b | 钓鱼网站 |
        credential[\s-]?(harvest|dump|stuffing) | password[\s-]?(dump|stealer) |
        token[\s-]?grabber | \bspyware\b | \bworm\b | \brat\b | remote\ access\ trojan |
        c2\ server | command[\s-]and[\s-]control | \bcobalt\s?strike\b | 免杀 | 木马 | 病毒 |
        steal\W{0,3}(my\W{0,3})?(bitcoin|crypto|wallet|eth|token|nft|credential|password) |
        窃取(密码|钱包|令牌|凭证) | (dump|extract)\w*\s+(lsass|sam|ntds|browser\s+password)
    """),
    ("circumvention", "high", "绕过厂商 / 硬件限制", r"""
        firmware[\s-]?unlock | unlock\ (gpu|firmware|vram|tensor|bios|driver|bootloader) |
        (gpu|vram|tensor)[\s-]?unlock | hackintosh | snapintosh |
        \b(boot|run|install|get)\w*\s+macos\s+on\s+(non|pc|amd|intel|asus|snapdragon|generic) |
        (bypass|解锁|绕过)\s*(厂商|硬件|固件|限制|锁) | 解锁(完整)?性能 |
        (downgrade|jailbreak)\s+(firmware|bios) | 绕过\s*(正版|付费|区域)\s*限制
    """),
    ("piracy", "high", "盗版 / DRM 破解", r"""
        \bdrm\b | fairplay | widevine | playready | \bcrack(ed|s)?\b | keygen |
        \bwarez\b | pirat | 盗版 | 破解版? | 激活工具 | kms[\s-]?activ |
        adobe[\s-]?crack | office[\s-]?activ | serial[\s-]?key | license[\s-]?bypass |
        注册机 | 去广告破解 | 付费内容解锁
    """),
    ("cheat", "high", "游戏外挂 / 作弊", r"""
        aimbot | wallhack | \besp\b\ hack | game[\s-]?cheat | 外挂 | 自动瞄准 | 自瞄 |
        anti[\s-]?cheat[\s-]?bypass | hwid[\s-]?spoofer | 过检测 | 透视挂
    """),
    ("evasion", "high", "翻墙 / 规避网络管控", r"""
        翻墙 | 科学上网 | 梯子 | \bvpn\b | shadowsocks | \bv2ray\b | \bxray\b |
        trojan[\s-]?go | \bclash\b | sing[\s-]?box | \bgfw\b | anti[\s-]?censor |
        proxy[\s-]?pool | 代理池 | 免费节点 | 机场节点 | 订阅节点
    """),
    ("adult", "high", "色情 / 成人内容", r"""
        \bnsfw\b | \bporn\w*\b | \bhentai\b | 18\+ | 成人内容 | 色情 | 约炮 | 福利姬
    """),
    ("gambling", "high", "博彩 / 刷单 / 洗钱", r"""
        博彩 | 赌场 | \bcasino\b | \bbetting\b | 刷单 | 返利盘 | 跑分平台 | 洗钱 |
        \bponzi\b | 资金盘
    """),
    # ---------- 中危：不是不能碰，但不适合做成"小白部署教程" ----------
    ("privacy", "medium", "侵犯隐私 / 监控他人", r"""
        stalkerware | spy\ on\ (someone|partner|wife|husband) | phone\ tracker |
        位置追踪 | 监控(老公|老婆|伴侣|对象) | 查开房 | 人肉搜索 | 开盒
    """),
    ("crypto_spec", "medium", "炒币 / 薅羊毛脚本", r"""
        trading[\s-]?bot | 炒币 | 量化交易 | 合约跟单 | 薅羊毛 | 空投脚本 |
        airdrop[\s-]?bot | sniper[\s-]?bot | \bmev\b\ bot | 搬砖套利脚本
    """),
    ("academic", "medium", "学术不端 / 自动刷量", r"""
        论文降重 | 论文代写 | 自动刷课 | 刷(播放|阅读|点赞|粉丝) | 群发营销 |
        爬虫接单 | 数据抓取服务
    """),
    # ---------- 不是软件：涨星再快也做不成"一键部署" ----------
    ("not_software", "high", "不是可部署的软件（清单/提示词/合集）", r"""
        ^awesome[-_] | [-_]awesome\b | awesome[\s-](list|collection|tools) |
        curated\ list | a\ list\ of\ | collection\ of\ (awesome|resources|tools|links) |
        (resource|link|tool|paper)s?\s+collection | 资源合集 | 清单合集 | 大合集 |
        prompts?\s+(for|library|collection|pack|to\ use) | prompt\ library | 提示词库 |
        agent\ skills? | skills?\ that\ teach | \bskills\b\s+for\s+(claude|codex|ai|coding) |
        \bskills?\s+for\s+ai\b | \bskill\b\s+(pack|collection|library) |
        \bskills?\b\s+(to|that)\s+(teach|help)\s+(ai|agents?|claude|codex) |
        \broadmap\b | learning\ path | 学习路线 | 面试题 | interview\ questions |
        paper\ list | 论文清单 | \bdecomp\b.*\blist\b
    """),
]

_COMPILED: list[tuple[str, str, str, re.Pattern[str]]] = [
    (cat, sev, label, re.compile(pattern, re.I | re.X))
    for cat, sev, label, pattern in RULES
]

SEVERITY_LABEL = {"high": "高危", "medium": "中危"}


def scan(*texts: str | None) -> list[dict[str, str]]:
    """扫描文本，返回命中的风险项列表（可能为空）。"""
    blob = " \n ".join(t for t in texts if t)
    if not blob:
        return []
    hits: list[dict[str, str]] = []
    for cat, sev, label, rx in _COMPILED:
        m = rx.search(blob)
        if m:
            hits.append({
                "category": cat,
                "severity": sev,
                "label": label,
                "matched": (m.group(0) or "").strip()[:60],
            })
    return hits


def check_repo(repo: dict[str, Any], readme: str | None = None) -> list[dict[str, str]]:
    """检查一个仓库。高危命中即应直接淘汰。"""
    topics = repo.get("topics")
    if isinstance(topics, list):
        topics = " ".join(str(t) for t in topics)
    return scan(
        repo.get("full_name"),
        repo.get("name"),
        repo.get("description"),
        topics,
        (readme or "")[:20000],
    )


def worst_severity(hits: list[dict[str, str]]) -> str | None:
    if any(h["severity"] == "high" for h in hits):
        return "high"
    if any(h["severity"] == "medium" for h in hits):
        return "medium"
    return None


def reason_text(hits: list[dict[str, str]]) -> str:
    parts = []
    for h in hits[:3]:
        parts.append(f"{SEVERITY_LABEL.get(h['severity'], h['severity'])}·{h['label']}"
                     f"(命中「{h['matched']}」)")
    return "内容安全拦截：" + "；".join(parts)
