/* 情报雷达 · 前端逻辑（零依赖、零构建） */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts) {
  const r = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}));
  if (!r.ok) throw new Error(`${r.status} ${await r.text().catch(() => "")}`.slice(0, 300));
  return r.json();
}

function toast(msg, kind) {
  const d = document.createElement("div");
  d.className = kind || "";
  d.textContent = msg;
  $("toast").appendChild(d);
  setTimeout(() => d.remove(), 4200);
}

/* ---------------- 标签页 ---------------- */
document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".view").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    $("view-" + t.dataset.view).classList.add("active");
    if (t.dataset.view === "pool") loadPool();
    if (t.dataset.view === "runs") { loadRuns(); loadNotify(); }
    if (t.dataset.view === "settings") loadSettings();
    if (t.dataset.view === "guide") renderGuide();
  };
});

/* ---------------- 状态栏 ---------------- */
let RUNNING = false;

async function loadStatus() {
  let s;
  try { s = await api("/api/status"); } catch (e) { toast("状态读取失败：" + e.message, "err"); return; }

  const p = s.power || {};
  const pw = $("pill-power");
  if (p.available) {
    pw.textContent = `电源 ${p.percent}% ${p.plugged ? "已插电" : "电池"}`;
    pw.className = "pill " + (p.plugged ? "ok" : "warn");
  } else { pw.textContent = "电源 桌面机"; pw.className = "pill"; }

  const ai = s.llm || {};
  const aiP = $("pill-ai");
  const eff = ai.effective;
  aiP.textContent = "AI " + ({ hybrid: "混合", local: "本地", api: "API", rule: "规则降级", off: "已关闭" }[eff] || eff);
  aiP.className = "pill " + (eff === "hybrid" ? "ok" : eff === "rule" || eff === "off" ? "warn" : "ok");
  aiP.title = ai.summary || "";

  const gh = $("pill-github");
  const g = s.github || {};
  gh.textContent = g.authed ? "GitHub 已认证" : "GitHub 未认证";
  gh.className = "pill " + (g.authed ? "ok" : "warn");
  gh.title = g.authed ? "核心配额 5000/小时" : "未配 token，核心配额只有 60 次/小时，会限制 README 抓取";

  const sc = s.schedule || {};
  const scP = $("pill-sched");
  scP.textContent = sc.enabled ? `调度 ${(sc.times || []).join(" / ") || "未设时刻"}` : "调度 已关闭";
  scP.className = "pill " + (sc.enabled ? "ok" : "warn");
  scP.title = sc.power_note || "";

  const rs = s.run_state || {};
  RUNNING = !!rs.running;
  const dot = $("dot"), btn = $("btn-run");
  dot.className = "dot" + (RUNNING ? " busy" : "");
  btn.disabled = RUNNING;
  btn.innerHTML = RUNNING ? '<span class="spinner"></span> 采集中…' : "立即采集";

  const c = s.repo_stats || {};
  $("c-pool").textContent = c.total ? ` (${c.total})` : "";
}

/* ---------------- 今日榜单 ---------------- */
function ring(score) {
  const v = Math.max(0, Math.min(1, Number(score) || 0));
  const r = 26, C = 2 * Math.PI * r;
  const color = v >= 0.7 ? "#2ecc8f" : v >= 0.5 ? "#4f8cff" : v >= 0.3 ? "#f5a623" : "#6b7688";
  return `<div class="ring">
    <svg width="62" height="62" viewBox="0 0 62 62">
      <circle cx="31" cy="31" r="${r}" fill="none" stroke="#0b0d11" stroke-width="5"/>
      <circle cx="31" cy="31" r="${r}" fill="none" stroke="${color}" stroke-width="5"
              stroke-linecap="round" stroke-dasharray="${C.toFixed(1)}"
              stroke-dashoffset="${(C * (1 - v)).toFixed(1)}"/>
    </svg>
    <div class="val">${(v * 100).toFixed(0)}<small>综合分</small></div>
  </div>`;
}

function bar(label, value, cls) {
  const v = Math.max(0, Math.min(1, Number(value) || 0));
  return `<div class="metric">
    <div class="k">${label}</div>
    <div class="v">${(v * 100).toFixed(0)}%</div>
    <div class="bar ${cls || ""}"><i style="width:${(v * 100).toFixed(0)}%"></i></div>
  </div>`;
}

