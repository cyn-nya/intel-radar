"""Web 看板与 API。

零构建：直接托管 ``web/`` 下的静态文件，浏览器打开就是情报台。
"""
from __future__ import annotations

import json
import logging
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db, scheduler
from .config import ROOT, cfg
from .llm import router
from .notify import active_channels, dispatch
from .power import power_state
from .sources.github_trending import GitHubClient

log = logging.getLogger("radar.web")
WEB_DIR = ROOT / "web"

app = FastAPI(title="情报雷达", version="1.0.0", docs_url="/api/docs")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_run_lock = threading.Lock()
_run_state: dict[str, Any] = {"running": False, "started_at": None, "last_result": None}


# ----------------------------------------------------------------------
#  运行控制
# ----------------------------------------------------------------------
def _do_run(trigger: str, respect_power: bool) -> None:
    from .pipeline import run_open_source

    try:
        result = run_open_source(respect_power=respect_power, trigger=trigger)
        _run_state["last_result"] = result
    except Exception as exc:
        log.exception("手动运行失败")
        _run_state["last_result"] = {"ok": False, "error": str(exc)}
    finally:
        _run_state["running"] = False


@app.post("/api/run")
def api_run(respect_power: bool = Query(True), trigger: str = Query("manual")) -> dict[str, Any]:
    if _run_state["running"]:
        return {"ok": False, "busy": True, "message": "已经有一次采集在跑，等它结束"}
    _run_state.update({"running": True, "started_at": datetime.now().isoformat(timespec="seconds")})
    threading.Thread(target=_do_run, args=(trigger, respect_power), daemon=True).start()
    return {"ok": True, "busy": False, "message": "已开始采集，跑完看板会自动刷新"}


@app.get("/api/run/state")
def api_run_state() -> dict[str, Any]:
    return dict(_run_state)


# ----------------------------------------------------------------------
#  数据
# ----------------------------------------------------------------------
@app.get("/api/status")
def api_status() -> dict[str, Any]:
    db.init_db()
    return {
        "server_time": datetime.now().isoformat(timespec="seconds"),
        "version": "1.0.0",
        "repo_stats": db.repo_stats(),
        "schedule": scheduler.status(),
        "power": power_state(),
        "llm": router.probe(),
        "llm_stats": db.llm_stats(7),
        "channels": active_channels(),
        "run_state": dict(_run_state),
        "data_dir": str(cfg.data_dir),
        "github": {"authed": bool(cfg.secret("GITHUB_TOKEN"))},
    }


@app.get("/api/digest/latest")
def api_digest_latest(kind: str = "open_source") -> dict[str, Any]:
    d = db.latest_digest(kind)
    if not d:
        return {"empty": True, "items": []}
    # 分数/星标保持快照，但 AI 卡实时刷新 —— 点了「重新生成」要马上看到
    if isinstance(d.get("payload"), dict):
        db.hydrate_digest_ai(d["payload"])
    return d


@app.get("/api/digests")
def api_digests(kind: str = "open_source", limit: int = 30) -> dict[str, Any]:
    return {"items": db.list_digests(kind, limit)}


@app.get("/api/digests/{digest_id}")
def api_digest_one(digest_id: int) -> dict[str, Any]:
    d = db.get_digest(digest_id)
    if not d:
        raise HTTPException(404, "没有这份榜单")
    if isinstance(d.get("payload"), dict):
        db.hydrate_digest_ai(d["payload"])
    return d


@app.get("/api/repos")
def api_repos(status: str = "all", q: str = "", order: str = "score",
              min_score: float | None = None, limit: int = 300,
              offset: int = 0) -> dict[str, Any]:
    rows = db.query_repos(status=status or "all", q=q or None, min_score=min_score,
                          order=order, limit=limit, offset=offset)
    out = []
    for r in rows:
        try:
            detail = json.loads(r.get("score_detail") or "{}")
        except Exception:
            detail = {}
        try:
            topics = json.loads(r.get("topics") or "[]")
        except Exception:
            topics = []
        r = dict(r)
        r["topics"] = topics
        r["detail"] = detail
        r.pop("readme_text", None)
        out.append(r)
    return {"items": out, "stats": db.repo_stats()}


@app.get("/api/repo/{repo_id}")
def api_repo(repo_id: int) -> dict[str, Any]:
    r = db.get_repo(repo_id)
    if not r:
        raise HTTPException(404, "没有这个仓库")
    try:
        r["topics"] = json.loads(r.get("topics") or "[]")
    except Exception:
        r["topics"] = []
    try:
        r["detail"] = json.loads(r.get("score_detail") or "{}")
    except Exception:
        r["detail"] = {}
    r["history"] = db.snapshot_history(repo_id, limit=60)
    r["readme_excerpt"] = (r.get("readme_text") or "")[:4000]
    r.pop("readme_text", None)
    return r


