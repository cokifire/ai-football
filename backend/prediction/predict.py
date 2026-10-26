"""
推理入口:给定 fixture_id,执行完整预测流程并写入 predictions 表.

流程:
  1. 从 DB 读取比赛信息
  2. 提取特征 → XGBoost 推理(λ, Top3, 参考概率)
  3. 赔率(API-Football 实时; 失败时回退到 Flashscore 爬取)
  4. LLM(全量数据 + XGBoost 参考) 最终决策
  5. 写入 predictions 表
"""

import os
import sys
import json
import pickle
import re
import time
from datetime import datetime
from typing import Any

import numpy as np
from scipy.stats import poisson
from loguru import logger

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from sqlalchemy import text
from app.db.session import SessionLocal
from app.core.config import settings
from llm.codex_cli import PREDICTION_SCHEMA_PATH, CodexError, run_codex
from prediction.features import extract_features_for_fixture
from prediction.training.model import load_models, _fill_na
from prediction.training.data import assign_group
from prediction.bayes import (
    BAYES_VERSION, apply_updates, blend_prior, market_divergence, team_elo_prior,
)

import pandas as pd

MODELS_DIR = os.path.join(os.path.dirname(__file__), 'models')
FEATURE_COLS_PATH = os.path.join(MODELS_DIR, 'feature_cols.pkl')


# ── API-Football 请求节流 ──
# 统一走 app.core.api_football 的进程级共享限流器：预测路径与赛程同步路径
# 必须共用同一份 10 次/分钟预算，否则两者叠加会把服务端推到 ~19 次/分钟。
from app.core.api_football import (
    _api_rate_limiter,
    _rate_limit_wait_seconds,
    is_quota_exhausted,
    mark_quota_exhausted,
)

# 预测路径上 429 的重试次数（与统一 client 的 RATE_LIMIT_RETRIES 对齐）
_API_GET_RETRIES = 3


