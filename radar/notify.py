"""通知层：桌面通知 / 企业微信 / 邮件 / Telegram。

设计原则
--------
* **配了才启用**：渠道没填配置就静默跳过，不算失败。
* **幂等**：同一个 dedupe_key 只发一次（存在 notifications 表里）。
* **互不拖累**：任一渠道报错只记录，不影响其它渠道和主流程。
"""
from __future__ import annotations

import html
import json
import logging
import smtplib
import subprocess
import sys
import tempfile
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Any

import httpx

from . import db
from .config import cfg

log = logging.getLogger("radar.notify")

TOAST_PS = r"""
param([string]$Title, [string]$Body)
$ErrorActionPreference = 'Stop'
try {
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null
    $tpl = @"
<toast scenario="reminder">
  <visual>
    <binding template="ToastGeneric">
      <text>$Title</text>
      <text>$Body</text>
    </binding>
  </visual>
</toast>
"@
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml($tpl)
    $toast = New-Object Windows.UI.Notifications.ToastNotification $xml
    $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
    exit 0
} catch {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $ni = New-Object System.Windows.Forms.NotifyIcon
        $ni.Icon = [System.Drawing.SystemIcons]::Information
        $ni.BalloonTipTitle = $Title
        $ni.BalloonTipText  = $Body
        $ni.Visible = $true
        $ni.ShowBalloonTip(10000)
        Start-Sleep -Seconds 6
        $ni.Dispose()
        exit 0
    } catch { exit 1 }
}
"""


# --------------------------------------------------------------------------
def _enabled(name: str) -> bool:
    return bool(cfg.get(f"notify.{name}.enabled", False))


def _textify(title: str, body: str) -> str:
    return f"{title}\n\n{body}"


