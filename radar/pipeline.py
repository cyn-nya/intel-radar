"""管道编排：采集 → 归一 → 过滤 → 打分 → AI 加工 → 交付 → 推送。

一条 ``run`` 就是跑完这一整条链。所有步骤都做了降级处理：
API 配额不够就少取一些、AI 不可用就走规则模板、通知失败不影响主流程。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any

from . import db, probes, safety, scoring
from .config import cfg
from .enrich import make_card
from .llm import router
from .power import should_defer
from .sources.github_trending import GitHubClient, build_queries, normalize

log = logging.getLogger("radar.pipeline")

KIND = "open_source"


# ----------------------------------------------------------------------
def _score_node() -> dict[str, Any]:
    node = dict(cfg.get("open_source.scoring", {}) or {})
    node["_cn_gap"] = cfg.get("open_source.cn_gap", {}) or {}
    return node


def _filters_node() -> dict[str, Any]:
    return cfg.get("open_source.filters", {}) or {}


def _pace(authed: bool, kind: str = "search") -> None:
    """按配额给请求降速，避免自己把自己限流。

    未认证时 search 只有 10 次/分钟，所以要压到 8 次/分钟以内。
    配了 token 就是 30 次/分钟，可以快很多。
    """
    if kind == "search":
        time.sleep(1.6 if authed else 7.6)
    else:
        time.sleep(0.12 if authed else 0.8)


# ----------------------------------------------------------------------
def _discover(client: GitHubClient, stats: dict[str, Any]) -> None:
    """第一轮：把候选池灌进数据库，并给每个仓库留一条星标快照。"""
    node = cfg.get("open_source.discover", {}) or {}
    queries = build_queries(node)
    authed = client.authed

    seen: set[int] = set()
    for i, spec in enumerate(queries):
        if client.search_left() <= 0 and i > 0:
            stats.setdefault("notes", []).append("search 配额用尽，提前结束发现阶段")
            break
        try:
            items = client.search_repos(spec["q"], per_page=int(spec.get("per_page", 30)))
        except Exception as exc:
            stats.setdefault("notes", []).append(f"查询「{spec['label']}」失败: {exc}")
            continue
        for raw in items:
            repo = normalize(raw)
            if not repo.get("repo_id"):
                continue
            db.upsert_repo(repo)
            db.add_snapshot(repo["repo_id"], repo["stars"], repo["forks"], repo["open_issues"])
            seen.add(repo["repo_id"])
        stats["discovered"] += len(items)
        stats.setdefault("queries", []).append({"label": spec["label"], "got": len(items)})
        if i < len(queries) - 1:
            _pace(authed, "search")

    stats["unique_discovered"] = len(seen)
    log.info("发现阶段完成：%d 条结果 / %d 个唯一仓库", stats["discovered"], len(seen))


def _refresh(client: GitHubClient, stats: dict[str, Any]) -> None:
    """第二轮：回访已跟踪仓库，刷新星标 —— 这是算出真实增速的来源。"""
    node = cfg.get("open_source.discover", {}) or {}
    if not node.get("refresh_tracked", True):
        return

    wanted = int(node.get("refresh_limit", 120))
    budget = client.core_budget()
    # 给 README 抓取留出余量
    allow = max(0, min(wanted, budget - 45))
    if allow <= 0:
        stats.setdefault("notes", []).append(
            f"核心 API 配额只剩 {budget}，本轮跳过回访刷新（配 GITHUB_TOKEN 可解除此限制）"
        )
        return

    targets = db.tracked_for_refresh(allow)
    refreshed = 0
    for row in targets:
        if client.core_budget() <= 8:
            stats.setdefault("notes", []).append("核心 API 配额不足，回访刷新提前结束")
            break
        try:
            raw = client.get_repo(row["full_name"])
        except Exception as exc:
            log.debug("刷新 %s 失败: %s", row["full_name"], exc)
            continue
        if not raw:
            continue
        repo = normalize(raw)
        db.upsert_repo(repo)
        db.add_snapshot(repo["repo_id"], repo["stars"], repo["forks"], repo["open_issues"])
        refreshed += 1
        _pace(client.authed, "core")
    stats["refreshed"] = refreshed
    log.info("回访刷新 %d 个仓库", refreshed)


def _fetch_readmes(client: GitHubClient, stats: dict[str, Any]) -> None:
    """给还没读过 README 的高分候选抓 README（打分和 AI 都要用它）。"""
    node = cfg.get("open_source.discover", {}) or {}
    wanted = int(node.get("readme_limit", 90))
    budget = client.core_budget()
    # 每份 README 只花 1 次核心调用，留 8 次余量即可。
    # 旧写法 min(45, (budget-8)//2) 把上限写死成 45，配了 token 也白白浪费几千次配额。
    allow = max(0, min(wanted, budget - 8))
    if allow <= 0:
        stats.setdefault("notes", []).append(
            f"核心 API 配额只剩 {budget}，本轮跳过 README 抓取（配 GITHUB_TOKEN 可解除）"
        )
        return

    pending = db.repo_needs_readme(allow)
    got = 0
    for row in pending:
        if client.core_budget() <= 6:
            stats.setdefault("notes", []).append("核心 API 配额不足，README 抓取提前结束")
            break
        try:
            bundle = client.get_repo_bundle(row["full_name"])
        except Exception as exc:
            log.debug("抓 README 失败 %s: %s", row["full_name"], exc)
            continue
        if bundle.get("readme"):
            db.set_readme(row["repo_id"], bundle["readme"], bundle.get("variants") or [])
            got += 1
        else:
            db.set_readme(row["repo_id"], "", [])
        _pace(client.authed, "core")
    stats["readmes"] = got
    log.info("抓到 %d 份 README", got)


# ----------------------------------------------------------------------
def _score_all(client: GitHubClient, stats: dict[str, Any]) -> None:
    """对候选池做硬筛 + 内容安全拦截 + 两遍打分。

    两遍是为了拿到"整池相对排名"：先算出每个仓库的增速，再按排名给分。
    只用绝对阈值的话，行情好的时候一大片仓库会一起顶格，榜单就没用了。
    """
    filters = _filters_node()
    node = _score_node()
    cn_node = cfg.get("open_source.cn_gap", {}) or {}
    windows = [int(w) for w in (node.get("momentum_windows") or [1, 3, 7])]
    # 快照跨度不足这个小时数就不外推日增（否则 9 分钟差分会被放大成上千星/天）
    min_span_hours = float(node.get("min_span_hours", 6))

    candidates = db.query_repos(status="candidate", order="stars", limit=800)
    stats["scored_pool"] = len(candidates)

    # ---------- 第一遍：硬筛 + 内容安全 ----------
    kept: list[dict[str, Any]] = []
    rejected = 0
    blocked = 0
    for repo in candidates:
        reason = scoring.hard_filter(repo, filters)
        if reason:
            db.set_status(repo["repo_id"], "rejected", reason)
            rejected += 1
            continue
        hits = safety.check_repo(repo)
        if not filters.get("block_non_software", True):
            hits = [h for h in hits if h["category"] != "not_software"]
        if safety.worst_severity(hits) == "high":
            db.set_status(repo["repo_id"], "rejected", safety.reason_text(hits))
            rejected += 1
            blocked += 1
            continue
        kept.append(repo)
    stats["blocked_unsafe"] = blocked

    # ---------- 第二遍：预备指标 ----------
    bing_budget = 30 if cn_node.get("bing_probe", True) else 0
    prepared: list[dict[str, Any]] = []
    for repo in kept:
        readme = repo.get("readme_text") or ""
        info = scoring.analyze_readme(readme)
        try:
            variants = json.loads(repo.get("readme_variants") or "[]")
        except Exception:
            variants = []
        if not isinstance(variants, list):
            variants = []

        # Bing 不消耗 GitHub 配额，所以即使没抓到 README 也要做，
        # 否则「中文稀缺度」会退化成对所有仓库都一样的常数。
        bing_hits = None
        pre_score = float(repo.get("score") or 0)
        stars = int(repo.get("stars") or 0)
        if (bing_budget > 0 and stars >= int(filters.get("min_stars", 0))
                and (pre_score == 0 or pre_score >= 0.30)):
            bing_hits = probes.cn_scarcity_probe(
                repo["full_name"], per_item=int(cn_node.get("bing_per_item", 1))
            )
            bing_budget -= 1

        cn_repo_hits = None
        if cn_node.get("search_cn_ecosystem") and client.search_left() > 2:
            cn_repo_hits = probes.cn_ecosystem_repos(client, repo["full_name"])
            _pace(client.authed, "search")

        hist = db.snapshot_history(repo["repo_id"], limit=60)
        rate, _, _ = scoring.effective_rate(repo, hist, windows, min_span_hours)
        prepared.append({"repo": repo, "info": info, "variants": variants,
                         "bing_hits": bing_hits, "cn_repo_hits": cn_repo_hits,
                         "history": hist, "rate": rate})

    pct_map = scoring.momentum_percentiles({p["repo"]["repo_id"]: p["rate"] for p in prepared})

    # ---------- 第三遍：打分 ----------
    scored = 0
    for p in prepared:
        repo = p["repo"]
        try:
            score, detail = scoring.compute(
                repo, p["history"], p["info"], p["variants"], p["bing_hits"],
                p["cn_repo_hits"], node, momentum_pct=pct_map.get(repo["repo_id"]),
            )
        except Exception as exc:
            log.warning("打分失败 %s: %s", repo["full_name"], exc)
            continue
        db.update_score(repo["repo_id"], score, detail)
        scored += 1

    stats["scored"] = scored
    stats["rejected"] = rejected
    log.info("打分完成：%d 个进池，%d 个淘汰（其中 %d 个因内容安全拦截）",
             scored, rejected, blocked)


def _enrich(stats: dict[str, Any]) -> None:
    """对得分靠前的仓库生成 AI 选题卡。"""
    node = cfg.get("open_source.enrich", {}) or {}
    top_n = int(node.get("top_n", 12))
    days = int(node.get("reenrich_days", 7))
    limit = min(top_n, int((cfg.get("llm.cost_guard", {}) or {}).get("max_bulk_items", 25)))

    todo = db.repo_needs_ai(limit, days, min_score=0.30,
                            upgrade_fallback=bool(router.available("bulk")))
    if not todo:
        stats["enriched"] = 0
        return

    models: set[str] = set()
    done = 0
    for repo in todo:
        fields, model = make_card(repo, task="bulk")
        db.set_ai(repo["repo_id"], fields, model)
        models.add(model)
        done += 1
    stats["enriched"] = done
    stats["enrich_models"] = sorted(models)
    log.info("AI 加工完成 %d 张选题卡（%s）", done, ", ".join(sorted(models)))


# ----------------------------------------------------------------------
def _build_digest(stats: dict[str, Any]) -> dict[str, Any]:
    """组装当天的榜单。"""
    deliver = cfg.get("open_source.deliver", {}) or {}
    filters = _filters_node()
    top_n = int(deliver.get("notify_top_n", 5))
    day = db.today()

    rows = db.query_repos(status="candidate", order="score", limit=40)
    rows = [r for r in rows if float(r.get("score") or 0) > 0][:max(20, top_n * 4)]

    items: list[dict[str, Any]] = []
    for r in rows:
        try:
            detail = json.loads(r.get("score_detail") or "{}")
        except Exception:
            detail = {}
        try:
            topics = json.loads(r.get("topics") or "[]")
        except Exception:
            topics = []
        is_new = (r.get("first_digest_day") is None)
        items.append({
            "repo_id": r["repo_id"],
            "full_name": r["full_name"],
            "html_url": r["html_url"],
            "homepage": r.get("homepage"),
            "description": r.get("description"),
            "language": r.get("language"),
            "stars": r.get("stars"),
            "forks": r.get("forks"),
            "license": r.get("license"),
            "topics": topics,
            "created_at": r.get("created_at"),
            "pushed_at": r.get("pushed_at"),
            "score": round(float(r.get("score") or 0), 4),
            "momentum": detail.get("momentum", 0),
            "momentum_window": detail.get("momentum_window"),
            "momentum_estimated": detail.get("momentum_estimated"),
            "momentum_delta": detail.get("momentum_delta", 0),
            "cn_gap": detail.get("cn_gap", 0),
            "deployability": detail.get("deployability", 0),
            "penalties": detail.get("penalties", {}),
            "readme_signals": detail.get("readme_signals", {}),
            "has_cjk_readme": detail.get("has_cjk_readme"),
            "bing_hits": detail.get("bing_hits"),
            "ai_summary": r.get("ai_summary"),
            "ai_audience": r.get("ai_audience"),
            "ai_deploy": r.get("ai_deploy"),
            "ai_monetize": r.get("ai_monetize"),
            "ai_risk": r.get("ai_risk"),
            "ai_model": r.get("ai_model"),
            "is_new": is_new,
            "detail": detail,
        })

    items.sort(key=lambda x: x["score"], reverse=True)
    payload = {
        "day": day,
        "kind": KIND,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dashboard_url": f"http://{cfg.get('app.host')}:{cfg.get('app.port')}",
        "filters": {"min_stars": filters.get("min_stars"), "max_stars": filters.get("max_stars")},
        "stats": stats,
        "items": items,
    }
    # 榜单取的是"当前分数前 N 名"，不是"今天新出现的" —— 重复上榜很正常。
    # 标题要如实反映，否则写着"20 个新机会"却大半是昨天就推过的。
    n_new = sum(1 for it in items if it["is_new"])
    title = f"开源雷达 · {day} · {len(items)} 个机会"
    if n_new:
        title += f"（{n_new} 个新上榜）"
    payload["new_count"] = n_new
    payload["title"] = title
    digest_id = db.save_digest(day, KIND, title, payload)

    # 标记首次上榜
    conn = db.get_conn()
    for it in items:
        if it["is_new"]:
            conn.execute("UPDATE repos SET first_digest_day=? WHERE repo_id=? AND first_digest_day IS NULL",
                         (day, it["repo_id"]))
    conn.commit()

    stats["digest_id"] = digest_id
    stats["digest_items"] = len(items)
    return payload


def _notify(payload: dict[str, Any], stats: dict[str, Any]) -> None:
    """推送：高分新条目逐条提醒 + 一份日报。"""
    from . import notify as notify_mod

    deliver = cfg.get("open_source.deliver", {}) or {}
    if not notify_mod.active_channels():
        stats["notified"] = 0
        return

    min_score = float(deliver.get("notify_min_score", 0.62))
    top_n = int(deliver.get("notify_top_n", 5))
    day = payload["day"]

    sent = 0
    fresh = [it for it in payload["items"] if it["is_new"] and it["score"] >= min_score][:top_n]
    for it in fresh:
        title, body = notify_mod.render_repo_alert(it)
        notify_mod.dispatch(title, body, dedupe_key=f"repo:{it['repo_id']}:{day}")
        sent += 1

    if deliver.get("daily_digest", True):
        fake_digest = {"day": day, "payload": payload, "dashboard_url": payload["dashboard_url"]}
        title, body = notify_mod.render_digest_text(fake_digest, max_items=max(3, top_n))
        notify_mod.dispatch(title, body, dedupe_key=f"digest:{KIND}:{day}")
        sent += 1

    stats["notified"] = sent


# ----------------------------------------------------------------------
def run_open_source(respect_power: bool = True, trigger: str = "manual") -> dict[str, Any]:
    """跑一次完整的开源雷达。返回统计信息。"""
    db.init_db()
    db.close_stale_runs()          # 上次被中断的记录先收尾，避免永远挂在"进行中"
    run_id = db.start_run(KIND)
    stats: dict[str, Any] = {
        "trigger": trigger, "discovered": 0, "unique_discovered": 0, "refreshed": 0,
        "readmes": 0, "scored": 0, "rejected": 0, "blocked_unsafe": 0, "enriched": 0,
        "notified": 0, "notes": [],
    }

    # --- 电源守卫 ---
    if respect_power:
        defer, why = should_defer(cfg.get("schedule.power_guard", {}) or {})
        if defer:
            stats["deferred"] = True
            stats["notes"].append(why)
            db.kv_set("pending_catchup", "1")
            db.finish_run(run_id, "deferred", stats)
            log.info("因电源原因跳过采集：%s", why)
            return {"ok": True, "deferred": True, "reason": why, "stats": stats}
        stats["power"] = why

    client = GitHubClient()
    try:
        rl = client.rate_limit()
        stats["rate_limit"] = rl
        stats["github_authed"] = client.authed
        if not client.authed:
            stats["notes"].append(
                "未配置 GITHUB_TOKEN：核心 API 只有 60 次/小时，本轮只能抓少量 README。"
                "去 https://github.com/settings/tokens 建一个免费 token 填进 .env 即可解除。"
            )
        stats["llm"] = router.probe()

        _discover(client, stats)
        _refresh(client, stats)
        _fetch_readmes(client, stats)
        _score_all(client, stats)
        _enrich(stats)
        payload = _build_digest(stats)
        _notify(payload, stats)

        db.kv_set("last_open_source_run", datetime.now().isoformat(timespec="seconds"))
        db.kv_set("last_open_source_day", db.today())
        db.kv_set("pending_catchup", "0")
        db.prune_snapshots()
        db.finish_run(run_id, "ok", stats)
        return {"ok": True, "deferred": False, "stats": stats,
                "digest_id": stats.get("digest_id"),
                "items": stats.get("digest_items", 0)}
    except Exception as exc:
        log.exception("运行失败")
        stats["notes"].append(f"运行异常: {exc}")
        db.finish_run(run_id, "error", stats, error=str(exc))
        return {"ok": False, "error": str(exc), "stats": stats}
