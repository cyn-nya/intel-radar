"""打分引擎：把"这个仓库值不值得做成一门生意"拆成可计算的子分。

三个子分（各自归一化到 0..1）：

* **momentum 增速**     —— 最近 N 天平均每天涨多少星（自己用快照差分算出来的真值）
* **cn_gap 中文稀缺度** —— 中文资料越少，信息差越大
* **deployability 可部署性** —— 能不能真的跑起来、能不能写成一键部署教程

再加一组"劝退项"（要 GPU / 要企业级设施 / 要买商业 API / 疑似刷星）做扣分。
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from typing import Any

CJK = re.compile(r"[\u4e00-\u9fff]")

# README 里出现这些，说明部署链路成熟
GOOD_SIGNALS = {
    "docker": r"\bdocker(-|\s)?(compose|run|build)?\b",
    "oneclick": r"(install\.(sh|ps1|bat)|一键|one[- ]?click|quick[- ]?start\.sh|make\s+install|setup\.(sh|ps1))",
    "release": r"(releases?/latest|\.exe\b|\.dmg\b|\.AppImage\b|download\s+the\s+(latest\s+)?(release|binary)|下载安装包)",
    "onecmd": r"(pip\s+install|npm\s+i(nstall)?\s|npx\s|go\s+install|cargo\s+install|brew\s+install|uvx?\s+|docker\s+pull)",
    "webui": r"(localhost:\d+|127\.0\.0\.1:\d+|web\s*(ui|interface)|screenshot|dashboard|界面|打开浏览器)",
    "quickstart": r"(quick\s*start|getting\s+started|installation|快速开始|安装)",
}

BAD_SIGNALS = {
    "gpu": r"(cuda|nvidia|gpu|vram|显存|显卡|torch\.cuda|requires?\s+a?\s*gpu)",
    "enterprise": r"(kubernetes|k8s|helm\s+chart|terraform|enterprise\s+(edition|license)|集群部署)",
    "api_key": r"(api[_ -]?key\s+(is\s+)?required|get\s+your\s+api\s+key|openai_api_key|需要申请.{0,6}(key|密钥)|付费\s*api)",
    "library_only": r"(this\s+is\s+a\s+(python|javascript|go|rust)?\s*(library|sdk|framework)|^#\s*\w+\s+library\b)",
}


def has_cjk(text: str | None) -> bool:
    return bool(text and CJK.search(text))


def cjk_ratio(text: str | None, sample: int = 4000) -> float:
    if not text:
        return 0.0
    seg = text[:sample]
    if not seg:
        return 0.0
    hits = len(CJK.findall(seg))
    return hits / max(1, len(seg))


# ----------------------------------------------------------------------
def analyze_readme(text: str | None) -> dict[str, Any]:
    """纯函数：从 README 里抽取部署信号、劝退信号、中文占比。"""
    low = (text or "").lower()
    good = {k: bool(re.search(p, low, re.I | re.M)) for k, p in GOOD_SIGNALS.items()}
    bad = {k: bool(re.search(p, low, re.I | re.M)) for k, p in BAD_SIGNALS.items()}
    return {
        "len": len(text or ""),
        "has_cjk": has_cjk(text),
        "cjk_ratio": round(cjk_ratio(text), 5),
        "good": good,
        "bad": bad,
        "good_count": sum(1 for v in good.values() if v),
    }


# ----------------------------------------------------------------------
def snapshots_velocity(history: list[dict[str, Any]], stars_now: int,
                       windows: list[int], min_span_hours: float = 6.0) -> dict[str, Any]:
    """用快照历史算真实增速。

    ``history`` 按时间倒序（最新的在前），每项含 ``stars`` 与 ``captured_at``。
    返回 ``{window, delta, rate_per_day, acceleration}``。

    **为什么要 ``min_span_hours``**：一天跑好几次时，两次快照可能只隔几分钟。
    实测 9 分钟的差分被线性外推成「1691 星/天」——而那个仓库总共才 2203 星。
    所以跨度不够长时宁可不算，退回长期均速并标注「估算」，也不给一个荒谬的数。
    """
    if not history:
        return {"window": None, "delta": 0.0, "rate_per_day": 0.0, "acceleration": 0.0,
                "span_hours": None}

    now = datetime.now()
    points: list[tuple[datetime, int]] = []
    for h in history:
        try:
            points.append((datetime.fromisoformat(str(h["captured_at"])), int(h["stars"])))
        except Exception:
            continue
    if not points:
        return {"window": None, "delta": 0.0, "rate_per_day": 0.0, "acceleration": 0.0,
                "span_hours": None}

    points.sort(key=lambda p: p[0])                 # 时间正序
    oldest_t, oldest_s = points[0]
    span_hours = (now - oldest_t).total_seconds() / 3600.0
    span_delta = float(stars_now - oldest_s)

    chosen_window = None
    for w in sorted(windows):
        if span_hours >= w * 24 * 0.75:             # 至少覆盖 75% 的窗口
            chosen_window = w
    if chosen_window is None:
        # 历史还不够长。跨度太短就不外推 —— 分钟级差分放大成日增毫无意义。
        if span_hours < max(1.0, min_span_hours):
            return {"window": None, "delta": span_delta, "rate_per_day": 0.0,
                    "acceleration": 0.0, "span_hours": round(span_hours, 2),
                    "estimated": True, "reason": "span_too_short"}
        rate = span_delta / (span_hours / 24.0)
        return {"window": None, "delta": span_delta,
                "rate_per_day": float(rate), "acceleration": 0.0,
                "span_hours": round(span_hours, 2), "estimated": True,
                "reason": "span_extrapolated"}

    target = now - timedelta(days=chosen_window)
    base = None
    for t, s in points:
        if t <= target:
            base = s
        else:
            break
    if base is None:
        base = oldest_s
    delta = float(stars_now - base)
    rate = delta / chosen_window

    # 加速度：最近半段 vs 前半段
    accel = 0.0
    mid_t = now - timedelta(days=chosen_window / 2)
    mid_s = None
    for t, s in points:
        if t <= mid_t:
            mid_s = s
    if mid_s is not None:
        recent_rate = (stars_now - mid_s) / max(0.5, chosen_window / 2)
        earlier_rate = (mid_s - base) / max(0.5, chosen_window / 2)
        accel = recent_rate - earlier_rate

    return {"window": chosen_window, "delta": delta, "rate_per_day": float(rate),
            "acceleration": float(accel), "span_hours": round(span_hours, 2),
            "estimated": False}


# ----------------------------------------------------------------------
def _norm_rate(rate_per_day: float) -> float:
    """把"每天涨多少星"压到 0..1。每天涨 150 星 ≈ 满分。"""
    if rate_per_day <= 0:
        return 0.0
    return min(1.0, math.log1p(rate_per_day) / math.log1p(150))


def _lifetime_rate(repo: dict[str, Any]) -> float:
    """终身平均增速 = 总星数 ÷ 仓库年龄（天）。

    首次运行时还没有历史快照，用它做真实增速的替身，
    否则冷启动那一次所有仓库都会算成 0 分。增长通常在早期更快，
    所以调用方会再打个折。
    """
    created = repo.get("created_at")
    stars = int(repo.get("stars") or 0)
    if not created or stars <= 0:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return 0.0
    age_days = max(1.0, (datetime.now() - dt).total_seconds() / 86400.0)
    return stars / age_days


def compute_cn_gap(readme_info: dict[str, Any], variants: list[str],
                   bing_hits: int | None, cn_repo_hits: int | None,
                   node: dict[str, Any]) -> float:
    """中文稀缺度 0..1，越高 = 中文资料越缺 = 信息差越大。"""
    gap = 1.0
    if node.get("check_readme_cjk", True) and readme_info.get("has_cjk"):
        ratio = float(readme_info.get("cjk_ratio") or 0)
        # README 里中文占比越高，稀缺度越低
        gap *= max(0.10, 1.0 - min(0.9, ratio * 12))
    if node.get("check_readme_variants", True) and variants:
        gap *= 0.45
    if bing_hits is not None:
        # 中文搜索结果越多，说明已经有人写过
        if bing_hits >= 8:
            gap *= 0.35
        elif bing_hits >= 3:
            gap *= 0.65
        elif bing_hits == 0:
            gap = min(1.0, gap * 1.05)
    if cn_repo_hits is not None and cn_repo_hits > 0:
        gap *= max(0.25, 1.0 - min(0.75, cn_repo_hits * 0.12))
    return round(max(0.0, min(1.0, gap)), 4)


def compute_deployability(readme_info: dict[str, Any]) -> float | None:
    """可部署性 0..1：越接近"小白能一键跑起来"越高。

    **README 缺失时返回 ``None`` 而不是 0** —— 那是"不知道"，不是"很难部署"。
    把它当成 0 会让所有没抓到 README 的仓库分数一起塌到底部，排名失去意义。
    """
    if not readme_info.get("len"):
        return None
    good = readme_info.get("good") or {}
    weights = {"docker": 0.22, "oneclick": 0.18, "release": 0.14,
               "onecmd": 0.14, "webui": 0.16, "quickstart": 0.16}
    score = sum(w for k, w in weights.items() if good.get(k))
    bad = readme_info.get("bad") or {}
    if bad.get("library_only"):
        score -= 0.30
    if readme_info.get("len", 0) < 400:
        score -= 0.20                              # README 太短，多半是玩具或纯库
    return round(max(0.0, min(1.0, score)), 4)


def demand_factor(repo: dict[str, Any]) -> float:
    """受众规模：星标越多，说明想要它的人越多，同样的"中文稀缺"就越值钱。

    一个 4000 星、中文零资料的项目，和一个 200 星、中文零资料的项目，
    信息差一样大，但前者的市场大得多。
    """
    stars = int(repo.get("stars") or 0)
    if stars <= 0:
        return 0.0
    return min(1.0, math.log1p(stars) / math.log1p(5000))


def momentum_percentiles(rates: dict[int, float]) -> dict[int, float]:
    """把整池的增速换算成 0..1 的**相对排名**。

    绝对阈值（比如"每天 150 星算满分"）在行情好的时候会让一大片仓库一起顶格，
    排名就废了。相对排名永远有区分度。
    """
    if not rates:
        return {}
    items = sorted(rates.items(), key=lambda kv: kv[1])
    n = len(items)
    if n == 1:
        return {items[0][0]: 0.5}
    out: dict[int, float] = {}
    for i, (rid, _) in enumerate(items):
        out[rid] = i / (n - 1)
    return out



# ----------------------------------------------------------------------
def hard_filter(repo: dict[str, Any], node: dict[str, Any]) -> str | None:
    """返回淘汰原因，None 表示通过。"""
    stars = int(repo.get("stars") or 0)
    if stars < int(node.get("min_stars", 0)):
        return f"星标 {stars} 低于下限 {node.get('min_stars')}"
    if stars > int(node.get("max_stars", 10 ** 9)):
        return f"星标 {stars} 超过上限 {node.get('max_stars')}（中文教程多半已泛滥）"
    if node.get("exclude_archived", True) and repo.get("archived"):
        return "仓库已归档"
    if node.get("exclude_forks", True) and repo.get("is_fork"):
        return "是 fork"

    created = repo.get("created_at")
    if created:
        try:
            age = (datetime.now() - datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                   .replace(tzinfo=None)).days
            if age > int(node.get("max_age_days", 10 ** 6)):
                return f"创建已 {age} 天，超出窗口"
        except Exception:
            pass

    blob = " ".join([
        str(repo.get("full_name") or ""),
        str(repo.get("description") or ""),
        " ".join(repo.get("topics") or []),
    ]).lower()
    for kw in node.get("exclude_keywords") or []:
        if kw and str(kw).lower() in blob:
            return f"命中排除词「{kw}」"

    # 没有简介 = 无法判断这工具干嘛的 = 没法做成选题
    if len((repo.get("description") or "").strip()) < 12:
        return "没有简介，无法判断用途"

    langs = node.get("languages") or []
    if langs and (repo.get("language") or "") not in langs:
        return f"语言 {repo.get('language')} 不在白名单"

    req = node.get("require_any_keyword") or []
    if req and not any(str(k).lower() in blob for k in req):
        return "未命中任何必需关键词"

    return None


def effective_rate(repo: dict[str, Any], history: list[dict[str, Any]],
                   windows: list[int], min_span_hours: float = 6.0
                   ) -> tuple[float, dict[str, Any], str | None]:
    """算出这个仓库的有效日增星数（含冷启动估算）。返回 ``(rate, vel, estimate_kind)``。

    单独抽出来是为了让管道可以**先拿到整池的增速做排名**，再回来打分。
    """
    stars = int(repo.get("stars") or 0)
    vel = snapshots_velocity(history, stars, windows, min_span_hours)
    rate = float(vel.get("rate_per_day") or 0)
    kind = "snapshot" if vel.get("window") else ("span" if vel.get("estimated") else None)
    life = _lifetime_rate(repo)

    if vel.get("window") is None:
        # 短期外推容易失真（放量、刷星、或者间隔太短），最多信到长期均速的 3 倍
        if kind == "span" and life > 0:
            capped = life * 3.0
            if rate > capped:
                vel["rate_capped_from"] = round(rate, 1)
                vel["reason"] = "capped_by_lifetime"
                rate = capped
        if life > rate:
            rate = life * 0.6                      # 早期增长更快，打个六折更保守
            vel["rate_per_day"] = rate
            kind = "lifetime"
    vel["rate_per_day"] = rate
    vel["estimate_kind"] = kind
    return rate, vel, kind


def compute(repo: dict[str, Any], history: list[dict[str, Any]],
            readme_info: dict[str, Any], variants: list[str],
            bing_hits: int | None, cn_repo_hits: int | None,
            node: dict[str, Any], momentum_pct: float | None = None
            ) -> tuple[float, dict[str, Any]]:
    """算出总分与明细。返回 ``(score, detail)``。

    ``momentum_pct`` 是该仓库增速在整池里的相对排名（0..1）。给了就和绝对分混合使用，
    保证榜单在"行情好、大家涨得都快"的时候依然分得出高下。
    """
    windows = [int(w) for w in (node.get("momentum_windows") or [1, 3, 7])]
    stars = int(repo.get("stars") or 0)
    rate, vel, estimate_kind = effective_rate(
        repo, history, windows, float(node.get("min_span_hours", 6)))

    absolute_norm = _norm_rate(rate)
    if momentum_pct is None:
        momentum_norm = absolute_norm
    else:
        # 绝对分告诉你"涨得快不快"，相对分告诉你"在这批里排第几"，各占一半
        momentum_norm = 0.5 * absolute_norm + 0.5 * max(0.0, min(1.0, momentum_pct))

    accel = float(vel.get("acceleration") or 0)
    accel_bonus = 0.0
    if accel > 0:
        accel_bonus = min(0.12, math.log1p(accel) / math.log1p(60) * 0.12)

    cn_gap_raw = compute_cn_gap(readme_info, variants, bing_hits, cn_repo_hits,
                                node.get("_cn_gap") or {})
    demand = demand_factor(repo)
    # 稀缺本身不值钱，"稀缺 × 有人要"才值钱
    cn_gap = round(cn_gap_raw * (0.55 + 0.45 * demand), 4)

    deploy = compute_deployability(readme_info)

    w_m = float(node.get("weight_momentum", 0.42))
    w_c = float(node.get("weight_cn_gap", 0.33))
    w_d = float(node.get("weight_deployability", 0.25))

    # --- 缺数据的子分不参与加权，而是把权重让给其它子分 ---
    parts: list[tuple[float, float]] = [(momentum_norm, w_m), (cn_gap, w_c)]
    if deploy is not None:
        parts.append((deploy, w_d))
    total_w = sum(w for _, w in parts) or 1.0
    base = sum(v * w for v, w in parts) / total_w

    score = base + accel_bonus

    # --- 劝退扣分 ---
    bad = readme_info.get("bad") or {}
    penalties: dict[str, float] = {}
    if bad.get("gpu"):
        penalties["需要 GPU"] = float(node.get("penalty_gpu", 0.25))
    if bad.get("enterprise"):
        penalties["需要企业级设施"] = float(node.get("penalty_enterprise", 0.20))
    if bad.get("api_key"):
        penalties["需要商业 API Key"] = float(node.get("penalty_api_key", 0.15))
    if bad.get("library_only"):
        penalties["更像纯库/SDK"] = 0.12

    forks = int(repo.get("forks") or 0)
    issues = int(repo.get("open_issues") or 0)
    if stars >= 800 and (forks / max(1, stars)) < 0.02 and issues < 5:
        penalties["疑似刷星（fork/issue 异常低）"] = float(node.get("penalty_hype", 0.30))

    total_penalty = sum(penalties.values())
    score = max(0.0, min(1.0, score - total_penalty))

    detail = {
        "score": round(score, 4),
        "base": round(base, 4),
        "accel_bonus": round(accel_bonus, 4),
        "momentum": round(rate, 2),
        "momentum_norm": round(momentum_norm, 4),
        "momentum_absolute_norm": round(absolute_norm, 4),
        "momentum_percentile": None if momentum_pct is None else round(momentum_pct, 4),
        "momentum_delta": float(vel.get("delta") or 0),
        "momentum_window": vel.get("window"),
        "momentum_estimated": bool(vel.get("estimated")),
        "estimate_kind": estimate_kind,
        "acceleration": round(accel, 2),
        "cn_gap": cn_gap,
        "cn_gap_raw": cn_gap_raw,
        "demand": round(demand, 4),
        "deployability": deploy if deploy is not None else 0.0,
        "deployability_known": deploy is not None,
        "weights_used": {k: round(w, 3) for k, w in
                         zip(("momentum", "cn_gap", "deployability"),
                             (w_m, w_c, w_d if deploy is not None else 0.0))},
        "penalties": penalties,
        "readme_signals": readme_info.get("good"),
        "has_cjk_readme": bool(readme_info.get("has_cjk")),
        "readme_variants": variants,
        "bing_hits": bing_hits,
        "cn_repo_hits": cn_repo_hits,
    }
    return round(score, 4), detail