def _http_with_deadline(method: str, url: str, *, headers=None, params=None,
                        json=None, timeout: float = 30.0, label: str = "HTTP"):
    """发起 HTTP 请求并强制「墙钟超时」上限，超时/异常返回 None。

    httpx 同步客户端的 timeout 不约束 DNS 解析阶段：若服务器无法访问外网
    （DNS/连接被防火墙黑洞），请求会无限阻塞，导致后端 worker 被永久挂起
    （表现为前端 socket hang up、curl 永久等待）。这里改用独立线程 + join 超时
    强制设上限，让请求快速失败而非卡死进程。
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout

    def _do():
        import httpx
        return httpx.request(
            method, url,
            headers=headers or {}, params=params, json=json,
            timeout=timeout,
        )

    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(_do)
        try:
            return fut.result(timeout=timeout + 5)
        except _FuturesTimeout:
            logger.error(
                f"{label} 请求在 {timeout + 5:.0f}s 内无响应"
                f"（疑似服务器无法访问外网 / DNS 解析挂起）: {url}"
            )
            return None
        except Exception as e:  # noqa: BLE001
            logger.debug(f"{label} 请求异常: {e}")
            return None


def _api_get(path: str, params: dict, timeout: float = 10.0) -> dict:
    """带节流地请求 API-Football 的指定接口，返回解析后的 JSON。

    节流走 app.core.api_football 的共享限流器（与赛程同步共用同一份预算）；
    HTTP 仍经 _http_with_deadline，保留墙钟超时保护——httpx 的 timeout 不
    覆盖 DNS 解析阶段，见该函数说明。
    """
    if not settings.api_football_base_url:
        logger.debug("api_football_base_url 未配置，跳过 API-Football 请求")
        return {}
    if is_quota_exhausted():
        logger.debug(f"API-Football 当日额度已耗尽，跳过请求 [{path}]")
        return {}
    url = f"{settings.api_football_base_url.rstrip('/')}/{path.lstrip('/')}"

    for attempt in range(_API_GET_RETRIES):
        # 每次真正发出的请求（含重试）都占用共享额度
        _api_rate_limiter.acquire()
        resp = _http_with_deadline(
            "GET", url,
            headers={"x-apisports-key": settings.api_football_key},
            params=params, timeout=timeout, label="API-Football",
        )
        if resp is None:
            logger.warning(f"API-Football 请求无响应 [{path}] params={params}")
            return {}

        if resp.status_code == 429:
            if attempt >= _API_GET_RETRIES - 1:
                logger.warning(f"API-Football 短时限流重试耗尽 [{path}]")
                return {}
            wait = _rate_limit_wait_seconds(resp, attempt)
            # 共享窗口：把本地节奏一并后移，避免后续请求继续踩在同一条边界上
            _api_rate_limiter.penalize(wait)
            logger.warning(
                f"API-Football 短时限流 [{path}] (429)，{wait:.1f}s 后重试 #{attempt + 1}"
            )
            time.sleep(wait)
            continue

        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"API-Football 响应失败 [{path}] status={resp.status_code} "
                f"params={params}: {e}; body={resp.text[:500]!r}"
            )
            return {}
        # 即便 HTTP 200，接口也可能在 errors 字段返回限流/配额等错误（如当日请求数耗尽）
        if isinstance(data, dict) and data.get("errors"):
            _msg = data["errors"].get("requests") or data["errors"].get("message") or str(data["errors"])
            logger.warning(f"API-Football 返回错误 [{path}]: {_msg}")
            # errors.requests 是当日额度耗尽的明确信号：置位闩锁，避免后续
            # 预测继续消耗已用尽的额度。
            if isinstance(data["errors"], dict) and "requests" in data["errors"]:
                mark_quota_exhausted(f"{path}: {_msg}")
        return data
    return {}


def _load_feature_cols() -> list[str]:
    with open(FEATURE_COLS_PATH, 'rb') as f:
        return pickle.load(f)


def _load_best_models(league_id: int) -> tuple[dict, str]:
    league_key = f'L_{league_id}'
    m = load_models(league_key)
    if m:
        return m, league_key
    group = assign_group(league_id)
    m = load_models(group)
    if m:
        return m, group
    m = load_models('GLOBAL')
    if m:
        return m, 'GLOBAL'
    return None, None


def _poisson_top3(lambda_home: float, lambda_away: float,
                  max_goals: int = 8) -> list[dict]:
    lh = max(0.1, min(lambda_home, 8.0))
    la = max(0.1, min(lambda_away, 8.0))
    scores = []
    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            p = poisson.pmf(h, lh) * poisson.pmf(a, la)
            scores.append({'score': f'{h}-{a}', 'prob': round(float(p), 4)})
    scores.sort(key=lambda x: x['prob'], reverse=True)
    return scores[:3]


# 胜负口径别名：llm_win 落库后要与赛果直接比对，写法不统一会导致命中率永远判不中。
_WIN_LABEL_ALIASES = {
    "home": "主胜", "home win": "主胜", "主队": "主胜", "主队胜": "主胜", "主胜": "主胜",
    "draw": "平局", "draws": "平局", "平": "平局", "平局": "平局", "和局": "平局",
    "away": "客胜", "away win": "客胜", "客队": "客胜", "客队胜": "客胜", "客胜": "客胜",
}


def _normalize_llm_fields(parsed: dict) -> dict:
    """将模型可能返回的列表/数值字段规整为字符串，避免写入 TEXT 列时报错。

    对缺失/异常字段做容错：任何字段（含值本身）只要无法安全转字符串，
    都统一落为 ""，绝不让单字段异常把整次 LLM 预测拖垮（此前曾因
    data['venue'] 缺失触发 KeyError，导致整场预测被判失败）。
    """
    out = {}
    for k, v in parsed.items():
        if v is None:
            out[k] = ""
        elif isinstance(v, list):
            try:
                out[k] = ",".join(str(x) for x in v)
            except Exception:
                out[k] = ""
        else:
            try:
                out[k] = str(v)
            except Exception:
                out[k] = ""
    alias = _WIN_LABEL_ALIASES.get(str(out.get("win", "")).strip().lower())
    if alias:
        out["win"] = alias
    return out


def _call_codex_provider(
    prompt: str, *, model: str, base_url: str, api_key: str, provider_name: str
) -> dict | None:
    """经 codex exec 完成一次预测推理，并校验其结构化输出。

    失败仅对 codex 自身重试（run_codex 内部按 codex_retries 重试），
    不回退 HTTP。输出由 --output-schema 强约束为 10 字段 JSON，这里仍做
    解析/归一/必填二次校验，防 schema 之外的脏数据进库。
    """
    content = run_codex(
        prompt,
        schema_path=PREDICTION_SCHEMA_PATH,
        model=model,
        base_url=base_url,
        api_key=api_key,
        sandbox=settings.codex_sandbox,
        timeout=float(settings.codex_timeout_predict),
        retries=settings.codex_retries,
        label=provider_name,
    )
    if content is None:
        return None
    # codex 在 exec 完成前不返回中间/流式数据；这里把最终返回的原始内容落日志，
    # 便于排查前端误判为失败时后端实际收到了什么。
    logger.info("{} 返回原始内容(长度={}): {!r}", provider_name, len(content), content[:2000])
    parsed = _parse_llm_json(content)
    if not parsed:
        logger.warning("{} 返回内容中未找到 JSON，preview={!r}", provider_name, content[:500])
        return None
    # 先规整再校验：schema 已限定 win/handicap_team/ou_type 取值，这里再兜底
    # 归一一次，防止上游契约放宽后出现 home/draw/away 这类别名写法。
    parsed = _normalize_llm_fields(parsed)
    if not _has_required_llm_fields(parsed):
        logger.warning("{} 返回缺少必要预测字段，keys={}", provider_name, sorted(parsed.keys()))
        return None
    return parsed


def _call_llm(prompt: str) -> tuple[dict | None, str | None]:
    """以 codex CLI 完成最终预测；主模型不可用时改用备用模型名（仍走 codex）。

    返回 ``(结果, 实际出结果的模型名)``，两侧都失败时为 ``(None, None)``。
    模型名要落库，否则事后无法区分一条预测是主模型还是备用模型算出来的。
    """
    if settings.prediction_use_codex_default:
        logger.info("使用 codex 默认模型配置进行预测")
        result = _call_codex_provider(
            prompt,
            model="",
            base_url="",
            api_key="",
            provider_name="codex 默认模型",
        )
        if result is not None:
            return result, "codex:default"
        return None, None

    if (settings.prediction_llm_model or "").strip():
        primary_name = f"codex 主预测 ({settings.prediction_llm_model})"
        result = _call_codex_provider(
            prompt,
            model=settings.prediction_llm_model,
            base_url=settings.prediction_llm_base_url,
            api_key=settings.prediction_llm_api_key,
            provider_name=primary_name,
        )
        if result is not None:
            logger.info("预测 LLM 成功: provider=codex, model={}", settings.prediction_llm_model)
            return result, f"codex:{settings.prediction_llm_model}"
    else:
        logger.warning("未配置 prediction_llm_model，跳过主预测模型")

    fallback_model = (settings.prediction_fallback_llm_model or "").strip()
    if not fallback_model:
        return None, None
    fallback_name = f"codex 备用预测 ({fallback_model})"
    logger.warning("主预测模型不可用，切换备用模型: {}", fallback_name)
    fallback_base_url = settings.prediction_fallback_llm_base_url or settings.prediction_llm_base_url
    fallback_api_key = settings.prediction_fallback_llm_api_key or settings.prediction_llm_api_key
    result = _call_codex_provider(
        prompt,
        model=fallback_model,
        base_url=fallback_base_url,
        api_key=fallback_api_key,
        provider_name=fallback_name,
    )
    if result is not None:
        logger.info("预测 LLM 成功: provider=codex-fallback, model={}", fallback_model)
        return result, f"codex:{fallback_model}"
    return None, None


def _parse_llm_json(content: str) -> dict | None:
    """从模型回复中提取第一个合法 JSON object。"""
    decoder = json.JSONDecoder()
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _has_required_llm_fields(data: dict) -> bool:
    required = (
        "handicap_num", "handicap_team", "handicap_pct",
        "ou_line", "ou_type", "ou_pct", "deep_report",
        # 胜负与比分现在是 LLM 自己的结论（不再由贝叶斯结果回填），缺了就会让
        # llm_win/llm_score 落空：命中率判不了，auto_predict 的重算条件也会一直成立。
        "win", "win_pct", "score",
    )
    # 注意：handicap_num 可能为 0（平手盘），不能用 `or ""` 的布尔判断，
    # 否则合法值 0 会被误判为“缺失”，导致整场预测被丢弃。
    for k in required:
        v = data.get(k)
        if v is None or str(v).strip() == "":
            return False
    return True


def _parse_1x2(values) -> dict | None:
    """解析 1X2（主/平/客）赔率，返回隐含概率与原始赔率，失败返回 None。"""
    try:
        vals = {v["value"]: float(v["odd"]) for v in values}
        ho, dr, aw = vals.get("Home"), vals.get("Draw"), vals.get("Away")
        if not (ho and dr and aw):
            return None
        t = 1 / ho + 1 / dr + 1 / aw
        return {
            "home_odd": round(1 / ho / t, 3), "draw_odd": round(1 / dr / t, 3), "away_odd": round(1 / aw / t, 3),
            "home_raw": ho, "draw_raw": dr, "away_raw": aw,
        }
    except Exception:
        return None


def _parse_asian_handicap(values) -> dict | None:
    """解析亚盘（Asian Handicap）。

    收集所有让球盘口，默认取双方赔率最相近（原始赔率差最小）的盘口线，
    即庄家认为最均衡的让球盘；返回该盘口的让球线与两侧隐含概率/原始赔率。
    """
    try:
        lines: dict = {}
        for v in values:
            s = v["value"]  # e.g. "Home -1.5"
            team, hcap = s.rsplit(" ", 1)
            hcap = float(hcap)
            lines.setdefault(hcap, {})[team] = float(v["odd"])
        # 仅保留主/客都有的有效盘口
        valid = {ln: o for ln, o in lines.items() if o.get("Home") and o.get("Away")}
        if not valid:
            return None
        # 默认取双方（主/客）赔率最相近的盘口线
        ln = min(valid, key=lambda l: abs(valid[l]["Home"] - valid[l]["Away"]))
        ho, ao = valid[ln]["Home"], valid[ln]["Away"]
        t = 1 / ho + 1 / ao
        return {
            "ah_line": ln,
            "ah_home_odd": round(1 / ho / t, 3), "ah_away_odd": round(1 / ao / t, 3),
            "ah_home_raw": ho, "ah_away_raw": ao,
        }
    except Exception:
        return None


def _parse_over_under(values, prefer_line: float | None = None) -> dict | None:
    """解析大小球（Goals Over/Under）。

    默认（prefer_line 为 None）取双方赔率最相近（原始赔率差最小）的盘口线，
    即庄家认为最均衡、最像"五五开"的盘口；若指定 prefer_line 且该盘口存在则优先使用。
    """
    try:
        lines: dict = {}
        for v in values:
            s = v["value"]  # e.g. "Over 2.5"
            kind, ln = s.rsplit(" ", 1)
            ln = float(ln)
            lines.setdefault(ln, {})[kind] = float(v["odd"])
        # 仅保留 Over / Under 都有的有效盘口
        valid = {ln: o for ln, o in lines.items() if o.get("Over") and o.get("Under")}
        if not valid:
            return None
        if prefer_line is not None and prefer_line in valid:
            ln = prefer_line
        else:
            # 默认：取双方（大/小）赔率最相近的盘口线
            ln = min(valid, key=lambda l: abs(valid[l]["Over"] - valid[l]["Under"]))
        ov, un = valid[ln]["Over"], valid[ln]["Under"]
        t = 1 / ov + 1 / un
        return {
            "ou_line": ln,
            "ou_over_odd": round(1 / ov / t, 3), "ou_under_odd": round(1 / un / t, 3),
            "ou_over_raw": ov, "ou_under_raw": un,
        }
    except Exception:
        return None


# 固定庄家白名单（大小写不敏感匹配）。仅抓取并保留这些庄家的赔率数据，
# 用于替代原先「取数据最多前 5 家」的逻辑，保证各家数据来源稳定一致。
ODDS_BOOKMAKER_WHITELIST = ["Bet365", "Unibet", "SBO", "Pinnacle", "Marathonbet"]


def _fetch_odds(fixture_id: int, match_date=None,
                bookmaker_whitelist: list[str] | None = None) -> dict | None:
    """拉取赔率（含 1X2 / 亚盘 / 大小球）。

    仅查询 API-Football 当前日期（UTC）；每次调用都会按需请求最新赔率，不做 fixture 级
    缓存或调用次数限制。本地时区凌晨时，API 仍可能处于前一天。
    这里的 ``date`` 是赔率快照的发布日期，而不是比赛日期；未来比赛的赔率只能通过今天
    的快照查询。仅保留真正抓到赔率的日期。

    庄家筛选使用固定白名单（默认 ODDS_BOOKMAKER_WHITELIST，大小写不敏感），只保留
    白名单内的庄家，最终按白名单顺序输出；若某家在窗口内无数据则自动缺失。
    """
    try:
        from datetime import datetime, timezone
        whitelist = [b.strip() for b in (bookmaker_whitelist or ODDS_BOOKMAKER_WHITELIST) if b.strip()]
        wl_lower = {b.lower(): b for b in whitelist}  # 规范名（按白名单原样）
        # API-Football 按 UTC 计算赔率快照日期；本地凌晨时仍可能处于 API 的前一天。
        api_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        ordered = [api_date]
        logger.info(
            f"赔率 API 请求: fixture_id={fixture_id}, dates={ordered}, "
            f"local_now={datetime.now().isoformat()}, utc_date={api_date}"
        )

        # 收集所有庄家数据: {bookmaker_name: {date: entry}}
        bookmaker_odds = {}
        first_api_error = None  # 记录首个 API-Football 错误（如当日配额耗尽），用于向上游暴露
        for date_str in ordered:
            resp: dict[Any, Any] = _api_get("odds", {"fixture": fixture_id, "date": date_str})
            if isinstance(resp, dict) and resp.get("errors"):
                if first_api_error is None:
                    first_api_error = resp["errors"]
                logger.warning(
                    f"赔率 API 返回错误: fixture_id={fixture_id}, date={date_str}, "
                    f"errors={resp['errors']}"
                )
                continue  # 该日期无赔率数据
            data = resp.get("response", []) if isinstance(resp, dict) else []
            if not data:
                logger.warning(
                    f"赔率 API 无数据: fixture_id={fixture_id}, date={date_str}, "
                    f"response_count=0"
                )
                continue

            raw_bookmakers = [bm.get("name", "未知") for bm in data[0].get("bookmakers", [])]
            matched_bookmakers = []
            for bm in data[0].get("bookmakers", []):
                bm_name = bm.get("name", "未知")
                # 仅保留白名单内的庄家（大小写不敏感）
                key = bm_name.lower()
                if key not in wl_lower:
                    continue
                canon = wl_lower[key]
                matched_bookmakers.append(canon)
                bm_dict = bookmaker_odds.setdefault(canon, {})
                for bet in bm.get("bets", []):
                    name = bet.get("name")
                    values = bet.get("values", [])
                    parsed = None
                    if name == "Match Winner":
                        parsed = _parse_1x2(values)
                    elif name == "Asian Handicap":
                        parsed = _parse_asian_handicap(values)
                    elif name == "Goals Over/Under":
                        parsed = _parse_over_under(values)
                    if parsed:
                        base = bm_dict.get(date_str, {"date": date_str})
                        base.update(parsed)
                        base["date"] = date_str
                        bm_dict[date_str] = base

            # 白名单内的庄家全部集满即可停止, 减少无效 API 请求
            if len(bookmaker_odds) >= len(whitelist):
                break

        if not bookmaker_odds:
            # 优先返回真实的接口错误（如限流），让上层给出明确的 429 而非误判为「无赔率」
            if first_api_error is not None:
                return {"__api_error__": first_api_error}
            logger.warning(
                f"赔率数据过滤后为空: fixture_id={fixture_id}, date={ordered[0]}, "
                f"api_bookmakers={raw_bookmakers if 'raw_bookmakers' in locals() else []}, "
                f"whitelist={whitelist}, matched={matched_bookmakers if 'matched_bookmakers' in locals() else []}"
            )
            return None

        # 按白名单顺序输出（仅含白名单内且实际抓到数据的庄家）
        odds_data = []
        for canon in whitelist:
            dates = bookmaker_odds.get(canon)
            if not dates:
                continue
            all_dates = sorted(dates.keys())
            entries = [dates[d] for d in all_dates]
            odds_data.append({"bookmaker": canon, "entries": entries})

        if not odds_data:
            logger.warning(f"赔率盘口解析后为空: fixture_id={fixture_id}, date={ordered[0]}")
            return None
        return {"odds_data": odds_data}
    except Exception as e:
        logger.exception(f"赔率获取异常: fixture_id={fixture_id}, error={e}")
    return None


# Flashscore 散列表 / 赔率兜底所需：Flashscore 8位 hash -> API-Football team_id
_FLASHSCORE_TEAM_MAP_FILE = os.path.join(
    os.path.dirname(__file__), '..', 'tools', 'flashscore_team_map.json')


def _fetch_flashscore_odds(fixture: dict) -> dict | None:
    """API-Football 赔率拉取失败时的兜底：爬取 Flashscore 的 1X2 / 亚盘 / 大小球。

    仅当双方球队都能在 flashscore_team_map.json 中找到对应 hash 时可用。
    抓取前会核对页面比赛日期与双方队名，避免拿到两队之间另一场比赛的赔率。
    成功返回 {"odds_data": [...]}（与 _fetch_odds 同构），否则返回 None。
    """
    if not os.path.exists(_FLASHSCORE_TEAM_MAP_FILE):
        logger.warning("未找到 flashscore_team_map.json，跳过 Flashscore 赔率兜底")
        return None
    try:
        with open(_FLASHSCORE_TEAM_MAP_FILE, encoding="utf-8") as f:
            team_map = json.load(f)
    except Exception as e:
        logger.warning(f"读取 flashscore_team_map.json 失败，跳过兜底: {e}")
        return None

    id_to_hash = {tid: h for h, tid in team_map.items()}
    home_hash = id_to_hash.get(fixture.get('home_id'))
    away_hash = id_to_hash.get(fixture.get('away_id'))
    if not home_hash or not away_hash:
        logger.info(
            f"Flashscore 赔率兜底不可用（球队缺少 hash 映射）fixture={fixture.get('id')}: "
            f"home={fixture.get('home_name')} away={fixture.get('away_name')}"
        )
        return None

    hash_to_name = {
        home_hash: fixture.get('home_name'),
        away_hash: fixture.get('away_name'),
    }
    try:
        from tools.fetch_flashscore_odds import scrape_match_odds

        result = scrape_match_odds(
            home_hash, away_hash, hash_to_name, match_date=fixture.get('date'))
    except Exception as e:
        logger.warning(f"Flashscore 赔率抓取失败 fixture={fixture.get('id')}: {e}")
        return None

    odds_data = result.get("odds_data")
    if not odds_data:
        logger.warning(
            f"Flashscore 未抓到可用赔率 fixture={fixture.get('id')}: {result.get('error')}"
        )
        return None
    logger.info(
        f"Flashscore 赔率兜底成功 fixture={fixture.get('id')} "
        f"via={result.get('resolved_via')} page_date={result.get('page_date')}"
    )
    return {"odds_data": odds_data}


def _odds_to_text(odds_data: list) -> str:
    """将结构化赔率数据转换为可读文本，供 LLM 提示词使用（含 1X2 / 亚盘 / 大小球）。"""
    from datetime import datetime as _dt

    def _fmt_line(v):
        fv = float(v)
        return str(int(fv)) if fv == int(fv) else str(fv)

    lines = []
    for bm in odds_data:
        lines.append(f"  {bm['bookmaker']}:")
        for e in bm['entries']:
            try:
                d = _dt.strptime(e['date'], "%Y-%m-%d").strftime("%m/%d")
            except Exception:
                d = e['date']
            parts = []
            if e.get('home_odd') is not None:
                parts.append(
                    f"1X2 主{e['home_odd']:.0%}({e['home_raw']}) "
                    f"平{e['draw_odd']:.0%}({e['draw_raw']}) "
                    f"客{e['away_odd']:.0%}({e['away_raw']})"
                )
            if e.get('ah_home_odd') is not None:
                parts.append(
                    f"亚盘{_fmt_line(e['ah_line'])} "
                    f"主{e['ah_home_odd']:.0%}({e['ah_home_raw']}) "
                    f"客{e['ah_away_odd']:.0%}({e['ah_away_raw']})"
                )
            if e.get('ou_over_odd') is not None:
                parts.append(
                    f"大小球{_fmt_line(e['ou_line'])} "
                    f"大{e['ou_over_odd']:.0%}({e['ou_over_raw']}) "
                    f"小{e['ou_under_odd']:.0%}({e['ou_under_raw']})"
                )
            if parts:
                lines.append(f"    {d}: " + " | ".join(parts))

    return "\n" + "\n".join(lines)


def _fetch_weather_text(city_name: str, kickoff_ts: int | None = None) -> str:
    """获取比赛城市在**开球时刻**的天气及海拔，转换为供 LLM 使用的文本。

    ``kickoff_ts`` 必须是 UTC 时间戳（fixtures.timestamp）。注意 fixtures.date 存的是
    MySQL 服务器本地时间（北京 +08）的 naive datetime，直接用它对不齐开球时刻。

    开球时刻在 5 天预报窗口内时取预报中时间上最接近的那一格；超出窗口则降级为当前
    实况，并在文本末尾明确标注，避免 LLM 把实况当成开球时的天气。
    """
    if not city_name:
        return "未提供比赛城市，无法获取天气及海拔数据。"
    if not settings.openweathermap_api_key:
        return "未配置天气 API key，无法获取天气及海拔数据。"

    try:
        from tools.weather import get_weather_and_elevation

        raw = get_weather_and_elevation(city_name, settings.openweathermap_api_key, kickoff_ts)
        data = json.loads(raw)
        if data.get("status") != "success":
            return "天气及海拔数据获取失败，不能据此推断外部环境。"

        location = data.get("location", {})
        weather = data.get("weather", {})
        elevation = location.get("elevation_meters")
        elevation_text = f"{elevation}米" if elevation is not None else "未知"

        parts = [
            f"城市:{location.get('city') or city_name}",
            f"天气:{weather.get('description', '未知')}",
            f"气温:{weather.get('temperature_celsius', '未知')}°C",
            f"体感:{weather.get('feels_like_celsius', '未知')}°C",
        ]
        # 3 小时预报格的 min/max 只是该格区间抖动，只在实况下呈现当天最低/最高
        if weather.get("source") == "current":
            parts.append(f"最低/最高:{weather.get('temp_min', '未知')}/{weather.get('temp_max', '未知')}°C")
        parts += [
            f"湿度:{weather.get('humidity_percent', '未知')}%",
            f"气压:{weather.get('pressure_hpa', '未知')}hPa",
            f"风速:{weather.get('wind_speed_m_s', '未知')}m/s",
        ]
        pop = weather.get("pop")
        if pop is not None:
            parts.append(f"降水概率:{round(float(pop) * 100)}%")
        parts.append(f"海拔:{elevation_text}")

        if weather.get("source") == "forecast":
            parts.append(
                f"(以上为开球时刻预报，取值时刻 {weather.get('valid_at')} 当地时间，"
                f"与开球时间相差 {weather.get('offset_minutes')} 分钟)"
            )
        else:
            note = weather.get("note") or "开球时刻无可用预报"
            parts.append(f"(注意:以上为当前实况而非开球时刻天气——{note})")

        return " ".join(parts)
    except Exception:  # noqa: BLE001 - 外部环境数据不得阻断主预测流程
        return "天气及海拔数据处理失败，不能据此推断外部环境。"


# scrape_match_analysis 会把异常吞成一段占位 Markdown，靠这个标记串识别失败。
_INTELLIGENCE_FAILURE_MARK = "获取失败，忽略该数据源"


def _fetch_intelligence_markdown(fixture: dict) -> tuple[str, str]:
    """获取本场外部比赛情报 Markdown，失败时不阻断预测。

    返回 ``(markdown, status)``，status 为 ``AVAILABLE`` / ``UNAVAILABLE``。
    此前来源账本里 club_news 的 status 恒为 CANDIDATE，与实际取数结果完全脱节；
    这里把真实结果带回去，供 :func:`_bayes_evidence` 回写账本状态。
    """
    logger.info(
        "情报获取开始: fixture={}, home={}, away={}",
        fixture.get("id"), fixture.get("home_name"), fixture.get("away_name"),
    )
    try:
        from tools.fetch_intelligence import scrape_match_analysis

        result = scrape_match_analysis(
            home_team=str(fixture.get("home_name") or ""),
            away_team=str(fixture.get("away_name") or ""),
            competition=str(fixture.get("league_name") or ""),
            year=fixture.get("season") or datetime.now().year,
            api_key=settings.firecrawl_api_key,
            firecrawl_base_url=settings.firecrawl_base_url,
            intelligence_llm_api_key=settings.intelligence_llm_api_key,
            intelligence_llm_base_url=settings.intelligence_llm_base_url,
            intelligence_llm_model=settings.intelligence_llm_model,
            limit=3,
        )
        text = result or ""
        logger.info("情报获取结束: fixture={}, output_chars={}", fixture.get("id"), len(text))
        if not text.strip() or _INTELLIGENCE_FAILURE_MARK in text:
            return text, "UNAVAILABLE"
        return text, "AVAILABLE"
    except Exception as exc:
        logger.exception("情报获取异常: fixture={}, error={}", fixture.get("id"), exc)
        return "# 外部比赛情报\n\n获取失败，忽略该数据源。", "UNAVAILABLE"


def _fetch_standings_text(db, team_id, league_id, season) -> str:
    row = db.execute(text("""
        SELECT `rank`, points, goals_diff, all_played, all_win, all_draw, all_lose
        FROM standings WHERE team_id=:tid AND league_id=:lid AND season=:s LIMIT 1
    """), {"tid": team_id, "lid": league_id, "s": season}).fetchone()
    if not row:
        return "无积分榜数据"
    d = dict(row._mapping)
    return f"排名{d['rank']} 积{d['points']}分 {d['all_win']}胜{d['all_draw']}平{d['all_lose']}负 净胜球{d['goals_diff']}"


def _fetch_team_recent_via_api(team_id: int) -> str:
    """通过API拉取球队近10场真实战绩（含逐场对手明细）"""
    try:
        data = _api_get("fixtures", {"team": team_id, "last": 10}).get("response", [])
        if not data:
            return "无"
        wins = draws = losses = gf = ga = 0
        details = []
        results_form = []
        for item in data:
            teams = item["teams"]
            goals = item["goals"]
            league = item.get("league", {})
            is_home = teams["home"]["id"] == team_id
            gh = goals["home"] if goals["home"] is not None else 0
            ga_ = goals["away"] if goals["away"] is not None else 0
            opponent = teams["away"]["name"] if is_home else teams["home"]["name"]
            league_name = league.get("name", "")
            if is_home:
                gf += gh; ga += ga_
                if gh > ga_: w = 'W'; wins += 1
                elif gh == ga_: w = 'D'; draws += 1
                else: w = 'L'; losses += 1
                score = f"{gh}-{ga_}"
            else:
                gf += ga_; ga += gh
                if ga_ > gh: w = 'W'; wins += 1
                elif ga_ == gh: w = 'D'; draws += 1
                else: w = 'L'; losses += 1
                score = f"{ga_}-{gh}"
            results_form.append(w)
            details.append(f"  {opponent}({league_name}) {score} {w}")

        results_form.reverse()
        details.reverse()
        n = len(data)
        detail_text = '\n'.join(details)
        form_str = ' '.join(results_form[-10:])
        return f"{wins}胜{draws}平{losses}负 进{gf}球失{ga}球 场均进{gf/n:.1f}失{ga/n:.1f}\n近10场明细:\n{detail_text}"
    except Exception as e:
        logger.debug(f"API拉取球队数据失败 team={team_id}: {e}")
        return "无"


def _fetch_team_recent_via_db(db, team_id: int, before_date=None) -> str:
    """从本地 fixtures 表聚合球队近10场真实战绩（不依赖外部 API，绕开免费版限制）。

    同时关联 fixture_statistics 表，附上每场比赛本队的 expected_goals(xG)，
    用于交给 LLM 做高阶数据（xG）维度的分析。

    before_date: 只统计该日期之前的比赛，用于计算某场比赛前的真实状态；
                 为 None 时取数据库中最新的10场。
    """
    try:
        if before_date is not None:
            sql = text("""
                SELECT id, home_id, away_id, home_name, away_name, league_name,
                       goals_home, goals_away, status_short, date
                FROM fixtures
                WHERE (home_id = :tid OR away_id = :tid)
                  AND goals_home IS NOT NULL AND goals_away IS NOT NULL
                  AND status_short IN ('FT', 'AET', 'PEN')
                  AND date < :bdate
                ORDER BY date DESC
                LIMIT 10
            """)
            params = {"tid": team_id, "bdate": before_date}
        else:
            sql = text("""
                SELECT id, home_id, away_id, home_name, away_name, league_name,
                       goals_home, goals_away, status_short, date
                FROM fixtures
                WHERE (home_id = :tid OR away_id = :tid)
                  AND goals_home IS NOT NULL AND goals_away IS NOT NULL
                  AND status_short IN ('FT', 'AET', 'PEN')
                ORDER BY date DESC
                LIMIT 10
            """)
            params = {"tid": team_id}

        rows = db.execute(sql, params).fetchall()
        if not rows:
            return "无"

        # 关联 fixture_statistics，批量拉取近10场本队的 expected_goals(xG)
        fixture_ids = tuple(r._mapping["id"] for r in rows) or (-1,)
        xg_rows = db.execute(text("""
            SELECT fixture_id, stat_value
            FROM fixture_statistics
            WHERE team_id = :tid AND stat_type = 'expected_goals'
              AND fixture_id IN :fids
        """), {"tid": team_id, "fids": fixture_ids}).fetchall()
        xg_map = {}
        for r in xg_rows:
            rm = dict(r._mapping)
            try:
                xg_map[rm["fixture_id"]] = float(str(rm["stat_value"]).replace('%', ''))
            except (TypeError, ValueError):
                xg_map[rm["fixture_id"]] = None

        wins = draws = losses = gf = ga = 0
        xg_sum = 0.0
        xg_cnt = 0
        details = []
        for r in rows:  # rows 已按 date DESC，由新到旧输出明细
            rm = dict(r._mapping)
            is_home = rm["home_id"] == team_id
            gh = int(rm["goals_home"] or 0)
            ga_ = int(rm["goals_away"] or 0)
            tg = gh if is_home else ga_
            ta = ga_ if is_home else gh
            opp = rm["away_name"] if is_home else rm["home_name"]
            gf += tg
            ga += ta
            if tg > ta:
                w, wins = "胜", wins + 1
            elif tg < ta:
                w, losses = "负", losses + 1
            else:
                w, draws = "平", draws + 1
            score = f"{gh}-{ga_}" if is_home else f"{ga_}-{gh}"

            xg = xg_map.get(rm["id"])
            if xg is not None:
                xg_sum += xg
                xg_cnt += 1
                xg_str = f" xG{xg:.2f}"
            else:
                xg_str = ""
            details.append(f"  {opp}({rm['league_name']}) {score} {w}{xg_str}")

        n = len(rows)
        detail_text = "\n".join(details)
        # xG 样本不足 5 场时代表性不足，不统计场均 xG
        avg_xg = f" 场均xG{xg_sum / xg_cnt:.2f}" if xg_cnt >= 5 else ""
        return (f"{wins}胜{draws}平{losses}负 进{gf}球失{ga}球 "
                f"场均进{gf / n:.1f}失{ga / n:.1f}{avg_xg}\n"
                f"近10场明细(由近及远):\n{detail_text}")
    except Exception as e:
        logger.debug(f"本地聚合球队近况失败 team={team_id}: {e}")
        return "无"


def _fetch_lineups_text(fixture_id: int, home_id: int, away_id: int) -> str:
    """拉取确认首发；未公布时返回阵容不确定提示。"""
    try:
        data = _api_get("fixtures/lineups", {"fixture": fixture_id}).get("response", [])
        if not data:
            return "未获取到确认首发。国家队/世界杯阵容轮换、临场战术和球员状态不确定，必须降低置信度。"

        lines = []
        for team_entry in data:
            team = team_entry.get("team") or {}
            tid = team.get("id")
            side = "主队" if tid == home_id else "客队" if tid == away_id else "球队"
            formation = team_entry.get("formation") or "未知阵型"
            starters = []
            for item in team_entry.get("startXI") or []:
                player = item.get("player") or {}
                name = player.get("name")
                pos = player.get("pos")
                if name:
                    starters.append(f"{name}{f'({pos})' if pos else ''}")
            if starters:
                lines.append(f"{side} {team.get('name', '')} {formation}: " + ", ".join(starters))

        if not lines:
            return "已查询阵容接口，但未得到有效首发名单。按阵容未确认处理，降低置信度。"
        return "确认首发:\n" + "\n".join(lines)
    except Exception as e:
        logger.debug(f"首发阵容获取失败 fixture={fixture_id}: {e}")
        return "首发阵容获取失败。按阵容未确认处理，特别是世界杯/国家队赛事必须降低置信度。"


# --- 无效控球(possession trap) 判定参数 ---
# 效力差 = 控球占比 − Field Tilt(进攻三区传球占比)。窗口内样本不足时不判定。
FIELD_TILT_WINDOW = 5           # 取样窗口: 近 N 场已完赛比赛
FIELD_TILT_GAP_TRIGGER = 0.08   # 触发阈值: 效力差 ≥ 8pp 视为无效控球
FIELD_TILT_CAP = 0.02           # 概率移动上限 (与 schedule_density 同量级)


def _stat_float(value) -> float | None:
    """把 fixture_statistics.stat_value 字符串转成浮点数。

    控球率等存的是 '58%' 这类带百分号的字符串, 与 features.py 的 pf() 口径一致。
    """
    if value is None:
        return None
    try:
        return float(str(value).replace('%', '').strip())
    except (TypeError, ValueError):
        return None


def _recent_field_tilt_gap(db, team_id, kickoff,
                           window: int = FIELD_TILT_WINDOW) -> dict:
    """近 ``window`` 场已完赛比赛的「控球占比 − Field Tilt」均值。

    Field Tilt 用 Flashscore 的 ``passes_final_third`` 按场次内外相除得到,
    控球占比取自 API-Football 的 ``Ball Possession``。两者都按本场双方求和
    归一化为占比, 这样即便两家数据商的三区边界/控球口径略有差异, 也不会被
    直接放大成假信号。

    某场必须四项数值(己方/对手 × 三区传球/控球)全部齐备才计入; 凑不满
    ``window`` 场返回 ``status=UNAVAILABLE``, 由调用方按缺失处理, 不使用
    射门、角球等作为代理近似。
    """
    if kickoff is None:
        return {"status": "UNAVAILABLE", "samples": 0, "reason": "缺少本场开赛时间"}

    rows = db.execute(text(f"""
        SELECT f.id, f.date,
               MAX(CASE WHEN fs.team_id =  :team AND fs.stat_type = 'passes_final_third'
                        THEN fs.stat_value END) AS own_pft,
               MAX(CASE WHEN fs.team_id <> :team AND fs.stat_type = 'passes_final_third'
                        THEN fs.stat_value END) AS opp_pft,
               MAX(CASE WHEN fs.team_id =  :team AND fs.stat_type = 'Ball Possession'
                        THEN fs.stat_value END) AS own_poss,
               MAX(CASE WHEN fs.team_id <> :team AND fs.stat_type = 'Ball Possession'
                        THEN fs.stat_value END) AS opp_poss
          FROM fixtures f
          JOIN fixture_statistics fs ON fs.fixture_id = f.id
         WHERE (f.home_id = :team OR f.away_id = :team)
           AND f.date < :kickoff
           AND f.status_short IN ('FT','AET','PEN')
         GROUP BY f.id, f.date
         ORDER BY f.date DESC
         LIMIT {int(window)}
    """), {"team": team_id, "kickoff": kickoff}).fetchall()

    gaps = []
    for row in rows:
        own_pft = _stat_float(row[2])
        opp_pft = _stat_float(row[3])
        own_poss = _stat_float(row[4])
        opp_poss = _stat_float(row[5])
        if own_pft is None or opp_pft is None or own_poss is None or opp_poss is None:
            continue
        tilt_den = own_pft + opp_pft
        poss_den = own_poss + opp_poss
        if tilt_den <= 0 or poss_den <= 0:
            continue
        gaps.append(own_poss / poss_den - own_pft / tilt_den)

    if len(gaps) < window:
        return {"status": "UNAVAILABLE", "samples": len(gaps),
                "reason": f"近{window}场中仅 {len(gaps)} 场可算出三区传球占比与控球占比"}

    return {"status": "AVAILABLE", "samples": len(gaps),
            "gap": round(sum(gaps) / len(gaps), 4)}


def _bayes_evidence(db, fixture: dict, lineups_text: str,
                    intel_status: str = "UNAVAILABLE") -> tuple[list[dict], list[dict], list[str]]:
    """Create a source ledger and only mechanically-supported Bayesian updates.

    The ledger is saved even for unavailable fields.  This is important: an
    absent Field Tilt/PPDA feed is a known uncertainty, not permission for the
    LLM to invent a tactical conclusion.

    ``CANDIDATE`` 只保留「本场尚未真正取到该场次数据」的来源。已确认取到的来源
    在构造后按实际结果回写成 AVAILABLE / UNAVAILABLE，避免账本恒为 CANDIDATE 而
    沦为纯意图清单。

    注意 ``official_lineups`` 刻意保持 CANDIDATE：fixtures/lineups 当前返回的并
    非本场数据，不能算作已核验来源。
    """
    now = datetime.now().isoformat()
    # official_fixture 已移除：本场比赛信息实际来自本地 fixtures 表，与
    # source-local-fixtures 是同一个数据源，保留一条只会让账本重复计数。
    # fbref / understat 已移除：两者登记的分别是「队名搜索页」与「联赛整赛季页」，
    # 无法解析到单场 fixture，属永远无法核验的来源，留作 CANDIDATE 等于名不副实，
    # 故整条删除。它们的战术数据角色由 flashscore(每场 stats 页回填) 接管。
    candidates = [
        ("official_lineups", f"API-Football fixtures/lineups?fixture={fixture['id']}"),
        ("flashscore", "fixture_statistics:passes_final_third ← Flashscore .../summary/stats/overall/"),
        ("club_news", f"https://www.google.com/search?q={fixture['home_name']}+{fixture['away_name']}+team+news"),
    ]
    ledger = [{"id": f"source-{i + 1}", "kind": kind, "query_or_url": url,
               "fetched_at": now, "status": "CANDIDATE", "tier": 1 if kind.startswith("official") else 2}
              for i, (kind, url) in enumerate(candidates)]
    ledger.append({"id": "source-local-fixtures", "kind": "local_verified_history",
                   "query_or_url": "fixtures/fixture_statistics", "fetched_at": now,
                   "status": "AVAILABLE", "tier": 1})

    # 回写真实取数结果。club_news 由 Firecrawl 情报抓取落地，其实际检索词与该条
    # 示意 URL 不同，但来源归属一致。official_injuries 已移除：本地无 injuries
    # 数据、fixtures/players 也已停止同步，该来源永远无法解析。
    resolved = {"club_news": intel_status}
    for entry in ledger:
        if entry["kind"] in resolved:
            entry["status"] = resolved[entry["kind"]]

    updates: list[dict] = []
    risks: list[str] = []
    # Rest is completely reproducible from locally stored, completed matches.
    for side, team_id in (("home", fixture["home_id"]), ("away", fixture["away_id"])):
        row = db.execute(text("""
            SELECT date FROM fixtures WHERE (home_id=:team OR away_id=:team)
              AND date < :kickoff AND status_short IN ('FT','AET','PEN')
            ORDER BY date DESC LIMIT 1
        """), {"team": team_id, "kickoff": fixture["date"]}).fetchone()
        if not row or not row[0] or not fixture.get("date"):
            updates.append({"type": "schedule_density", "side": side, "status": "UNAVAILABLE",
                            "reason": "缺少上一场已完赛时间", "source_ids": ["source-local-fixtures"]})
            continue
        rest_days = max(0.0, (fixture["date"] - row[0]).total_seconds() / 86400)
        status = "AVAILABLE"
        delta = -0.035 if rest_days < 3 else (-0.015 if rest_days < 4 else 0.0)
        updates.append({"type": "schedule_density", "side": side, "status": status,
                        "delta": delta, "cap": 0.035, "rest_days": round(rest_days, 2),
                        "reason": "短休赛程" if delta else "休息时间正常",
                        "source_ids": ["source-local-fixtures"]})
        if delta:
            risks.append(f"{('主队' if side == 'home' else '客队')}短休，体能不确定性上升")

    # Confirmation changes confidence, but it is not assumed to be a positive
    # or negative team-strength shock without player-role evidence.
    confirmed = "确认首发:" in (lineups_text or "")
    updates.append({"type": "lineup_confirmation", "status": "AVAILABLE" if confirmed else "UNAVAILABLE",
                    "side": "draw", "delta": 0.0, "cap": 0.0,
                    "reason": "已取得确认首发" if confirmed else "未取得确认首发，不能假设最强阵容",
                    "source_ids": ["source-1"]})
    if not confirmed:
        risks.append("首发未确认；核心轴线伤停和轮换影响只能降级处理")

    # 无效控球(possession trap): 控球占比 − Field Tilt(进攻三区传球占比) = 效力差。
    # 效力差显著为正 = 球权集中在中后场 = 无效控球。
    # Field Tilt 来自 Flashscore 的 passes_final_third, 控球率来自 API-Football 的
    # Ball Possession。窗口内样本不齐一律 UNAVAILABLE, 不用射门/角球做代理近似。
    for side, team_id in (("home", fixture["home_id"]), ("away", fixture["away_id"])):
        ft = _recent_field_tilt_gap(db, team_id, fixture.get("date"))
        if ft["status"] != "AVAILABLE":
            updates.append({"type": "possession_trap", "status": "UNAVAILABLE", "side": side,
                            "reason": f"{ft['reason']}；因此不以单独控球率推断无效控球",
                            "source_ids": ["source-2", "source-local-fixtures"]})
            continue
        gap = ft["gap"]
        trapped = gap >= FIELD_TILT_GAP_TRIGGER
        updates.append({
            "type": "possession_trap", "status": "AVAILABLE", "side": side,
            "delta": -FIELD_TILT_CAP if trapped else 0.0,
            "cap": FIELD_TILT_CAP, "gap": gap, "samples": ft["samples"],
            "reason": (f"控球占比高于三区传球占比 {gap:.1%}，控球多为无效控球"
                       if trapped else
                       f"控球占比与三区传球占比差 {gap:.1%}，未触发无效控球"),
            "source_ids": ["source-2", "source-local-fixtures"],
        })
        if trapped:
            risks.append(
                f"{'主队' if side == 'home' else '客队'}控球偏向中后场，"
                f"控球优势未转化为进攻三区压力")
    updates.append({
        "type": "style_matchup", "status": "UNAVAILABLE", "side": None,
        "reason": ("无经核验的身高/回追/出球弱点对位数据：球员属性仅有 players 表历史快照，"
                   "无法构建本场对位指标；原先引用的 fbref/understat 搜索/联赛页不能按场解析，"
                   "已从来源账本中删除"),
        "source_ids": [],
    })
    return ledger, updates, risks


def _competition_context(fixture: dict) -> str:
    league_name = str(fixture.get("league_name") or "")
    round_name = str(fixture.get("round") or "")
    text = f"{league_name} {round_name}".lower()

    if "world cup" in text or "世界杯" in text:
        kind = "世界杯/国家队正赛"
    elif any(k in text for k in ("qualifier", "qualification", "预选")):
        kind = "国家队预选赛"
    elif any(k in text for k in ("friendlies", "friendly", "友谊")):
        kind = "友谊赛"
    elif any(k in text for k in ("u23", "u21", "u20", "u19")):
        kind = "青年队赛事"
    elif any(k in text for k in ("women", "女足")):
        kind = "女足赛事"
    elif any(k in text for k in ("cup", "杯", "fifa", "euro", "copa", "olympic", "奥运")):
        kind = "杯赛/锦标赛"
    else:
        kind = ""

    if kind:
        return (
            f"本场属于{kind}。"
            "这类比赛样本少、轮换多、战意和赛制影响大，赔率市场不可作为主要判断依据。"
        )
    return (
        "本场是常规联赛。赔率仍只能作为市场情绪和风险提示，"
        "最终判断应优先依据球队状态、主客场、积分背景和近10场双方比赛历史。"
    )


def _build_llm_prompt(fixture: dict, xgb_result: dict, odds_text: str,
                      home_stats: str, away_stats: str,
                      home_standings: str, away_standings: str,
                      lineups_text: str, weather_text: str = "",
                      intelligence_markdown: str = "",
                      bayes_context: dict | None = None) -> str:
    pw = xgb_result
    top3_str = '  '.join(f"{t['score']}({t['prob']:.0%})" for t in pw['top3'])

    competition_context = _competition_context(fixture)
    bayes_context = bayes_context or {}
    evidence_text = json.dumps(bayes_context.get("evidence", []), ensure_ascii=False)

    # XGBoost 先验（未经贝叶斯修正）：与 P0/P1 区分。
    # 注意 pw(=xgb_result) 的胜平负已被 P1 覆盖，不能当作先验。
    xgb_prior = bayes_context.get("xgb_prior") or {}
    model_probs = {'主胜': xgb_prior.get('home'), '平局': xgb_prior.get('draw'), '客胜': xgb_prior.get('away')}
    if all(v is not None for v in model_probs.values()):
        xgb_win_text = f"主{model_probs['主胜']:.0%} 平{model_probs['平局']:.0%} 客{model_probs['客胜']:.0%}"
        _top = max(model_probs, key=model_probs.get)
        model_top_text = f"{_top}({model_probs[_top]:.0%})"
    else:
        xgb_win_text = "暂无"
        model_top_text = "暂无"
    lambda_text = f"主{pw['lambda_home']:.2f} 客{pw['lambda_away']:.2f}"

    # Elo 段落预先拼好（f-string 里不嵌多行三元）：team_elos 缺队时显式写明暂无，
    # 避免模型在拿不到的项上自行臆造实力差。
    elo_meta = bayes_context.get("elo") or {}
    if elo_meta.get("status") == "AVAILABLE" and elo_meta.get("home_elo") is not None \
            and elo_meta.get("away_elo") is not None:
        elo_gap = float(elo_meta["home_elo"]) - float(elo_meta["away_elo"])
        elo_section = (
            f"{elo_meta.get('home_name') or fixture.get('home_name', '')} "
            f"{float(elo_meta['home_elo']):.1f} / "
            f"{elo_meta.get('away_name') or fixture.get('away_name', '')} "
            f"{float(elo_meta['away_elo']):.1f}｜分差 {elo_gap:+.1f}（正=主队更高）"
            f"｜主场加成 {elo_meta.get('home_advantage')}"
        )
    else:
        elo_section = f"暂无（{elo_meta.get('reason') or 'team_elos 无该队评分'}）"

    # 赔率段落预先拼好：f-string 里不要再嵌多行三元表达式（可读性差且易写成
    # `if x:` 的语句语法导致 SyntaxError）。无赔率（含纯空白）时显式告知模型，
    # 避免它在没有市场定价依据的情况下臆造让球/大小球盘口，此时要求用 "-" 占位。
    odds_section = (
        "【市场赔率】各庄家逐日行情(1X2 / 亚盘 / 大小球)，括号内为原始赔率、前面为隐含概率:" + odds_text
        if (odds_text or "").strip()
        else '【市场赔率】暂无，不用预测盘口及大小球，对应的数据输出用 "-" 替代'
    )

    # 分析方法与最终输出由 codex 技能接管：技能负责推演过程，但最终回复只交付 JSON。
    skill_name = (settings.codex_skill_name or "").strip()

    return f"""# 【比赛信息】
    {fixture.get('home_name','')} vs {fixture.get('away_name','')}
    联赛:{fixture.get('league_name','')} 赛季:{fixture.get('season','')}
    赛事属性:{competition_context}
    阵容信息:{lineups_text}
    比赛场地:{fixture.get('venue_city','')}
    比赛地天气及海拔:{weather_text}

    # 【外部比赛情报】情报仅供参考。
    {intelligence_markdown}

    # 核心数据输入（请基于以下信息进行推理）

    *【{fixture['home_name']} 数据】
    积分榜: {home_standings}
    近10场战绩: {home_stats}

    *【{fixture['away_name']} 数据】
    积分榜: {away_standings}
    近10场战绩: {away_stats}

    *【赔率数据】
    {odds_section}

    *【贝叶斯结果】P0/P1 为宿主已计算的输入，原样引用、不得伪造或杜撰盘口；你的 win/win_pct 是独立判断，可与 P1 不同但需说明依据
    P0: {json.dumps(bayes_context.get('p0', {}), ensure_ascii=False)}
    P1: {json.dumps(bayes_context.get('p1', {}), ensure_ascii=False)}
    风险: {json.dumps(bayes_context.get('risks', []), ensure_ascii=False)}
    证据账本: {evidence_text}

    *【Elo 评分】球队长期实力的客观标尺，用于检验市场定价与模型结论是否偏离基本面
    {elo_section}

    *【机器模型数据】XGBoost 原始输出（先验与参数，未经贝叶斯修正），仅供校准参考
    胜平负先验: {xgb_win_text}
    预期进球λ: {lambda_text}（用于泊松派生市场）
    让球参考:{pw['handicap']}（仅 XGBoost 参考，handicap_num 以赔率盘口为准）  大小球参考:{pw['over25_prob']:.0%}大球
    Top3比分参考: {top3_str}
    模型最高项:{model_top_text}

    # 分析方法（技能）
    - 请使用 ${skill_name} 技能的宿主路径（路径 B）完成分析：宿主已注入 P0/P1/λ/赔率等数据，只做派生市场、稳健性网格、情报对比与输出，不要重算 P0/P1，可用该技能的 scripts 辅助计算。

    # output-schema
    输出约束,请严格输出JSON格式:
    {{"win":"主胜|平局|客胜",
    "win_pct":"win 所选项的概率（win=主胜则主胜概率），如85%",
    "score":"三个最可能比分用逗号分隔如2-1,1-1,3-0",
    "handicap_num":"让球盘口线，负数=主队让，正数=客队让，如-1=主让1球（必须取已提供赔率中的盘口）",
    "handicap_team":"建议下注的一方：主队或客队, 填你判断有赢盘价值的一方",
    "handicap_pct":"handicap_team 这一方的赢盘概率，如65%",
    "ou_line":"大小球线，如2.5（必须取已提供赔率中的盘口）",
    "ou_type":"大或小","ou_pct":"ou_type 所选项的概率（大=大球概率，小=小球概率），如60%",
    "deep_report":"500字内的深度分析"}}
    """


class PredictionDataError(Exception):
    """数据缺失/不足：比赛不存在、特征缺失或模型缺失，属于「数据问题」而非模型/外部服务问题。"""
    pass


class PredictionLLMError(Exception):
    """LLM 校验失败：模型未返回完整/合规的预测结果（解析失败或字段缺失）。"""
    pass


def predict_fixture(fixture_id: int, db=None) -> dict:
    own_db = db is None
    if own_db:
        db = SessionLocal()

    try:
        # 1. 读取比赛
        row = db.execute(text("""
            SELECT f.id, f.home_id, f.away_id, f.league_id, f.season,
                   f.date, f.timestamp, f.goals_home, f.goals_away,
                   f.home_name, f.away_name, f.home_logo, f.away_logo,
                   f.league_name, f.venue_name, f.venue_city,
                   COALESCE(NULLIF(f.venue_city, ''), NULLIF(v.city, '')) AS weather_city
            FROM fixtures f
            LEFT JOIN venues v ON v.id = f.venue_id
            WHERE f.id = :fid
        """), {"fid": fixture_id}).fetchone()
        if not row:
            raise PredictionDataError(f"比赛不存在 fixture_id={fixture_id}")
        fixture = dict(row._mapping)

        # 2. 赔率（尽早抓取并落库，独立于后续特征/模型/LLM 是否成功，
        #        确保点击「预测」即把可用赔率写入 odds 表）
        odds = _fetch_odds(fixture_id, fixture.get('date'))
        if not (odds and odds.get("odds_data")):
            # api-football 无赔率（联赛未覆盖 / 配额耗尽等），回退爬取 Flashscore
            odds = _fetch_flashscore_odds(fixture)
        if not (odds and odds.get("odds_data")):
            raise PredictionDataError("未获取到可用赔率，无法生成让球和大小球预测")
        odds_text = _odds_to_text(odds["odds_data"]) if odds and odds.get("odds_data") else ""
        if odds and odds.get("odds_data"):
            try:
                _save_odds(db, fixture_id, odds)
            except Exception as e:
                logger.error(f"赔率保存失败 fixture={fixture_id}: {e}")

        # 3. 特征 + XGBoost
        feat = extract_features_for_fixture(db, fixture)
        if feat is None:
            raise PredictionDataError("特征数据不足，无法构建预测特征（历史/阵容等数据缺失）")

        feature_cols = _load_feature_cols()
        models, model_group = _load_best_models(fixture['league_id'])
        if models is None:
            raise PredictionDataError(f"未加载到可用模型（league_id={fixture['league_id']}，请先训练/导入模型）")

        X = pd.DataFrame([feat])[feature_cols]
        X_filled = _fill_na(X)
        win_probs = models['win'].predict_proba(X_filled)[0]
        over25_prob = float(models['over25'].predict_proba(X_filled)[0][1])
        lambda_home = float(models['lambda_home'].predict(X_filled)[0])
        lambda_away = float(models['lambda_away'].predict(X_filled)[0])
        top3 = _poisson_top3(lambda_home, lambda_away)

        # 让球: 最低档0.5球, 无"平手"
        diff = lambda_home - lambda_away
        ad = abs(diff)
        if ad < 0.75:
            val = "0.5"
        elif ad < 1.25:
            val = "1"
        elif ad < 1.75:
            val = "1.5"
        else:
            val = "2"
        hc = f"{'主让' if diff > 0 else '客让'}{val}球"

        xgb_result = {
            'win_home': float(win_probs[2]),
            'win_draw': float(win_probs[1]),
            'win_away': float(win_probs[0]),
            'over25_prob': over25_prob,
            'top3': top3,
            'lambda_home': lambda_home,
            'lambda_away': lambda_away,
            'handicap': hc,
        }

        # 4. 全量数据
        home_stats = _fetch_team_recent_via_db(db, fixture['home_id'], fixture['date'])
        away_stats = _fetch_team_recent_via_db(db, fixture['away_id'], fixture['date'])
        home_standings = _fetch_standings_text(db, fixture['home_id'], fixture['league_id'], fixture['season'])
        away_standings = _fetch_standings_text(db, fixture['away_id'], fixture['league_id'], fixture['season'])
        lineups_text = _fetch_lineups_text(fixture_id, fixture['home_id'], fixture['away_id'])
        weather_text = _fetch_weather_text(fixture.get('weather_city'), fixture.get('timestamp'))
        intelligence_markdown, intel_status = _fetch_intelligence_markdown(fixture)

        # 4.5 Auditable Bayesian layer.  XGBoost is retained as an informed
        # prior, while only bounded, recorded evidence is allowed to alter it.
        xgb_prior = {
            "home": xgb_result["win_home"], "draw": xgb_result["win_draw"],
            "away": xgb_result["win_away"],
        }
        elo_prior, elo_meta = team_elo_prior(db, fixture)
        p0 = blend_prior(elo_prior, xgb_prior) if elo_prior else xgb_prior
        evidence, updates, risks = _bayes_evidence(db, fixture, lineups_text, intel_status)
        if elo_prior is None:
            risks.append("team_elos 缺少球队 Elo；P0 已降级为 XGBoost 先验")
        p1, applied_updates = apply_updates(p0, updates)
        market_check = market_divergence(odds.get("odds_data", []) if odds else [], p1)
        if market_check.get("risk"):
            risks.append(market_check["risk"])
        if market_check.get("status") != "AVAILABLE":
            risks.append("缺少完整市场赔率；盘口相关结论已降级")
        bayes_context = {
            "version": BAYES_VERSION,
            "p0": p0,
            "p1": p1,
            "evidence": evidence,
            "updates": applied_updates,
            "risks": risks,
            "elo": elo_meta,
            "xgb_prior": xgb_prior,
            "market_check": market_check,
        }
        # Existing public fields remain compatible but now expose final P1.
        xgb_result["win_home"] = p1["home"]
        xgb_result["win_draw"] = p1["draw"]
        xgb_result["win_away"] = p1["away"]

        # 5. LLM
        llm_result = None
        llm_model = None
        try:
            prompt = _build_llm_prompt(fixture, xgb_result, odds_text,
                                       home_stats, away_stats,
                                       home_standings, away_standings,
                                       lineups_text, weather_text,
                                       intelligence_markdown, bayes_context)
            llm_result, llm_model = _call_llm(prompt)
        except CodexError:
            # codex 配置/环境错误（如 schema 文件缺失）不是可重试的模型故障，
            # 不能降级成 PredictionLLMError（否则会被当成"重试一下就好"）
            raise
        except Exception as e:
            logger.debug("LLM失败: {}", e)
        if llm_result is None:
            logger.warning(f"预测失败 fixture={fixture_id}: LLM 未返回完整盘口预测，跳过入库")
            raise PredictionLLMError("LLM 未返回完整/合规的让球或大小球预测结果（可重试）")

        # 贝叶斯到此为止：它只负责修正 XGBoost（已写回 xgb_result），不再反向
        # 覆盖 LLM 的推导。win_* 落的是贝叶斯修正后的模型概率，llm_win/llm_score/
        # llm_win_pct 落的是 LLM 自己的判断，两者并排可直接对照差异。
        logger.info("贝叶斯修正胜平负: P0(elo+xgb)=%s -> P1(证据后验)=%s", p0, p1)

        # 6. 写库（外部情报只注入 prompt 与本次返回，不再落库）
        _save_prediction(db, fixture, xgb_result, llm_result, odds, model_group,
                         bayes_context,
                         llm_model=llm_model)

        # 近10场原始战绩仅本次返回给前端核对，不落库
        result = {
            **xgb_result,
            'llm': {**llm_result, 'home_stats': home_stats, 'away_stats': away_stats},
            'weather': weather_text,
            'intelligence_markdown': intelligence_markdown,
            'bayes': bayes_context,
            'model_group': model_group,
        }
        logger.info(
            f"预测完成 fixt={fixture_id} "
            f"{fixture['home_name']} vs {fixture['away_name']} "
            f"LLM:{llm_result.get('win','-') if llm_result else '-'}"
        )
        return result

    finally:
        if own_db:
            db.close()


def _save_prediction(db, fixture, xgb, llm, odds, model_group, bayes=None,
                     llm_model: str | None = None):
    """写入预测结果。

    贝叶斯概率（P0/P1/version）不落库：实测 P1 恒等于 P0，而 win_home/win_draw/
    win_away 存的本就是 P1，再存一份只是重复副本。但来源账本、证据更新、风险提示
    属于可审计信息，予以保留，便于事后复盘某次预测当时究竟依据了什么。

    llm_model 落的是实际出结论的那个预测模型的名字（codex:<模型名>）：
    主模型可用时是 prediction_llm_model，主模型失败改由备用模型出结果时是
    prediction_fallback_llm_model。只记名字，不记它的推导过程。

    外部情报不落库：它只注入预测 prompt 与本次接口返回；历史行里的
    intelligence_markdown 保留不动。
    """
    llm = llm or {}

    def _j(value):
        """序列化为 JSON；无值时写 NULL，避免把字面量 "null" 存进列里。"""
        return json.dumps(value, ensure_ascii=False) if value is not None else None

    db.execute(text("""
        INSERT INTO predictions (
            fixture_id, home_name, away_name, home_logo, away_logo,
            league_name, match_date, model_group,
            win_home, win_draw, win_away, over25_prob,
            top3_scores, lambda_home, lambda_away, handicap,
            llm_win, llm_score, llm_win_pct,
            llm_deep_report,
            llm_handicap_num, llm_handicap_team, llm_handicap_pct,
            llm_ou_line, llm_ou_type, llm_ou_pct, llm_model,
            bayes_updates, bayes_evidence, bayes_risk,
            created_at, updated_at
        ) VALUES (
            :fid, :hname, :aname, :hlogo, :alogo,
            :lname, :mdate, :mgroup,
            :wh, :wd, :wa, :o25,
            :top3, :lh, :la, :hc,
            :lw, :ls, :lwp,
            :ldr,
            :hcn, :hct, :hcp,
            :oun, :out, :oup, :lmodel,
            :bupdates, :bevidence, :brisk,
            :now, :now
        )
        ON DUPLICATE KEY UPDATE
            win_home=:wh, win_draw=:wd, win_away=:wa, over25_prob=:o25,
            top3_scores=:top3, lambda_home=:lh, lambda_away=:la, handicap=:hc,
            llm_win=:lw, llm_score=:ls, llm_win_pct=:lwp,
            llm_deep_report=:ldr,
            llm_handicap_num=:hcn, llm_handicap_team=:hct, llm_handicap_pct=:hcp,
            llm_ou_line=:oun, llm_ou_type=:out, llm_ou_pct=:oup,
            llm_model=COALESCE(:lmodel, llm_model),
            bayes_updates=:bupdates, bayes_evidence=:bevidence, bayes_risk=:brisk,
            model_group=:mgroup, updated_at=:now
    """), {
        'fid': fixture['id'],
        'hname': fixture['home_name'], 'aname': fixture['away_name'],
        'hlogo': fixture.get('home_logo'), 'alogo': fixture.get('away_logo'),
        'lname': fixture['league_name'], 'mdate': fixture['date'],
        'mgroup': model_group,
        'wh': xgb['win_home'], 'wd': xgb['win_draw'], 'wa': xgb['win_away'],
        'o25': xgb['over25_prob'],
        'top3': json.dumps(xgb['top3']),
        'lh': xgb['lambda_home'], 'la': xgb['lambda_away'],
        'hc': xgb['handicap'],
        'lw': llm.get('win'), 'ls': llm.get('score'), 'lwp': llm.get('win_pct'),
        'ldr': llm.get('deep_report'),
        'hcn': llm.get('handicap_num'), 'hct': llm.get('handicap_team'), 'hcp': llm.get('handicap_pct'),
        'oun': llm.get('ou_line'), 'out': llm.get('ou_type'), 'oup': llm.get('ou_pct'),
        'bupdates': _j((bayes or {}).get('updates')),
        'bevidence': _j((bayes or {}).get('evidence')),
        'brisk': _j({"risks": (bayes or {}).get('risks', []),
                     "market": (bayes or {}).get('market_check')}) if bayes else None,
        'lmodel': llm_model,
        'now': datetime.now(),
    })
    db.commit()


def _canonical_odds(obj) -> str:
    """将赔率结构规范化为可比较字符串（排序键 + 关闭 ensure_ascii）。

    用于快照去重时规避「键顺序不同」或「浮点文本表示略有差异」造成的误判：
    只要语义内容一致就判定为同一快照。
    """
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def _save_odds(db, fixture_id: int, odds_result: dict) -> None:
    """将赔率数据快照写入 odds 表（快照去重 + 复合索引）。

    策略:
    - 仅当本次抓取结果与同一 fixture 最近一次快照内容**不一致**时才插入新行,
      内容相同则跳过。既保留赔率变动历史,又避免每次点击都写入一份完全相同的大
      JSON 导致表无谓膨胀。
    - 建表时建立复合索引 (fixture_id, created_at),支撑「按 fixture 取最新/历史」
      的高效回查。
    """
    odds_data = odds_result.get("odds_data")
    if odds_data is None:
        # 无有效赔率,不写入空快照
        return

    now = datetime.now()
    # 兼容旧表结构（fixture_id 为主键的覆盖式表）：检测到旧结构则重建为追加式
    try:
        if db.execute(text("SHOW TABLES LIKE 'odds'")).fetchone():
            if not db.execute(text("SHOW COLUMNS FROM odds LIKE 'id'")).fetchone():
                db.execute(text("DROP TABLE odds"))
                db.commit()
    except Exception:
        pass
    # 确保表存在（追加式 + 复合索引）
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS odds (
            id INT AUTO_INCREMENT PRIMARY KEY,
            fixture_id INT NOT NULL,
            odds_data JSON,
            created_at DATETIME,
            INDEX ix_odds_fixture_created (fixture_id, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """))

    # 去重：比较本次快照与最近一次快照的规范 JSON 是否一致
    try:
        last = db.execute(text(
            "SELECT odds_data FROM odds WHERE fixture_id = :fid "
            "ORDER BY created_at DESC, id DESC LIMIT 1"
        ), {"fid": fixture_id}).fetchone()
        if last is not None:
            prev = last[0]
            if isinstance(prev, (str, bytes, bytearray)):
                try:
                    prev = json.loads(prev)
                except Exception:
                    prev = None
            if _canonical_odds(prev) == _canonical_odds(odds_data):
                # 内容无变化,跳过插入
                return
    except Exception:
        # 读不到旧数据（如表刚建）则直接插入
        pass

    db.execute(text("""
        INSERT INTO odds (fixture_id, odds_data, created_at)
        VALUES (:fid, :odata, :now)
    """), {
        "fid": fixture_id,
        "odata": json.dumps(odds_data, ensure_ascii=False),
        "now": now,
    })
    db.commit()