@app.post("/api/repo/{repo_id}/status")
def api_repo_status(repo_id: int, status: str = Body(..., embed=True),
                    reason: str = Body("", embed=True)) -> dict[str, Any]:
    if status not in ("candidate", "tracking", "starred", "rejected"):
        raise HTTPException(400, "状态不合法")
    db.set_status(repo_id, status, reason or None)
    return {"ok": True, "status": status}


@app.post("/api/repo/{repo_id}/enrich")
def api_repo_enrich(repo_id: int) -> dict[str, Any]:
    """单独给某个仓库重新生成选题卡（忽略 reenrich 冷却）。"""
    from .enrich import make_card

    r = db.get_repo(repo_id)
    if not r:
        raise HTTPException(404, "没有这个仓库")
    fields, model = make_card(r, task="polish")
    db.set_ai(repo_id, fields, model)
    return {"ok": True, "model": model, "fields": fields}


@app.get("/api/runs")
def api_runs(limit: int = 20) -> dict[str, Any]:
    return {"items": db.recent_runs(limit)}


@app.get("/api/notifications")
def api_notifications(limit: int = 50) -> dict[str, Any]:
    return {"items": db.recent_notifications(limit)}


@app.get("/api/llm")
def api_llm() -> dict[str, Any]:
    return {"probe": router.probe(), "stats": db.llm_stats(14)}


@app.post("/api/llm/reprobe")
def api_llm_reprobe() -> dict[str, Any]:
    router.invalidate()
    return router.probe()


# ----------------------------------------------------------------------
#  配置
# ----------------------------------------------------------------------
@app.get("/api/config")
def api_config() -> dict[str, Any]:
    return cfg.dashboard_view()


EDITABLE = {
    "open_source.filters.min_stars", "open_source.filters.max_stars",
    "open_source.filters.exclude_keywords", "open_source.filters.languages",
    "open_source.discover.new_repos.days", "open_source.discover.new_repos.min_stars",
    "open_source.discover.new_repos.per_query", "open_source.discover.refresh_tracked",
    "open_source.scoring.weight_momentum", "open_source.scoring.weight_cn_gap",
    "open_source.scoring.weight_deployability",
    "open_source.cn_gap.bing_probe", "open_source.cn_gap.search_cn_ecosystem",
    "open_source.enrich.top_n", "open_source.enrich.reenrich_days",
    "open_source.deliver.notify_min_score", "open_source.deliver.notify_top_n",
    "open_source.deliver.daily_digest", "open_source.deliver.desktop_notify",
    "llm.mode",
    "notify.wecom.enabled", "notify.wecom.webhook",
    "notify.email.enabled", "notify.email.smtp_host", "notify.email.smtp_port",
    "notify.email.use_ssl", "notify.email.username", "notify.email.password",
    "notify.email.to",
    "notify.telegram.enabled", "notify.telegram.bot_token",
    "notify.telegram.chat_id", "notify.telegram.proxy",
    "schedule.enabled", "schedule.times",
    "schedule.power_guard.enabled", "schedule.power_guard.require_ac",
    "schedule.power_guard.min_battery",
}


@app.post("/api/config")
def api_config_save(patch: dict[str, Any] = Body(...)) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in (patch or {}).items():
        if key in EDITABLE:
            clean[key] = value
    if not clean:
        return {"ok": False, "message": "没有可写的字段"}
    cfg.set_overrides(clean)
    router.invalidate()
    # 通知/调度配置变了要重启调度器
    if any(k.startswith("schedule.") for k in clean):
        scheduler.stop()
        scheduler.start()
    return {"ok": True, "written": list(clean.keys()), "config": cfg.dashboard_view()}


@app.post("/api/config/reset")
def api_config_reset() -> dict[str, Any]:
    cfg.clear_overrides()
    return {"ok": True, "config": cfg.dashboard_view()}


@app.post("/api/notify/test")
def api_notify_test(channels: list[str] | None = Body(None, embed=True)) -> dict[str, Any]:
    title = "情报雷达 · 通知测试"
    body = f"如果你看到这条消息，说明通知通道已经打通。\n时间：{datetime.now():%Y-%m-%d %H:%M:%S}"
    report = dispatch(title, body, dedupe_key=f"test:{datetime.now():%Y%m%d%H%M%S}",
                      channels=channels or active_channels())
    return {"ok": True, "report": report}


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    return {"ok": True, "time": datetime.now().isoformat(timespec="seconds")}


