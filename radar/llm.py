"""混合 AI 路由。

* ``bulk``   量大便宜的活（摘要、翻译、初筛）—— 优先本地 Ollama
* ``polish`` 量小要质量的活（最终商机解读、文案）—— 优先 DeepSeek API

任一档不可用会自动降级到另一档；两档都不可用则返回 ``None``，
由调用方回退到纯规则模板 —— **AI 是增强，不是依赖**。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import db
from .config import cfg

log = logging.getLogger("radar.llm")


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    def json(self) -> Any:
        return extract_json(self.text)


def extract_json(text: str) -> Any:
    """从模型输出里稳健地抠出 JSON（兼容 ```json 包裹与前后废话）。"""
    if not text:
        return None
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    # 退一步：找第一个 { 到最后一个 }
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except Exception:
            pass
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start >= 0 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except Exception:
            pass
    return None


class LLMRouter:
    def __init__(self) -> None:
        self._probe_cache: dict[str, Any] | None = None
        self._probe_at: float = 0.0

    # ------------------------------------------------------------------
    #  可用性探测
    # ------------------------------------------------------------------
    def probe(self, use_cache: bool = False, ttl: float = 60.0) -> dict[str, Any]:
        """探测各引擎可用性。

        带 TTL 缓存：看板每 20 秒轮询一次状态，如果每次都去连 Ollama（超时上限 5 秒），
        状态接口就会慢到被前端判超时。60 秒内直接复用上次结果。
        """
        import time as _time

        now = _time.time()
        if self._probe_cache is not None and (use_cache or (now - self._probe_at) < ttl):
            return self._probe_cache
        out: dict[str, Any] = {"mode": cfg.get("llm.mode", "hybrid"), "local": {}, "api": {}}

        # --- 本地 Ollama ---
        local_cfg = cfg.get("llm.local", {}) or {}
        want_model = str(local_cfg.get("model", ""))
        if not local_cfg.get("enabled", True):
            out["local"] = {"ok": False, "reason": "配置里已关闭"}
        else:
            try:
                with httpx.Client(timeout=5) as cli:
                    r = cli.get(f"{local_cfg['base_url'].rstrip('/')}/api/tags")
                    r.raise_for_status()
                    entries = r.json().get("models") or []
                # 保留完整信息，用于"配置的模型没有，但同族其它规格有"时挑一个能用的
                catalog = [
                    {"name": str(m.get("name") or ""), "size": int(m.get("size") or 0)}
                    for m in entries if m.get("name")
                ]
                models = [m["name"] for m in catalog]
                chosen = self._pick_local_model(want_model, catalog)
                out["local"] = {
                    "ok": bool(chosen),
                    "available_models": models,
                    "want": want_model,
                    "model": chosen or want_model,
                    "substituted": bool(chosen and chosen != want_model),
                    "reason": self._local_reason(want_model, chosen, models),
                }
            except Exception as exc:
                out["local"] = {"ok": False, "reason": f"连不上 Ollama ({exc})"}

        # --- API（任意 OpenAI 兼容服务商）---
        api_cfg = cfg.get("llm.api", {}) or {}
        key = self._api_key(api_cfg)
        key_name = str(api_cfg.get("key_env") or "").strip() or "LLM_API_KEY / DEEPSEEK_API_KEY"
        if not api_cfg.get("enabled", True):
            out["api"] = {"ok": False, "reason": "配置里已关闭"}
        elif not key:
            out["api"] = {"ok": False, "reason": f"未设置 {key_name}（填 .env 即可启用）"}
        else:
            spent = db.api_cost_today()
            limit = float((cfg.get("llm.cost_guard", {}) or {}).get("daily_limit_cny", 3.0) or 0)
            if limit and spent >= limit:
                out["api"] = {"ok": False, "reason": f"今日 API 花费 ¥{spent:.2f} 已达上限 ¥{limit:.2f}"}
            else:
                out["api"] = {"ok": True, "reason": f"就绪（今日已花 ¥{spent:.2f}）"}

        # --- 结论 ---
        mode = out["mode"]
        any_ok = out["local"].get("ok") or out["api"].get("ok")
        if mode == "off":
            out["effective"] = "off"
            out["summary"] = "AI 已关闭，全部使用规则模板"
        elif not any_ok:
            out["effective"] = "rule"
            out["summary"] = "两个 AI 引擎都不可用 → 退化为规则模板（平台照常工作）"
        else:
            usable = []
            if out["local"].get("ok"):
                usable.append("本地 Ollama")
            if out["api"].get("ok"):
                usable.append("DeepSeek API")
            out["effective"] = "hybrid" if len(usable) == 2 else ("local" if out["local"].get("ok") else "api")
            out["summary"] = "可用：" + " + ".join(usable)
        self._probe_cache = out
        self._probe_at = now
        return out

    def invalidate(self) -> None:
        self._probe_cache = None

    # ------------------------------------------------------------------
    #  本地模型选择
    # ------------------------------------------------------------------
    @staticmethod
    def _pick_local_model(want: str, catalog: list[dict[str, Any]]) -> str | None:
        """挑一个真正能用的本地模型。

        配置里写 ``qwen3:8b`` 但用户只 pull 了 ``qwen3:0.6b`` 时，
        旧的判断会因为"同族名匹配"而误报可用，真调用却 404。
        这里改成：精确匹配优先；没有就退到同族里**体积最大**的那个（质量最好），
        并在状态里标注"已替换"。
        """
        if not catalog:
            return None
        names = [c["name"] for c in catalog]
        if want in names:
            return want
        family = want.split(":")[0]
        same = [c for c in catalog if c["name"].split(":")[0] == family]
        if same:
            same.sort(key=lambda c: c["size"], reverse=True)
            return same[0]["name"]
        return None

    @staticmethod
    def _local_reason(want: str, chosen: str | None, models: list[str]) -> str:
        if not chosen:
            return (f"Ollama 在跑，但没有模型 {want}；现有 {models or '无'}。"
                    f"执行 ollama pull {want} 即可启用")
        if chosen != want:
            return f"配置的是 {want}，实际使用 {chosen}（同族里体积最大的可用模型）"
        return "就绪"

    # ------------------------------------------------------------------
    #  路由
    # ------------------------------------------------------------------
    def _chain(self, task: str) -> list[str]:
        mode = cfg.get("llm.mode", "hybrid")
        if mode == "off":
            return []
        if mode == "local_only":
            return ["local"]
        if mode == "api_only":
            return ["api"]
        # hybrid：默认 API 优先。
        # 早先写成"批量任务本地优先"，前提是本地免费、API 按量收费。
        # 实测 qwen3:8b 纯 CPU 约 1.3 token/秒，一张卡要 2~3 分钟且经常撞超时，
        # 而 API 只要 3.6 秒 —— 先跑本地的唯一后果是整轮从 3 分钟变成 20 分钟。
        # 想省钱可以把 llm.prefer 改回 local。
        if str(cfg.get("llm.prefer", "api")).lower() == "local":
            return ["local", "api"]
        return ["api", "local"]

    def available(self, task: str = "bulk") -> list[str]:
        probe = self.probe(use_cache=True)
        return [p for p in self._chain(task) if probe.get(p, {}).get("ok")]

    # ------------------------------------------------------------------
    #  调用
    # ------------------------------------------------------------------
    def chat(self, system: str, user: str, task: str = "bulk",
             json_mode: bool = False, max_tokens: int = 1400,
             temperature: float = 0.3) -> LLMResult | None:
        chain = self.available(task)
        if not chain:
            return None
        last_err = ""
        for provider in chain:
            try:
                if provider == "local":
                    return self._call_local(system, user, task, json_mode, max_tokens, temperature)
                return self._call_api(system, user, task, json_mode, max_tokens, temperature)
            except Exception as exc:
                last_err = f"{provider}: {exc}"
                log.warning("LLM %s 调用失败，尝试下一个: %s", provider, exc)
                continue
        log.error("所有 AI 引擎都失败: %s", last_err)
        return None

    # --- 本地 ---------------------------------------------------------
    def _call_local(self, system: str, user: str, task: str, json_mode: bool,
                    max_tokens: int, temperature: float) -> LLMResult:
        c = cfg.get("llm.local", {}) or {}
        # 用探测结果里"实际可用"的模型，而不是配置里那个可能不存在的
        model = str(self.probe(use_cache=True).get("local", {}).get("model")
                    or c.get("model", "qwen3:8b"))
        url = f"{str(c['base_url']).rstrip('/')}/api/chat"
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if json_mode:
            payload["format"] = "json"

        # qwen3 / deepseek-r1 这类"思考模型"默认会把整个 token 预算耗在思考上，
        # 导致 message.content 为空、done_reason=length —— 实测 120 token 全被思考吃掉。
        # 关掉思考后同样的问题 18 token 就答完了。
        want_no_think = bool(c.get("disable_thinking", True))

        def _post(pl: dict[str, Any]) -> dict[str, Any]:
            with httpx.Client(timeout=float(c.get("timeout", 180))) as cli:
                resp = cli.post(url, json=pl)
                resp.raise_for_status()
                return resp.json()

        if want_no_think:
            payload["think"] = False
        try:
            data = _post(payload)
        except httpx.HTTPStatusError as exc:
            # 老版本 Ollama / 非思考模型不认 think 参数，去掉重试一次
            if want_no_think and exc.response is not None and exc.response.status_code < 500:
                payload.pop("think", None)
                data = _post(payload)
            else:
                raise

        msg = data.get("message") or {}
        text = (msg.get("content") or "").strip()
        thinking = (msg.get("thinking") or "").strip()
        done_reason = data.get("done_reason") or ""

        # 兜底：个别版本会把结果写在 thinking 里，或者 content 被截断
        if not text and thinking:
            log.warning("本地模型 content 为空，改用 thinking 字段（模型 %s）", model)
            text = thinking
        if not text:
            log.warning("本地模型返回空内容（model=%s done_reason=%s，"
                        "多半是思考模式吃光了 token 预算，可在 config 里调大 "
                        "llm.local.max_tokens 或设 disable_thinking: true）", model, done_reason)
        elif done_reason == "length":
            log.warning("本地模型输出被截断（model=%s），考虑调大 num_predict", model)

        pin = int(data.get("prompt_eval_count") or 0)
        pout = int(data.get("eval_count") or 0)
        db.log_llm("local", model, task, pin, pout, 0.0, bool(text), None)
        return LLMResult(text=text, provider="local", model=model,
                         prompt_tokens=pin, completion_tokens=pout, cost=0.0, raw=data)

    # --- API ----------------------------------------------------------
    @staticmethod
    def _api_key(c: dict[str, Any]) -> str:
        """取 API 密钥。

        支持任意 OpenAI 兼容服务商（DeepSeek / 并行智算云 / 硅基流动 …），
        所以不再写死 DEEPSEEK_API_KEY：
        - 配置里 ``key_env`` 指定环境变量名时用它；
        - 否则依次尝试 LLM_API_KEY、DEEPSEEK_API_KEY。
        """
        name = str(c.get("key_env") or "").strip()
        if name:
            return cfg.secret(name)
        return cfg.secret("LLM_API_KEY") or cfg.secret("DEEPSEEK_API_KEY")

    def _call_api(self, system: str, user: str, task: str, json_mode: bool,
                  max_tokens: int, temperature: float) -> LLMResult:
        c = cfg.get("llm.api", {}) or {}
        key = self._api_key(c)
        if not key:
            name = str(c.get("key_env") or "").strip() or "LLM_API_KEY"
            raise RuntimeError(f"缺少 API 密钥（在 .env 里设置 {name}）")
        url = f"{str(c['base_url']).rstrip('/')}/v1/chat/completions"
        payload: dict[str, Any] = {
            "model": c.get("model", "deepseek-chat"),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        timeout = float(c.get("timeout", 120))
        retries = int(c.get("retries", 2))

        data: dict[str, Any] = {}
        last_err = ""
        for attempt in range(retries + 1):
            try:
                with httpx.Client(timeout=timeout) as cli:
                    r = cli.post(url, json=payload, headers=headers)
                if r.status_code == 400 and "response_format" in payload:
                    # 不是所有 OpenAI 兼容服务商都支持 response_format。
                    # 去掉它重试一次，而不是整张卡降级成规则模板。
                    log.warning("服务商拒绝 response_format，去掉后重试: %s", r.text[:160])
                    payload.pop("response_format", None)
                    continue
                if r.status_code >= 500 or r.status_code == 429:
                    last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                    if attempt < retries:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    raise RuntimeError(last_err)
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
                data = r.json()
                break
            except RuntimeError:
                raise
            except Exception as exc:
                # 实测该服务商会直接重置连接（WinError 10054），必须重试
                last_err = f"{type(exc).__name__}: {exc}"
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"API 调用失败（重试 {retries} 次后）: {last_err}") from exc
        if not data:
            raise RuntimeError(f"API 调用失败: {last_err or '空响应'}")
        text = (((data.get("choices") or [{}])[0].get("message")) or {}).get("content", "") or ""
        usage = data.get("usage") or {}
        pin = int(usage.get("prompt_tokens") or 0)
        pout = int(usage.get("completion_tokens") or 0)
        cost = pin / 1e6 * float(c.get("price_in", 2.0)) + pout / 1e6 * float(c.get("price_out", 8.0))
        db.log_llm("api", str(c.get("model")), task, pin, pout, cost, True, None)
        return LLMResult(text=text, provider="api", model=str(c.get("model")),
                         prompt_tokens=pin, completion_tokens=pout, cost=cost, raw=data)


router = LLMRouter()