function repoCard(it) {
  const mom = Number(it.momentum || 0);
  const window = it.momentum_window ? `/${it.momentum_window}天` : "";
  const est = it.momentum_estimated ? `<span class="tag" title="快照历史还不够长，这是按仓库年龄估算的终身平均增速">估算</span>` : "";
  const pen = Object.keys(it.penalties || {});
  const tags = [];
  if (it.is_new) tags.push(`<span class="tag new">今日新上榜</span>`);
  if (it.language) tags.push(`<span class="tag lang">${esc(it.language)}</span>`);
  if (it.has_cjk_readme) tags.push(`<span class="tag" title="README 已含中文，信息差较小">已有中文</span>`);
  if (it.bing_hits === 0) tags.push(`<span class="tag new" title="Bing 上搜不到中文资料">中文零结果</span>`);
  pen.forEach((p) => tags.push(`<span class="tag">⚠ ${esc(p)}</span>`));

  const ai = [];
  if (it.ai_summary) ai.push(["🎯", "这工具干嘛的", it.ai_summary]);
  if (it.ai_audience) ai.push(["👥", "谁需要它", it.ai_audience]);
  if (it.ai_deploy) ai.push(["🔧", "部署要点", it.ai_deploy]);
  if (it.ai_monetize) ai.push(["💰", "变现角度", it.ai_monetize]);
  if (it.ai_risk) ai.push(["⚠️", "风险提醒", it.ai_risk]);
  if (!ai.length) ai.push(["📄", "原始简介", it.description || "（无）"]);

  return `<div class="repo">
    <div class="repo-head">
      ${ring(it.score)}
      <div class="repo-title">
        <h3><a href="${esc(it.html_url)}" target="_blank" rel="noreferrer">${esc(it.full_name)}</a></h3>
        <div class="repo-meta">
          <span>⭐ ${(it.stars || 0).toLocaleString()}</span>
          <span>增速 +${mom.toFixed(1)}${window}${est}</span>
          <span>🍴 ${(it.forks || 0).toLocaleString()}</span>
          ${it.license ? `<span>${esc(it.license)}</span>` : ""}
        </div>
        <div class="repo-meta" style="margin-top:6px">${tags.join("")}</div>
      </div>
    </div>
    <div class="metrics">
      ${bar("中文稀缺度", it.cn_gap, "gold")}
      ${bar("可部署性", it.deployability, "ok")}
      ${bar("增速得分", it.detail ? it.detail.momentum_norm : 0)}
    </div>
    <div class="ai-block">
      ${ai.map(([ico, k, v]) => `<div class="ai-row"><span class="ico">${ico}</span><span class="txt"><b>${k}</b>${esc(v)}</span></div>`).join("")}
    </div>
    <div class="repo-foot">
      ${it.ai_model ? `<span class="tag" title="生成这张卡的模型">${esc(it.ai_model)}</span>` : ""}
      <div class="spacer"></div>
      <button class="btn sm ghost" onclick="openRepo(${it.repo_id})">详情</button>
      <button class="btn sm ghost" onclick="reEnrich(${it.repo_id})">重生成选题卡</button>
      <button class="btn sm ghost" onclick="setStatus(${it.repo_id},'tracking')">跟进</button>
      <button class="btn sm ghost" onclick="setStatus(${it.repo_id},'rejected')">忽略</button>
    </div>
  </div>`;
}

async function loadDigest() {
  let d;
  try { d = await api("/api/digest/latest"); } catch (e) {
    $("digest-body").innerHTML = `<div class="notice bad">读取失败：${esc(e.message)}</div>`; return;
  }
  if (d.empty) {
    $("digest-body").innerHTML = `<div class="empty">
      <h3>还没有榜单</h3>
      <p>点右上角「立即采集」跑第一次。首次运行大约需要 2–5 分钟。</p>
      <p class="small">第一次跑完，系统才会开始记录星标快照；从第二次起，增速就是真实增量了。</p>
    </div>`;
    $("c-digest").textContent = "";
    return;
  }

  const items = d.payload.items || [];
  const stats = d.payload.stats || {};
  $("c-digest").textContent = ` (${items.length})`;

  const notices = [];
  notices.push(`<div class="notice ok"><b>榜单已生成</b> —— ${esc(d.day)}，共 ${items.length} 个机会。
    ${stats.enriched ? `其中 ${stats.enriched} 个已生成 AI 选题卡。` : ""}
    ${d.payload.generated_at ? `<span class="muted small">生成于 ${esc(d.payload.generated_at.replace("T", " "))}</span>` : ""}</div>`);

  const llm = stats.llm || {};
  if (llm.effective === "rule" || llm.effective === "off") {
    notices.push(`<div class="notice warn"><b>AI 引擎未启用</b> —— ${esc(llm.summary || "")}
      选题卡目前是规则模板生成的（只有部署信号，没有挖掘出的变现角度）。
      去「设置」里配置 <code>DEEPSEEK_API_KEY</code>，或在本机执行 <code>ollama pull qwen3:8b</code>，即可解锁完整分析。</div>`);
  }
  if (stats.github_authed === false) {
    notices.push(`<div class="notice warn"><b>GitHub 未认证</b> —— 核心 API 只有 60 次/小时，
      本轮只能抓少量 README，中文稀缺度判断和部署分析会因此打折。
      去 <a href="https://github.com/settings/tokens" target="_blank" rel="noreferrer">github.com/settings/tokens</a>
      建一个免费 token，填进 <code>radar/.env</code> 的 <code>GITHUB_TOKEN=</code> 即可。</div>`);
  }
  if (stats.deferred) {
    notices.push(`<div class="notice warn"><b>本次采集被电源守卫跳过</b> —— ${esc((stats.notes || []).join("；"))}
      插上电源后点「立即采集」，或等下一次定时任务。</div>`);
  }

  const notes = stats.notes || [];
  if (notes.length) {
    notices.push(`<details class="notes"><summary>本轮 ${notes.length} 条运行提示</summary>
      <ul>${notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></details>`);
  }

  const unenriched = items.filter((i) => !i.ai_summary).length;
  const aiBadge = stats.enrich_models && stats.enrich_models.length
    ? `<span class="tag">${esc(stats.enrich_models.join(", "))}</span>` : "";

  $("digest-notices").innerHTML = notices.join("");
  $("digest-body").innerHTML = `
    <div class="section-title">机会雷达 ${aiBadge}<div class="line"></div></div>
    ${unenriched === items.length && items.length
      ? `<div class="notice">本轮没有条目完成 AI 加工（可能是 AI 未启用，或得分未达门槛）。下方仍按综合分排序展示。` +
        ` <a href="#" onclick="runNow();return false">重新采集</a></div>` : ""}
    <div class="cards">${items.map(repoCard).join("")}</div>`;
}