@app.get("/api/diagnose")
def api_diagnose() -> dict[str, Any]:
    """一键体检：把每个外部依赖都实测一遍。"""
    out: dict[str, Any] = {"checks": {}, "time": datetime.now().isoformat(timespec="seconds")}

    # 本地服务
    out["checks"]["本地服务"] = {"ok": True, "detail": f"运行于 {cfg.get('app.host')}:{cfg.get('app.port')}"}

    # 电源
    p = power_state()
    out["checks"]["电源状态"] = {
        "ok": True,
        "detail": (f"电池 {p['percent']}%，{'已插电' if p['plugged'] else '使用电池'}"
                   if p["available"] else f"未检测到电池（{p['reason']}）"),
    }

    # AI
    probe = router.probe()
    out["checks"]["AI 引擎"] = {"ok": probe.get("effective") not in ("rule", "off"),
                                "detail": probe.get("summary")}
    out["checks"]["AI-本地"] = {"ok": bool(probe["local"].get("ok")), "detail": probe["local"].get("reason")}
    out["checks"]["AI-API"] = {"ok": bool(probe["api"].get("ok")), "detail": probe["api"].get("reason")}

    # GitHub
    try:
        cli = GitHubClient()
        rl = cli.rate_limit()
        out["checks"]["GitHub API"] = {
            "ok": True,
            "detail": (f"{'已认证' if rl['authed'] else '未认证'}｜核心 {rl['core_left']}/{rl['core_limit']}"
                       f"｜搜索 {rl['search_left']}/{rl['search_limit']}"),
        }
    except Exception as exc:
        out["checks"]["GitHub API"] = {"ok": False, "detail": f"不可用: {exc}"}

    # Bing（中文稀缺度探测）
    try:
        from .probes import bing_count
        n = bing_count("github trending 教程")
        out["checks"]["Bing 探测"] = {
            "ok": n is not None,
            "detail": f"返回 {n} 条结果" if n is not None else "不可用（会退化为仅用 README 判断中文稀缺度）",
        }
    except Exception as exc:
        out["checks"]["Bing 探测"] = {"ok": False, "detail": str(exc)}

    # 通知渠道
    chans = active_channels()
    out["checks"]["通知渠道"] = {"ok": bool(chans), "detail": "、".join(chans) or "未启用任何渠道"}
    for name in ("wecom", "email", "telegram"):
        if name in chans:
            out["checks"][f"通知-{name}"] = {"ok": True, "detail": "已配置（用「发送测试」验证连通性）"}

    out["ok"] = all(c.get("ok") for c in out["checks"].values())
    return out


# ----------------------------------------------------------------------
#  静态看板
# ----------------------------------------------------------------------
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
def index() -> Any:
    idx = WEB_DIR / "index.html"
    if not idx.exists():
        return JSONResponse({"error": "web/index.html 缺失"}, status_code=500)
    return FileResponse(str(idx))


@app.get("/favicon.ico")
def favicon() -> Any:
    # 204 按规范必须没有响应体。用 JSONResponse({}, status_code=204) 会带上 b"{}"，
    # 触发 uvicorn 的 "Response content longer than Content-Length"——
    # 浏览器每次开页面都请求 favicon，于是错误日志会被持续刷屏。
    return Response(status_code=204)


def _port_responds(host: str, port: int) -> bool:
    """端口上是否已经有一份看板在跑。

    用 /api/health 确认是我们自己的服务，而不是恰好占了同一端口的别的程序。
    """
    import socket

    probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    try:
        with socket.socket() as s:
            s.settimeout(0.8)
            if s.connect_ex((probe_host, port)) != 0:
                return False
    except OSError:
        return False
    try:
        with httpx.Client(timeout=2.5) as cli:
            r = cli.get(f"http://{probe_host}:{port}/api/health")
            return r.status_code == 200 and bool(r.json().get("ok"))
    except Exception as exc:
        # 端口通了但健康检查不通过 —— 可能是别的程序，也可能是我们自己正在启动中。
        log.debug("端口 %s:%s 已监听但 /api/health 未确认: %s", probe_host, port, exc)
        return False


def serve(open_browser: bool | None = None) -> None:
    import uvicorn

    db.init_db()
    stale = db.close_stale_runs()
    if stale:
        log.info("收尾了 %d 条上次被中断的运行记录", stale)
    host = str(cfg.get("app.host", "127.0.0.1"))
    port = int(cfg.get("app.port", 8787))
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"

    if open_browser is None:
        open_browser = bool(cfg.get("app.open_browser", True))

    # 重复双击启动器时，别再抛 [Errno 10048] 让人一头雾水 —— 直接把已有的那份用起来。
    if _port_responds(host, port):
        print()
        print(f"  看板已经在运行了  →  {url}")
        print("  直接用它就行，不需要再开一个。（这个窗口可以关掉）")
        print("  如果打不开，说明该端口被别的程序占用了，"
              "可以改 config.yaml 里的 app.port。")
        print()
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        return

    scheduler.start()
    try:
        scheduler.catch_up(reason="startup")
    except Exception:
        log.exception("补跑失败")

    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    print(f"\n  情报雷达已启动  →  {url}\n  按 Ctrl+C 停止\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