# --------------------------------------------------------------------------
#  各渠道实现
# --------------------------------------------------------------------------
def send_desktop(title: str, body: str) -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "非 Windows 平台，跳过桌面通知"
    try:
        tmp = Path(tempfile.gettempdir()) / "radar_toast.ps1"
        tmp.write_text(TOAST_PS, encoding="utf-8-sig")
        ps = "powershell.exe"
        proc = subprocess.run(
            [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(tmp),
             "-Title", title[:120], "-Body", body[:400]],
            capture_output=True, timeout=40,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode == 0:
            return True, "桌面通知已弹出"
        return False, f"powershell 返回 {proc.returncode}: {proc.stderr.decode('utf-8', 'ignore')[:200]}"
    except Exception as exc:
        return False, f"桌面通知失败: {exc}"


def send_wecom(title: str, body: str) -> tuple[bool, str]:
    c = cfg.get("notify.wecom", {}) or {}
    url = (c.get("webhook") or cfg.secret("WECOM_WEBHOOK") or "").strip()
    if not url:
        return False, "未配置企业微信 webhook"
    content = f"## {title}\n{body}"
    if len(content.encode("utf-8")) > 4000:
        content = content[:1200] + "\n\n…（内容过长已截断，详情见看板）"
    try:
        with httpx.Client(timeout=20) as cli:
            r = cli.post(url, json={"msgtype": "markdown", "markdown": {"content": content}})
        ok = r.status_code == 200 and (r.json().get("errcode") == 0)
        return ok, ("企业微信已送达" if ok else f"企业微信返回 {r.status_code} {r.text[:200]}")
    except Exception as exc:
        return False, f"企业微信失败: {exc}"


def send_email(title: str, body: str) -> tuple[bool, str]:
    c = cfg.get("notify.email", {}) or {}
    user = (c.get("username") or cfg.secret("SMTP_USERNAME") or "").strip()
    pwd = (c.get("password") or cfg.secret("SMTP_PASSWORD") or "").strip()
    to = [x for x in (c.get("to") or []) if x]
    if not (user and pwd and to):
        return False, "邮件渠道未配置完整（需要 username / password / to）"
    try:
        msg = MIMEText(f"<pre style='font:14px/1.7 -apple-system,Segoe UI,sans-serif;"
                       f"white-space:pre-wrap'>{html.escape(body)}</pre>", "html", "utf-8")
        msg["Subject"] = Header(title, "utf-8")
        msg["From"] = formataddr(("情报雷达", user))
        msg["To"] = ", ".join(to)
        host, port = c.get("smtp_host", "smtp.qq.com"), int(c.get("smtp_port", 465))
        if c.get("use_ssl", True):
            srv = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            srv = smtplib.SMTP(host, port, timeout=30)
            srv.starttls()
        with srv:
            srv.login(user, pwd)
            srv.sendmail(user, to, msg.as_string())
        return True, f"邮件已发往 {', '.join(to)}"
    except Exception as exc:
        return False, f"邮件失败: {exc}"


def send_telegram(title: str, body: str) -> tuple[bool, str]:
    c = cfg.get("notify.telegram", {}) or {}
    token = (c.get("bot_token") or cfg.secret("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = str(c.get("chat_id") or cfg.secret("TELEGRAM_CHAT_ID") or "").strip()
    if not (token and chat):
        return False, "Telegram 未配置（需要 bot_token / chat_id）"
    proxy = (c.get("proxy") or "").strip() or None
    text = _textify(title, body)[:4000]
    try:
        kwargs: dict[str, Any] = {"timeout": 25}
        if proxy:
            kwargs["proxy"] = proxy
        with httpx.Client(**kwargs) as cli:
            r = cli.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         json={"chat_id": chat, "text": text, "disable_web_page_preview": True})
        ok = r.status_code == 200 and r.json().get("ok")
        return bool(ok), ("Telegram 已送达" if ok else f"Telegram 返回 {r.status_code} {r.text[:200]}")
    except Exception as exc:
        return False, f"Telegram 失败: {exc}（本机直连 Telegram 通常不通，需要配 proxy）"


CHANNELS = {
    "desktop": send_desktop,
    "wecom": send_wecom,
    "email": send_email,
    "telegram": send_telegram,
}


# --------------------------------------------------------------------------
def active_channels() -> list[str]:
    """列出真正会被启用的渠道（桌面通知额外看 deliver.desktop_notify）。"""
    out = []
    if cfg.get("open_source.deliver.desktop_notify", True) or _enabled("desktop"):
        out.append("desktop")
    for name in ("wecom", "email", "telegram"):
        if _enabled(name):
            out.append(name)
    return out


def dispatch(title: str, body: str, dedupe_key: str,
             channels: list[str] | None = None) -> dict[str, Any]:
    """把一条消息发给所有启用的渠道。返回每个渠道的结果。"""
    report: dict[str, Any] = {"title": title, "results": {}}
    for name in (channels or active_channels()):
        fn = CHANNELS.get(name)
        if fn is None:
            continue
        # --- 幂等：同一条消息同一渠道只发一次 ---
        key = f"{dedupe_key}::{name}"
        if db.notify_seen(key):
            report["results"][name] = {"ok": True, "detail": "已发过，跳过"}
            continue
        try:
            ok, detail = fn(title, body)
        except Exception as exc:
            ok, detail = False, f"未捕获异常: {exc}"
        db.log_notification(key, name, title, body, "sent" if ok else "failed",
                            None if ok else detail)
        report["results"][name] = {"ok": ok, "detail": detail}
        log.info("通知[%s] %s -> %s", name, "OK" if ok else "FAIL", detail)
    return report


# --------------------------------------------------------------------------
#  内容渲染
# --------------------------------------------------------------------------
def render_digest_text(digest: dict[str, Any], max_items: int = 6) -> tuple[str, str]:
    """把一份榜单渲染成 (标题, 正文)。"""
    items = (digest.get("payload") or {}).get("items") or []
    day = digest.get("day", "")
    title = f"开源雷达 · {day} · {len(items)} 个新机会"
    lines: list[str] = []
    for i, it in enumerate(items[:max_items], 1):
        star = f"⭐{it.get('stars', 0)}"
        mom = it.get("momentum") or 0
        mom_txt = f" (+{int(mom)}/{it.get('momentum_window', 1)}天)" if mom else ""
        lines.append(f"{i}. {it.get('full_name')} {star}{mom_txt}")
        if it.get("ai_summary"):
            lines.append(f"   {it['ai_summary']}")
        elif it.get("description"):
            lines.append(f"   {str(it['description'])[:110]}")
        if it.get("ai_monetize"):
            lines.append(f"   💰 {it['ai_monetize']}")
        lines.append(f"   {it.get('html_url', '')}")
        lines.append("")
    if len(items) > max_items:
        lines.append(f"…还有 {len(items) - max_items} 条，见看板 {digest.get('dashboard_url', '')}")
    return title, "\n".join(lines).strip()


def render_repo_alert(repo: dict[str, Any]) -> tuple[str, str]:
    title = f"🔥 {repo.get('full_name')} 值得看"
    body = (
        f"⭐ {repo.get('stars')}  (+{int(repo.get('momentum') or 0)})\n"
        f"{repo.get('ai_summary') or repo.get('description') or ''}\n"
    )
    if repo.get("ai_monetize"):
        body += f"💰 {repo['ai_monetize']}\n"
    if repo.get("cn_gap") is not None:
        body += f"中文稀缺度 {float(repo.get('cn_gap') or 0):.0%}\n"
    body += str(repo.get("html_url") or "")
    return title, body