/* ---------------- 候选池 ---------------- */
let poolTimer = null;
function debounceLoadPool() { clearTimeout(poolTimer); poolTimer = setTimeout(loadPool, 350); }

async function loadPool() {
  const q = $("pool-q").value.trim();
  const status = $("pool-status").value;
  const order = $("pool-order").value;
  const min = $("pool-min").checked ? 0.5 : "";
  let d;
  try {
    d = await api(`/api/repos?status=${status}&q=${encodeURIComponent(q)}&order=${order}&limit=400${min ? "&min_score=" + min : ""}`);
  } catch (e) { $("pool-body").innerHTML = `<div class="notice bad">${esc(e.message)}</div>`; return; }

  const items = d.items || [];
  $("pool-count").textContent = `共 ${d.stats.total} 个仓库，当前显示 ${items.length} 条`;
  $("c-pool").textContent = ` (${d.stats.total})`;

  if (!items.length) {
    $("pool-body").innerHTML = `<div class="empty"><h3>没有匹配的仓库</h3><p>换个筛选条件，或点「立即采集」扩充候选池。</p></div>`;
    return;
  }

  const rows = items.map((r) => {
    const st = { candidate: "候选", tracking: "跟进中", starred: "收藏", rejected: "已淘汰" }[r.status] || r.status;
    return `<tr>
      <td><a href="${esc(r.html_url)}" target="_blank" rel="noreferrer"><b>${esc(r.full_name)}</b></a>
          <div class="muted small">${esc((r.ai_summary || r.description || "").slice(0, 90))}</div></td>
      <td class="num">${r.stars.toLocaleString()}</td>
      <td class="num">${Number(r.momentum || 0).toFixed(1)}</td>
      <td class="num">${(Number(r.cn_gap || 0) * 100).toFixed(0)}%</td>
      <td class="num">${(Number(r.deployability || 0) * 100).toFixed(0)}%</td>
      <td class="num"><b>${(Number(r.score || 0) * 100).toFixed(0)}</b></td>
      <td>${esc(r.language || "—")}</td>
      <td><span class="tag">${st}</span>${r.reject_reason ? `<div class="muted small">${esc(r.reject_reason)}</div>` : ""}</td>
      <td class="muted small">${esc((r.first_seen || "").slice(0, 10))}</td>
      <td><button class="btn sm ghost" onclick="openRepo(${r.repo_id})">详情</button></td>
    </tr>`;
  }).join("");

  $("pool-body").innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>仓库</th><th>星标</th><th>增速/天</th><th>中文稀缺</th><th>可部署</th><th>综合分</th>
    <th>语言</th><th>状态</th><th>首次发现</th><th></th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

/* ---------------- 运行记录 ---------------- */
async function loadRuns() {
  let d;
  try { d = await api("/api/runs?limit=30"); } catch (e) { return; }
  const items = d.items || [];
  if (!items.length) { $("runs-body").innerHTML = `<div class="empty">还没有运行记录</div>`; return; }
  $("runs-body").innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>#</th><th>开始</th><th>结束</th><th>状态</th><th>发现</th><th>刷新</th>
    <th>README</th><th>进池</th><th>淘汰</th><th>AI 卡</th><th>推送</th><th>错误/提示</th></tr></thead>
    <tbody>${items.map((r) => {
      const s = r.stats || {};
      const cls = r.status === "ok" ? "ok" : r.status === "deferred" ? "warn" : "bad";
      const msg = r.error || (s.notes || []).slice(0, 2).join("；") || "";
      return `<tr>
        <td class="num">${r.id}</td>
        <td class="small">${esc((r.started_at || "").replace("T", " "))}</td>
        <td class="small muted">${esc((r.finished_at || "…").replace("T", " "))}</td>
        <td><span class="pill ${cls}">${esc(r.status || "")}</span></td>
        <td class="num">${s.discovered ?? "—"}</td>
        <td class="num">${s.refreshed ?? "—"}</td>
        <td class="num">${s.readmes ?? "—"}</td>
        <td class="num">${s.scored ?? "—"}</td>
        <td class="num">${s.rejected ?? "—"}</td>
        <td class="num">${s.enriched ?? "—"}</td>
        <td class="num">${s.notified ?? "—"}</td>
        <td class="muted small">${esc(String(msg).slice(0, 130))}</td>
      </tr>`;
    }).join("")}</tbody></table></div>`;
}

async function loadNotify() {
  let d;
  try { d = await api("/api/notifications?limit=40"); } catch (e) { return; }
  const items = d.items || [];
  if (!items.length) { $("notify-body").innerHTML = `<div class="empty">还没有推送记录</div>`; return; }
  $("notify-body").innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>时间</th><th>渠道</th><th>标题</th><th>结果</th><th>说明</th></tr></thead>
    <tbody>${items.map((n) => `<tr>
      <td class="small">${esc((n.created_at || "").replace("T", " "))}</td>
      <td><span class="tag">${esc(n.channel)}</span></td>
      <td class="small">${esc((n.title || "").slice(0, 70))}</td>
      <td>${n.status === "sent" ? '<span class="pill ok">已送达</span>' : '<span class="pill bad">失败</span>'}</td>
      <td class="muted small">${esc((n.error || "").slice(0, 110))}</td>
    </tr>`).join("")}</tbody></table></div>`;
}

/* ---------------- 设置 ---------------- */
let CFG = null;

function fNum(path, label, hint, step) {
  const v = getPath(CFG, path);
  return `<label class="field">${label}
    <input type="number" step="${step || "any"}" data-path="${path}" value="${v ?? ""}">
    ${hint ? `<span class="muted small">${hint}</span>` : ""}</label>`;
}
function fText(path, label, hint, type) {
  const v = getPath(CFG, path);
  return `<label class="field">${label}
    <input type="${type || "text"}" data-path="${path}" value="${esc(v ?? "")}">
    ${hint ? `<span class="muted small">${hint}</span>` : ""}</label>`;
}
function fCheck(path, label, hint) {
  const v = getPath(CFG, path);
  return `<label class="check"><input type="checkbox" data-path="${path}" ${v ? "checked" : ""}> ${label}</label>
    ${hint ? `<span class="muted small">${hint}</span>` : ""}`;
}
function fList(path, label, hint) {
  const v = getPath(CFG, path);
  const txt = Array.isArray(v) ? v.join(", ") : (v || "");
  return `<label class="field">${label}
    <textarea data-path="${path}" data-list="1" rows="2">${esc(txt)}</textarea>
    ${hint ? `<span class="muted small">${hint}</span>` : ""}</label>`;
}
function getPath(o, p) { return p.split(".").reduce((n, k) => (n == null ? undefined : n[k]), o); }

async function loadSettings() {
  try { CFG = await api("/api/config"); } catch (e) { toast("配置读取失败", "err"); return; }
  const s = CFG.secrets_present || {};

  $("settings-body").innerHTML = `
  <div class="card">
    <h4>筛选条件</h4>
    <div class="hint">决定什么样的仓库能进入候选池</div>
    <div class="fields">
      ${fNum("open_source.filters.min_stars", "星标下限", "太小可能是玩具项目")}
      ${fNum("open_source.filters.max_stars", "星标上限", "超过这个数，中文教程通常已泛滥")}
      ${fList("open_source.filters.exclude_keywords", "排除关键词（逗号分隔）", "命中任一即淘汰，小写匹配")}
      ${fNum("open_source.discover.new_repos.days", "发现窗口（天）", "只看最近这么多天创建的仓库")}
      ${fNum("open_source.discover.new_repos.min_stars", "发现阶段星标门槛", "低于此值不进池")}
      ${fCheck("open_source.discover.refresh_tracked", "回访刷新已跟踪仓库", "这是算出真实增速的关键，建议保持开启")}
    </div>
  </div>

  <div class="card">
    <h4>打分权重</h4>
    <div class="hint">三项子分相加应为 1.0，平台会自动归一化</div>
    <div class="fields">
      ${fNum("open_source.scoring.weight_momentum", "星标增速权重", "越高越看重「正在涨」的项目", "0.01")}
      ${fNum("open_source.scoring.weight_cn_gap", "中文稀缺度权重", "越高越看重「中文没人写过」", "0.01")}
      ${fNum("open_source.scoring.weight_deployability", "可部署性权重", "越高越看重「能真的跑起来」", "0.01")}
    </div>
  </div>

  <div class="card">
    <h4>中文稀缺度探测</h4>
    <div class="hint">判断"中文教程到底有没有人写过"</div>
    <div class="fields">
      ${fCheck("open_source.cn_gap.bing_probe", "用 Bing 探测中文结果数", "每次探测一次请求；关掉可加快采集")}
      ${fCheck("open_source.cn_gap.search_cn_ecosystem", "用 GitHub 搜索中文生态", "更准但更慢，会消耗搜索配额")}
    </div>
  </div>

  <div class="card">
    <h4>推送与交付</h4>
    <div class="fields">
      ${fNum("open_source.deliver.notify_min_score", "推送分数门槛", "0~1，只有高于它的新条目才推送", "0.01")}
      ${fNum("open_source.deliver.notify_top_n", "每次最多推送几条")}
      ${fCheck("open_source.deliver.daily_digest", "每天推送一份汇总日报")}
      ${fCheck("open_source.deliver.desktop_notify", "启用 Windows 桌面通知")}
    </div>
  </div>

  <div class="card">
    <h4>AI 引擎</h4>
    <div class="hint">当前：${esc((CFG.llm && CFG.llm.mode) || "hybrid")}｜密钥：DeepSeek ${s.DEEPSEEK_API_KEY ? "✅" : "❌"}</div>
    <div class="fields">
      <label class="field">模式
        <select data-path="llm.mode">
          <option value="hybrid" ${CFG.llm.mode === "hybrid" ? "selected" : ""}>混合（本地粗活 + API 精活，推荐）</option>
          <option value="local_only" ${CFG.llm.mode === "local_only" ? "selected" : ""}>仅本地 Ollama（零成本）</option>
          <option value="api_only" ${CFG.llm.mode === "api_only" ? "selected" : ""}>仅 DeepSeek API（质量最好）</option>
          <option value="off" ${CFG.llm.mode === "off" ? "selected" : ""}>关闭（纯规则模板）</option>
        </select>
      </label>
      ${fNum("open_source.enrich.top_n", "每次最多生成几张选题卡")}
      <span class="muted small">API Key 与 GitHub Token 请填在 <code class="inline">radar/.env</code>（看板不读取密码明文）</span>
    </div>
  </div>

  <div class="card">
    <h4>通知 · 企业微信</h4>
    <div class="hint">群里加一个"群机器人"，把 Webhook 地址粘进来</div>
    <div class="fields">
      ${fCheck("notify.wecom.enabled", "启用")}
      ${fText("notify.wecom.webhook", "Webhook 地址", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…")}
    </div>
  </div>

  <div class="card">
    <h4>通知 · 邮件</h4>
    <div class="fields">
      ${fCheck("notify.email.enabled", "启用")}
      ${fText("notify.email.smtp_host", "SMTP 服务器", "QQ 邮箱 smtp.qq.com / 163 smtp.163.com")}
      ${fNum("notify.email.smtp_port", "端口", "SSL 一般 465，STARTTLS 一般 587")}
      ${fCheck("notify.email.use_ssl", "使用 SSL")}
      ${fText("notify.email.username", "邮箱账号")}
      ${fText("notify.email.password", "SMTP 授权码", "不是登录密码！在邮箱设置里单独生成", "password")}
      ${fList("notify.email.to", "收件人（逗号分隔）")}
    </div>
  </div>

  <div class="card">
    <h4>通知 · Telegram</h4>
    <div class="hint">本机直连 Telegram 通常不通，需要自备代理</div>
    <div class="fields">
      ${fCheck("notify.telegram.enabled", "启用")}
      ${fText("notify.telegram.bot_token", "Bot Token")}
      ${fText("notify.telegram.chat_id", "Chat ID")}
      ${fText("notify.telegram.proxy", "代理地址", "形如 http://127.0.0.1:7890，留空则直连")}
    </div>
  </div>

  <div class="card">
    <h4>调度与省电</h4>
    <div class="hint">笔记本场景：没插电就自动跳过采集，插上电后补跑</div>
    <div class="fields">
      ${fCheck("schedule.enabled", "启用定时采集")}
      ${fList("schedule.times", "每天采集时刻（逗号分隔）", "例如 09:10, 21:10")}
      ${fCheck("schedule.power_guard.enabled", "启用电源守卫")}
      ${fCheck("schedule.power_guard.require_ac", "必须插电才采集", "强烈建议保持开启，保护你的电池")}
      ${fNum("schedule.power_guard.min_battery", "电量低于此值也跳过", "百分比")}
    </div>
  </div>`;
}

async function saveConfig() {
  const patch = {};
  document.querySelectorAll("#settings-body [data-path]").forEach((el) => {
    const path = el.dataset.path;
    if (el.type === "checkbox") patch[path] = el.checked;
    else if (el.dataset.list) {
      patch[path] = el.value.split(/[,，\n]/).map((x) => x.trim()).filter(Boolean)
        .map((x) => (/^-?\d+(\.\d+)?$/.test(x) ? Number(x) : x));
    } else if (el.type === "number") {
      const n = parseFloat(el.value);
      if (!isNaN(n)) patch[path] = n;
    } else if (el.value.trim() !== "") patch[path] = el.value.trim();
  });
  try {
    const r = await api("/api/config", { method: "POST", body: JSON.stringify(patch) });
    toast(r.ok ? `已保存 ${r.written.length} 项设置` : (r.message || "没有变化"), r.ok ? "ok" : "err");
    CFG = r.config;
    loadStatus();
  } catch (e) { toast("保存失败：" + e.message, "err"); }
}

async function resetConfig() {
  if (!confirm("恢复所有设置为 config.yaml 里的默认值？")) return;
  await api("/api/config/reset", { method: "POST" });
  toast("已恢复默认", "ok");
  loadSettings(); loadStatus();
}

/* ---------------- 详情抽屉 ---------------- */
async function openRepo(id) {
  $("drawer").classList.add("open");
  $("drawer-bg").classList.add("open");
  $("drawer").innerHTML = `<div class="empty"><span class="spinner"></span></div>`;
  let r;
  try { r = await api("/api/repo/" + id); } catch (e) { $("drawer").innerHTML = `<div class="notice bad">${esc(e.message)}</div>`; return; }

  const hist = (r.history || []).slice().reverse();
  const max = Math.max(1, ...hist.map((h) => h.stars));
  const min = Math.min(...hist.map((h) => h.stars), max);
  const span = Math.max(1, max - min);
  const bars = hist.slice(-40).map((h) =>
    `<i style="height:${(6 + 46 * (h.stars - min) / span).toFixed(1)}px" title="${esc(h.captured_at)} · ${h.stars} 星"></i>`).join("");

  const d = r.detail || {};
  const fmt = (v) => (v === null || v === undefined || v === "") ? "—" : v;

  $("drawer").innerHTML = `
    <button class="close" onclick="closeDrawer()">×</button>
    <h2><a href="${esc(r.html_url)}" target="_blank" rel="noreferrer">${esc(r.full_name)}</a></h2>
    <div class="repo-meta" style="margin-bottom:16px">
      <span class="tag">综合分 ${(Number(r.score || 0) * 100).toFixed(0)}</span>
      <span>⭐ ${(r.stars || 0).toLocaleString()}</span>
      <span>🍴 ${r.forks}</span>
      <span>${esc(r.language || "—")}</span>
      ${r.license ? `<span>${esc(r.license)}</span>` : ""}
    </div>

    <div class="section-title">AI 选题卡<div class="line"></div></div>
    ${r.ai_summary ? `<div class="ai-block">
      ${[["🎯", "这工具干嘛的", r.ai_summary], ["👥", "谁需要它", r.ai_audience],
         ["🔧", "部署要点", r.ai_deploy], ["💰", "变现角度", r.ai_monetize],
         ["⚠️", "风险提醒", r.ai_risk]]
        .filter((x) => x[2])
        .map(([i, k, v]) => `<div class="ai-row"><span class="ico">${i}</span><span class="txt"><b>${k}</b>${esc(v)}</span></div>`).join("")}
      <div class="muted small" style="margin-top:6px">生成模型：${esc(r.ai_model || "—")} · ${esc((r.ai_at || "").replace("T", " "))}</div>
    </div>` : `<div class="notice warn">这张卡还没生成。点下面的「用 AI 重新生成」即可（会走精活通道，优先用 API）。</div>`}

    <div class="section-title">星标历史（${hist.length} 个快照）<div class="line"></div></div>
    ${hist.length > 1 ? `<div class="spark">${bars}</div>
      <div class="muted small" style="margin-top:6px">左旧右新 · 从 ${esc((hist[0] || {}).captured_at || "")} 到 ${esc((hist[hist.length - 1] || {}).captured_at || "")}</div>`
      : `<div class="notice">目前只有 1 个快照。再采集一两次之后，这里就会出现真实的星标增长曲线。</div>`}

    <div class="section-title">打分明细<div class="line"></div></div>
    <div class="kv">
      <div class="k">增速</div><div>${Number(d.momentum || 0).toFixed(2)} 星/天
        ${d.momentum_window ? `（${d.momentum_window} 天窗口）` : (d.estimate_kind === "lifetime" ? "（按仓库年龄估算）" : "")}</div>
      <div class="k">窗口内增量</div><div>+${Number(d.momentum_delta || 0).toFixed(0)} 星</div>
      <div class="k">加速度</div><div>${Number(d.acceleration || 0).toFixed(2)} 星/天²</div>
      <div class="k">中文稀缺度</div><div>${(Number(d.cn_gap || 0) * 100).toFixed(1)}%</div>
      <div class="k">可部署性</div><div>${(Number(d.deployability || 0) * 100).toFixed(1)}%</div>
      <div class="k">Bing 中文结果</div><div>${fmt(d.bing_hits)}</div>
      <div class="k">README 含中文</div><div>${d.has_cjk_readme ? "是" : "否"}</div>
      <div class="k">中文 README 变体</div><div>${(d.readme_variants || []).length ? esc((d.readme_variants || []).join(", ")) : "无"}</div>
      <div class="k">扣分项</div><div>${Object.keys(d.penalties || {}).length
        ? Object.entries(d.penalties).map(([k, v]) => `${esc(k)} −${v}`).join("；") : "无"}</div>
      <div class="k">部署信号</div><div>${Object.entries(d.readme_signals || {}).filter(([, v]) => v).map(([k]) => esc(k)).join(", ") || "无"}</div>
      <div class="k">创建时间</div><div>${esc((r.created_at || "").slice(0, 10))}</div>
      <div class="k">最近推送</div><div>${esc((r.pushed_at || "").slice(0, 10))}</div>
      <div class="k">首次发现</div><div>${esc((r.first_seen || "").replace("T", " "))}</div>
      <div class="k">Topics</div><div>${(r.topics || []).map((t) => `<span class="tag">${esc(t)}</span>`).join(" ") || "—"}</div>
      ${r.homepage ? `<div class="k">主页</div><div><a href="${esc(r.homepage)}" target="_blank" rel="noreferrer">${esc(r.homepage)}</a></div>` : ""}
    </div>

    <div class="section-title">README 摘录<div class="line"></div></div>
    <pre class="readme">${esc(r.readme_excerpt || "（尚未抓取到 README —— 多半是 GitHub API 配额用尽，配 token 后重新采集即可）")}</pre>

    <div class="section-title">操作<div class="line"></div></div>
    <div class="repo-foot">
      <button class="btn sm" onclick="reEnrich(${r.repo_id})">用 AI 重新生成</button>
      <button class="btn sm" onclick="setStatus(${r.repo_id},'tracking')">标记跟进中</button>
      <button class="btn sm" onclick="setStatus(${r.repo_id},'starred')">收藏</button>
      <button class="btn sm danger" onclick="setStatus(${r.repo_id},'rejected')">淘汰</button>
      <a class="btn sm ghost" href="${esc(r.html_url)}" target="_blank" rel="noreferrer">打开仓库 ↗</a>
    </div>`;
}

function closeDrawer() {
  $("drawer").classList.remove("open");
  $("drawer-bg").classList.remove("open");
}

async function setStatus(id, status) {
  try {
    await api(`/api/repo/${id}/status`, { method: "POST", body: JSON.stringify({ status, reason: "" }) });
    toast("已更新为「" + ({ tracking: "跟进中", starred: "收藏", rejected: "淘汰", candidate: "候选" }[status] || status) + "」", "ok");
    closeDrawer();
    loadDigest(); loadPool();
  } catch (e) { toast("操作失败：" + e.message, "err"); }
}

async function reEnrich(id) {
  toast("正在生成选题卡，请稍候…");
  try {
    const r = await api(`/api/repo/${id}/enrich`, { method: "POST" });
    toast(`已用 ${r.model} 重新生成`, "ok");
    openRepo(id); loadDigest();
  } catch (e) { toast("生成失败：" + e.message, "err"); }
}

/* ---------------- 动作 ---------------- */
async function runNow() {
  if (RUNNING) return;
  try {
    const r = await api("/api/run?respect_power=false&trigger=dashboard", { method: "POST" });
    if (r.busy) { toast("已经有一次采集在跑"); return; }
    toast("采集已开始，预计 2–5 分钟…", "ok");
    loadStatus();
    const timer = setInterval(async () => {
      await loadStatus();
      if (!RUNNING) {
        clearInterval(timer);
        toast("采集完成", "ok");
        loadDigest(); loadPool();
      }
    }, 3500);
  } catch (e) { toast("启动失败：" + e.message, "err"); }
}

async function testNotify() {
  toast("正在发送测试通知…");
  try {
    const r = await api("/api/notify/test", { method: "POST", body: JSON.stringify({}) });
    const res = r.report.results || {};
    const lines = Object.entries(res).map(([k, v]) => `${k}: ${v.ok ? "✅" : "❌ " + v.detail}`);
    toast(lines.join(" ｜ ") || "没有启用的渠道", "ok");
    if ($("view-runs").classList.contains("active")) loadNotify();
  } catch (e) { toast("发送失败：" + e.message, "err"); }
}

async function runDiagnose() {
  toast("正在体检…");
  try {
    const d = await api("/api/diagnose");
    const rows = Object.entries(d.checks).map(([k, v]) =>
      `<tr><td>${esc(k)}</td><td>${v.ok ? '<span class="pill ok">正常</span>' : '<span class="pill warn">异常</span>'}</td>
       <td class="muted small">${esc(v.detail || "")}</td></tr>`).join("");
    $("drawer").classList.add("open"); $("drawer-bg").classList.add("open");
    $("drawer").innerHTML = `<button class="close" onclick="closeDrawer()">×</button>
      <h2>系统体检</h2>
      <div class="muted small" style="margin-bottom:14px">${esc(d.time.replace("T", " "))} ·
        总体 ${d.ok ? '<span class="pill ok">全部正常</span>' : '<span class="pill warn">有项目需处理</span>'}</div>
      <div class="table-wrap"><table><thead><tr><th>检查项</th><th>结果</th><th>说明</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
      <div class="section-title">怎么把 ❌ 变成 ✅<div class="line"></div></div>
      <div class="notice">
        <b>1. GitHub Token（最重要）</b><br>
        打开 <a href="https://github.com/settings/tokens" target="_blank" rel="noreferrer">github.com/settings/tokens</a>
        → Fine-grained token → 权限只勾 <code class="inline">Public Repositories (read)</code> → 生成。
        把 <code class="inline">GITHUB_TOKEN=ghp_xxx</code> 写进 <code class="inline">radar/.env</code>。免费。<br><br>
        <b>2. DeepSeek API Key（AI 精活）</b><br>
        在 platform.deepseek.com 建 Key，把 <code class="inline">DEEPSEEK_API_KEY=sk-xxx</code> 写进 <code class="inline">radar/.env</code>。
        每天几毛钱，是"混合模式"的精活通道。<br><br>
        <b>3. 本地 Ollama（零成本 AI）</b><br>
        命令行执行 <code class="inline">ollama pull qwen3:8b</code>（约 5GB，只需一次）。
        之后翻译/摘要走本地，永久免费。<br><br>
        改完 .env 后，重启服务（或点「体检」重新探测）即生效。
      </div>`;
  } catch (e) { toast("体检失败：" + e.message, "err"); }
}

/* ---------------- 上手引导 ---------------- */
function renderGuide() {
  $("guide-body").innerHTML = `
  <div class="notice ok">
    <b>这套平台是怎么帮你赚钱的？</b><br>
    它把「刷 Trending → 判断值不值得做 → 写教程 → 上架卖」这条链路里
    <b>最耗时间的前 70%</b> 自动化了：发现机会、判断中文稀缺度、评估部署难度、生成选题卡。
    剩下的 30%（真跑一遍、补上踩坑经验、上架收款）必须你本人做 —— 那恰好也是买家愿意付钱的部分。
  </div>

  <div class="section-title">日常怎么用（每天不到 5 分钟）<div class="line"></div></div>
  <div class="table-wrap"><table>
    <thead><tr><th>步骤</th><th>你要做什么</th><th>耗时</th></tr></thead>
    <tbody>
      <tr><td>1</td><td>平台按 <code class="inline">schedule.times</code> 自动采集（默认 09:10 / 21:10）。
        不插电时自动跳过，插上电后自动补跑。</td><td>0 分钟</td></tr>
      <tr><td>2</td><td>打开看板看「今日榜单」。综合分 70+ 的优先看。</td><td>1 分钟</td></tr>
      <tr><td>3</td><td>挑 1 个项目，读它的 AI 选题卡（这工具干嘛的 / 谁需要 / 怎么部署 / 怎么变现 / 有什么坑）。</td><td>2 分钟</td></tr>
      <tr><td>4</td><td>决定跟进的点「跟进」，不做的点「忽略」——你的反馈会让候选池越来越准。</td><td>30 秒</td></tr>
      <tr><td>5</td><td>对决定要做的那个：本地真跑一遍，把卡住的地方记下来。</td><td>这一步只有你能做</td></tr>
    </tbody>
  </table></div>

  <div class="section-title">三个数字怎么读<div class="line"></div></div>
  <div class="cards">
    <div class="card">
      <h4>增速（星/天）</h4>
      <p class="muted small">系统自己存快照算出来的<b>真实日增星数</b>，不是 Trending 页面的排名。
      第一二次运行时历史不足，会用「总星数 ÷ 仓库年龄」估算并标注<b>估算</b>。跑满一周后就是真值。</p>
    </div>
    <div class="card">
      <h4>中文稀缺度（0–100%）</h4>
      <p class="muted small">越高说明中文资料越少、信息差越大。系统看三件事：
      README 里有没有中文、有没有中文 README 变体、Bing 上能搜到几篇中文教程。<b>这是你生意的核心指标。</b></p>
    </div>
    <div class="card">
      <h4>可部署性（0–100%）</h4>
      <p class="muted small">越高说明越容易跑起来，越适合做「一键部署」教程。
      系统看有没有 Docker、一键脚本、安装包、包管理器一行装、Web 界面、快速开始章节。</p>
    </div>
  </div>

  <div class="section-title">合规边界（别踩）<div class="line"></div></div>
  <div class="notice warn">
    <b>可以做：</b>把公开的开源工具装到用户自己的机器上，配上你实测过的踩坑解决方案。<br>
    <b>不要做：</b>① 逐句机翻 README 直接卖（侵权 + 无增量价值）；
    ② 承诺「包教包会 / 零失败 / 躺赚」；
    ③ 引导用户翻墙、绕过限制、免费白嫖付费 API —— 平台会下架，还可能有更麻烦的事。<br>
    平台已经把这些写进了 AI 的提示词（明确要求不许画大饼、要写清不适合谁），但最终发布前请你自己再通读一遍。
  </div>

  <div class="section-title">下一期规划<div class="line"></div></div>
  <div class="notice">
    这套底座（采集 → 归一 → 过滤 → AI 加工 → 交付 → 推送）是通用的。你选定的另外两条管道会直接复用：<br><br>
    <b>② 标讯雷达</b> —— 中国政府采购网 / 公共资源交易平台 → 关键词+地区+预算过滤 → AI 提取项目/采购人/预算/截止/资质 → 高匹配即时推送。
    已验证 <code class="inline">search.ccgp.gov.cn</code> 本机可达。<br><br>
    <b>③ 领域情报翻译</b> —— RSS/arXiv/行业站点 → AI 翻译+术语表+摘要 → 双语日报。<br><br>
    复用同一套数据库、看板、通知、调度，只新增一个 <code class="inline">sources/xxx.py</code>。
    等开源雷达跑稳，说一声就接着做。
  </div>`;
}

/* ---------------- 启动 ---------------- */
(async function init() {
  await loadStatus();
  await loadDigest();
  setInterval(loadStatus, 20000);
})();
