"""SQLite 存储层。

设计要点
--------
* ``repos``      —— 候选池，一行一个 GitHub 仓库，带打分与 AI 加工结果
* ``snapshots``  —— 星标快照，**算出真实增速的命脉**（每采一次存一行）
* ``digests``    —— 每次交付的榜单原文（JSON），看板读它
* ``runs``       —— 每次运行的统计与错误
* ``llm_calls``  —— 每次 AI 调用的 token 与花费，用来看今天烧了多少钱
* ``kv``         —— 杂项状态（上次跑完的时间、补跑标记等）
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import cfg

_LOCAL = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS repos (
    repo_id           INTEGER PRIMARY KEY,
    full_name         TEXT UNIQUE NOT NULL,
    owner             TEXT,
    name              TEXT,
    description       TEXT,
    html_url          TEXT,
    homepage          TEXT,
    language          TEXT,
    topics            TEXT,
    license           TEXT,
    stars             INTEGER DEFAULT 0,
    forks             INTEGER DEFAULT 0,
    open_issues       INTEGER DEFAULT 0,
    watchers          INTEGER DEFAULT 0,
    size_kb           INTEGER DEFAULT 0,
    created_at        TEXT,
    pushed_at         TEXT,
    archived          INTEGER DEFAULT 0,
    is_fork           INTEGER DEFAULT 0,
    has_readme        INTEGER DEFAULT 0,
    readme_text       TEXT,
    readme_fetched_at TEXT,
    readme_variants   TEXT,
    first_seen        TEXT,
    last_seen         TEXT,
    momentum          REAL DEFAULT 0,
    acceleration      REAL DEFAULT 0,
    cn_gap            REAL DEFAULT 0,
    deployability     REAL DEFAULT 0,
    score             REAL DEFAULT 0,
    score_detail      TEXT,
    status            TEXT DEFAULT 'candidate',
    reject_reason     TEXT,
    ai_summary        TEXT,
    ai_audience       TEXT,
    ai_deploy         TEXT,
    ai_monetize       TEXT,
    ai_risk           TEXT,
    ai_model          TEXT,
    ai_at             TEXT,
    first_digest_day  TEXT
);
CREATE INDEX IF NOT EXISTS idx_repos_score ON repos(score DESC);
CREATE INDEX IF NOT EXISTS idx_repos_status ON repos(status);
CREATE INDEX IF NOT EXISTS idx_repos_lastseen ON repos(last_seen);

CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id     INTEGER NOT NULL,
    stars       INTEGER NOT NULL,
    forks       INTEGER DEFAULT 0,
    open_issues INTEGER DEFAULT 0,
    captured_at TEXT NOT NULL,
    day         TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_snap_unique ON snapshots(repo_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_snap_repo_time ON snapshots(repo_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS digests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    day        TEXT NOT NULL,
    kind       TEXT NOT NULL,
    title      TEXT,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_digest_day ON digests(day DESC, kind);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT,
    started_at  TEXT,
    finished_at TEXT,
    status      TEXT,
    stats       TEXT,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    day               TEXT NOT NULL,
    provider          TEXT,
    model             TEXT,
    task              TEXT,
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cost              REAL DEFAULT 0,
    ok                INTEGER DEFAULT 1,
    error             TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT UNIQUE,
    channel    TEXT,
    title      TEXT,
    body       TEXT,
    status     TEXT,
    error      TEXT,
    created_at TEXT,
    sent_at    TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


# --------------------------------------------------------------------------
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else cfg.db_path
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_conn() -> sqlite3.Connection:
    """每个线程一个连接（sqlite 连接不能跨线程随便用）。"""
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        conn = connect()
        conn.executescript(SCHEMA)
        _LOCAL.conn = conn
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()


# --------------------------------------------------------------------------
#  repos
# --------------------------------------------------------------------------
REPO_FIELDS = (
    "repo_id", "full_name", "owner", "name", "description", "html_url", "homepage",
    "language", "topics", "license", "stars", "forks", "open_issues", "watchers",
    "size_kb", "created_at", "pushed_at", "archived", "is_fork",
)


def upsert_repo(repo: dict[str, Any]) -> None:
    """写入或更新一个仓库（只更新采集字段，不动打分与 AI 结果）。"""
    conn = get_conn()
    data = {k: repo.get(k) for k in REPO_FIELDS}
    data["topics"] = json.dumps(repo.get("topics") or [], ensure_ascii=False)
    data["archived"] = 1 if repo.get("archived") else 0
    data["is_fork"] = 1 if repo.get("is_fork") else 0
    now = _now()
    data["first_seen"] = now
    data["last_seen"] = now

    cols = ", ".join(data.keys())
    ph = ", ".join(f":{k}" for k in data)
    updates = ", ".join(
        f"{k}=excluded.{k}" for k in data if k not in ("repo_id", "first_seen")
    )
    conn.execute(
        f"INSERT INTO repos ({cols}) VALUES ({ph}) "
        f"ON CONFLICT(repo_id) DO UPDATE SET {updates}",
        data,
    )
    conn.commit()


def add_snapshot(repo_id: int, stars: int, forks: int = 0, open_issues: int = 0,
                 captured_at: str | None = None) -> None:
    conn = get_conn()
    ts = captured_at or datetime.now().isoformat(timespec="minutes")
    conn.execute(
        "INSERT OR REPLACE INTO snapshots (repo_id, stars, forks, open_issues, captured_at, day) "
        "VALUES (?,?,?,?,?,?)",
        (repo_id, stars, forks, open_issues, ts, ts[:10]),
    )
    conn.commit()


def stars_at_or_before(repo_id: int, when: datetime) -> int | None:
    row = get_conn().execute(
        "SELECT stars FROM snapshots WHERE repo_id=? AND captured_at<=? "
        "ORDER BY captured_at DESC LIMIT 1",
        (repo_id, when.isoformat(timespec="minutes")),
    ).fetchone()
    return int(row["stars"]) if row else None


def stars_at_or_after(repo_id: int, when: datetime) -> int | None:
    row = get_conn().execute(
        "SELECT stars FROM snapshots WHERE repo_id=? AND captured_at>=? "
        "ORDER BY captured_at ASC LIMIT 1",
        (repo_id, when.isoformat(timespec="minutes")),
    ).fetchone()
    return int(row["stars"]) if row else None


def snapshot_history(repo_id: int, limit: int = 30) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT stars, forks, open_issues, captured_at FROM snapshots "
        "WHERE repo_id=? ORDER BY captured_at DESC LIMIT ?",
        (repo_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def prune_snapshots(keep_days: int = 120, min_gap_minutes: int = 60) -> int:
    """删掉太老的快照，并把同一天过密的快照压稀，避免库无限膨胀。"""
    conn = get_conn()
    cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat(timespec="minutes")
    cur = conn.execute("DELETE FROM snapshots WHERE captured_at < ?", (cutoff,))
    removed = cur.rowcount
    conn.commit()
    return removed


def update_score(repo_id: int, score: float, detail: dict[str, Any]) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE repos SET score=?, score_detail=?, momentum=?, acceleration=?, "
        "cn_gap=?, deployability=? WHERE repo_id=?",
        (
            round(float(score), 4),
            json.dumps(detail, ensure_ascii=False),
            float(detail.get("momentum", 0) or 0),
            float(detail.get("acceleration", 0) or 0),
            float(detail.get("cn_gap", 0) or 0),
            float(detail.get("deployability", 0) or 0),
            repo_id,
        ),
    )
    conn.commit()


def set_status(repo_id: int, status: str, reason: str | None = None) -> None:
    conn = get_conn()
    conn.execute("UPDATE repos SET status=?, reject_reason=? WHERE repo_id=?",
                 (status, reason, repo_id))
    conn.commit()


def set_readme(repo_id: int, text: str, variants: Sequence[str] | None = None) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE repos SET readme_text=?, readme_fetched_at=?, has_readme=1, readme_variants=? "
        "WHERE repo_id=?",
        (text[:200_000], _now(), json.dumps(list(variants or []), ensure_ascii=False), repo_id),
    )
    conn.commit()


def set_ai(repo_id: int, fields: dict[str, Any], model: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE repos SET ai_summary=?, ai_audience=?, ai_deploy=?, ai_monetize=?, "
        "ai_risk=?, ai_model=?, ai_at=? WHERE repo_id=?",
        (
            fields.get("summary_zh"), fields.get("audience"), fields.get("deploy_note"),
            fields.get("monetize"), fields.get("risk"), model, _now(), repo_id,
        ),
    )
    conn.commit()


def get_repo(repo_id: int) -> dict[str, Any] | None:
    row = get_conn().execute("SELECT * FROM repos WHERE repo_id=?", (repo_id,)).fetchone()
    return dict(row) if row else None


def repo_needs_readme(limit: int = 40) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT repo_id, full_name, stars FROM repos "
        "WHERE has_readme=0 AND status!='rejected' AND reject_reason IS NULL "
        "ORDER BY score DESC, stars DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def repo_needs_ai(limit: int, reenrich_days: int, min_score: float = 0.0,
                  upgrade_fallback: bool = False) -> list[dict[str, Any]]:
    """挑出需要（重新）生成 AI 选题卡的仓库。

    upgrade_fallback=True 时，**规则模板生成的卡也算"待升级"**。
    否则会出现这种情况：用户在没配 AI 时跑了一轮，拿到 12 张规则卡；
    第二天配好 AI 再跑，这 12 张因为"今天刚生成过"而被跳过，永远不会升级成真 AI 卡。
    """
    cutoff = (datetime.now() - timedelta(days=reenrich_days)).isoformat(timespec="seconds")
    cond = "(ai_at IS NULL OR ai_at < ?)"
    params: list[Any] = [min_score, cutoff]
    if upgrade_fallback:
        cond = f"({cond} OR ai_model IS NULL OR ai_model = 'rule-based')"
    rows = get_conn().execute(
        f"SELECT * FROM repos WHERE status!='rejected' AND score>=? AND {cond} "
        # 按分数排序。规则卡只是占位符，和"没卡"一样没用，所以不区分优先级 ——
        # AI 额度应该花在榜单头部（用户真正会看的那几条），而不是补低分仓库。
        "ORDER BY score DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [dict(r) for r in rows]


_AI_FIELDS = ("ai_summary", "ai_audience", "ai_deploy", "ai_monetize",
              "ai_risk", "ai_model", "ai_at")


def hydrate_digest_ai(payload: dict[str, Any]) -> dict[str, Any]:
    """把榜单条目里的 AI 字段刷新成仓库当前的值。

    榜单本身是快照 —— 分数和星标要冻结，才能反映"那一刻"的机会。
    但 AI 选题卡是**随时会被补写或升级的注解**：用户点「重新生成」，
    或者配好 AI 之后把规则卡升级成了真 AI 卡，看板就应该立刻反映出来，
    而不是等下一轮采集。
    """
    items = payload.get("items") or []
    ids = [it.get("repo_id") for it in items if it.get("repo_id")]
    if not ids:
        return payload
    placeholders = ",".join("?" * len(ids))
    rows = get_conn().execute(
        f"SELECT repo_id, {', '.join(_AI_FIELDS)} FROM repos "
        f"WHERE repo_id IN ({placeholders})",
        ids,
    ).fetchall()
    fresh = {r["repo_id"]: dict(r) for r in rows}
    for it in items:
        cur = fresh.get(it.get("repo_id"))
        if not cur:
            continue
        for f in _AI_FIELDS:
            if cur.get(f) is not None:
                it[f] = cur[f]
    return payload


def tracked_for_refresh(limit: int) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT repo_id, full_name, stars, forks, open_issues FROM repos "
        "WHERE status IN ('candidate','tracking','starred') "
        "ORDER BY last_seen DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def query_repos(status: str | None = None, q: str | None = None,
                min_score: float | None = None, order: str = "score",
                limit: int = 300, offset: int = 0) -> list[dict[str, Any]]:
    sql = "SELECT * FROM repos WHERE 1=1"
    args: list[Any] = []
    if status and status != "all":
        sql += " AND status=?"
        args.append(status)
    if q:
        sql += " AND (full_name LIKE ? OR description LIKE ? OR ai_summary LIKE ?)"
        like = f"%{q}%"
        args += [like, like, like]
    if min_score is not None:
        sql += " AND score>=?"
        args.append(min_score)
    order_col = {"score": "score DESC", "stars": "stars DESC", "momentum": "momentum DESC",
                 "seen": "last_seen DESC", "first_seen": "first_seen DESC"}.get(order, "score DESC")
    sql += f" ORDER BY {order_col} LIMIT ? OFFSET ?"
    args += [limit, offset]
    rows = get_conn().execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def repo_stats() -> dict[str, Any]:
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) c FROM repos").fetchone()["c"]
    by_status = {
        r["status"]: r["c"]
        for r in conn.execute("SELECT status, COUNT(*) c FROM repos GROUP BY status").fetchall()
    }
    today_new = conn.execute(
        "SELECT COUNT(*) c FROM repos WHERE substr(first_seen,1,10)=?", (today(),)
    ).fetchone()["c"]
    with_ai = conn.execute("SELECT COUNT(*) c FROM repos WHERE ai_at IS NOT NULL").fetchone()["c"]
    return {"total": total, "by_status": by_status, "today_new": today_new, "with_ai": with_ai}


# --------------------------------------------------------------------------
#  digests / runs / llm / notify / kv
# --------------------------------------------------------------------------
def save_digest(day: str, kind: str, title: str, payload: Any) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO digests (day, kind, title, payload, created_at) VALUES (?,?,?,?,?)",
        (day, kind, title, json.dumps(payload, ensure_ascii=False), _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def latest_digest(kind: str = "open_source") -> dict[str, Any] | None:
    row = get_conn().execute(
        "SELECT * FROM digests WHERE kind=? ORDER BY created_at DESC LIMIT 1", (kind,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    return d


def list_digests(kind: str = "open_source", limit: int = 30) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT id, day, kind, title, created_at FROM digests WHERE kind=? "
        "ORDER BY created_at DESC LIMIT ?",
        (kind, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_digest(digest_id: int) -> dict[str, Any] | None:
    row = get_conn().execute("SELECT * FROM digests WHERE id=?", (digest_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    return d


def start_run(kind: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO runs (kind, started_at, status) VALUES (?,?,'running')", (kind, _now())
    )
    conn.commit()
    return int(cur.lastrowid)


def close_stale_runs() -> int:
    """把上次进程被杀/断电时卡在 running 的记录收尾。

    否则运行记录里会永远挂着一条"进行中"的假记录，也会误导前端判断。
    """
    conn = get_conn()
    cur = conn.execute(
        "UPDATE runs SET status='interrupted', finished_at=? WHERE status='running'", (_now(),)
    )
    conn.commit()
    return int(cur.rowcount)


def finish_run(run_id: int, status: str, stats: dict[str, Any] | None = None,
               error: str | None = None) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE runs SET finished_at=?, status=?, stats=?, error=? WHERE id=?",
        (_now(), status, json.dumps(stats or {}, ensure_ascii=False), error, run_id),
    )
    conn.commit()


def recent_runs(limit: int = 20) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["stats"] = json.loads(d["stats"]) if d["stats"] else {}
        except Exception:
            d["stats"] = {}
        out.append(d)
    return out


def log_llm(provider: str, model: str, task: str, pin: int, pout: int,
            cost: float, ok: bool = True, error: str | None = None) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO llm_calls (ts, day, provider, model, task, prompt_tokens, "
        "completion_tokens, cost, ok, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (_now(), today(), provider, model, task, pin, pout, cost, 1 if ok else 0, error),
    )
    conn.commit()


def api_cost_today() -> float:
    row = get_conn().execute(
        "SELECT COALESCE(SUM(cost),0) c FROM llm_calls WHERE day=? AND provider='api'", (today(),)
    ).fetchone()
    return float(row["c"] or 0)


def llm_stats(days: int = 7) -> dict[str, Any]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT day, provider, COUNT(*) n, SUM(prompt_tokens) pin, "
        "SUM(completion_tokens) pout, SUM(cost) cost FROM llm_calls "
        "WHERE day >= date('now', ?) GROUP BY day, provider ORDER BY day DESC",
        (f"-{days} days",),
    ).fetchall()
    return {"by_day": [dict(r) for r in rows], "today_cost": api_cost_today()}


def notify_seen(dedupe_key: str) -> bool:
    row = get_conn().execute(
        "SELECT 1 FROM notifications WHERE dedupe_key=? AND status='sent'", (dedupe_key,)
    ).fetchone()
    return bool(row)


def log_notification(dedupe_key: str, channel: str, title: str, body: str,
                     status: str, error: str | None = None) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO notifications (dedupe_key, channel, title, body, status, error, created_at, sent_at) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(dedupe_key) DO UPDATE SET status=excluded.status, "
        "error=excluded.error, sent_at=excluded.sent_at",
        (dedupe_key, channel, title[:300], body[:4000], status, error, _now(),
         _now() if status == "sent" else None),
    )
    conn.commit()


def recent_notifications(limit: int = 50) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def kv_get(key: str, default: str | None = None) -> str | None:
    row = get_conn().execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def kv_set(key: str, value: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO kv (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, value),
    )
    conn.commit()
