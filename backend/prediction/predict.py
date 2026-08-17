"""
推理入口:给定 fixture_id,执行完整预测流程并写入 predictions 表.

流程:
  1. 从 DB 读取比赛信息
  2. 提取特征 → XGBoost 推理(λ, Top3, 参考概率)
  3. 赔率API(实时)
  4. LLM(全量数据 + XGBoost 参考) 最终决策
  5. 写入 predictions 表
"""

import os
import sys
import json
import pickle
import re
from datetime import datetime
from typing import Any

import numpy as np
from scipy.stats import poisson
from loguru import logger

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from sqlalchemy import text
from app.db.session import SessionLocal
from app.core.config import settings
from prediction.features import extract_features_for_fixture
from prediction.training.model import load_models, _fill_na
from prediction.training.data import assign_group

import pandas as pd

MODELS_DIR = os.path.join(os.path.dirname(__file__), 'models')
FEATURE_COLS_PATH = os.path.join(MODELS_DIR, 'feature_cols.pkl')


# ── API-Football 请求节流（免费版 10 次/分钟）──
import time
from collections import deque
from threading import Lock

_API_RATE_LIMIT = 10          # 免费版每分钟最多请求次数
_API_RATE_WINDOW = 60.0       # 滚动时间窗（秒）
_api_timestamps: deque = deque()
_api_rate_lock = Lock()


def _throttle_api_football():
    """限制对 API-Football 的请求频率：任意 60 秒滚动窗口内最多 10 次。"""
    with _api_rate_lock:
        now = time.monotonic()
        # 清理已离开时间窗的时间戳
        while _api_timestamps and now - _api_timestamps[0] >= _API_RATE_WINDOW:
            _api_timestamps.popleft()
        # 窗口已满则等待最早的一次离开窗口
        if len(_api_timestamps) >= _API_RATE_LIMIT:
            wait = _API_RATE_WINDOW - (now - _api_timestamps[0]) + 0.1
            if wait > 0:
                time.sleep(wait)
        _api_timestamps.append(time.monotonic())


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
    """带节流地请求 API-Football 的指定接口，返回解析后的 JSON。"""
    _throttle_api_football()
    if not settings.api_football_base_url:
        logger.debug("api_football_base_url 未配置，跳过 API-Football 请求")
        return {}
    url = f"{settings.api_football_base_url.rstrip('/')}/{path.lstrip('/')}"
    resp = _http_with_deadline(
        "GET", url,
        headers={"x-apisports-key": settings.api_football_key},
        params=params, timeout=timeout, label="API-Football",
    )
    if resp is None:
        return {}
    try:
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"API-Football 响应解析失败: {e}")
        return {}
    # 即便 HTTP 200，接口也可能在 errors 字段返回限流/配额等错误（如当日请求数耗尽）
    if isinstance(data, dict) and data.get("errors"):
        _msg = data["errors"].get("requests") or data["errors"].get("message") or str(data["errors"])
        logger.warning(f"API-Football 返回错误 [{path}]: {_msg}")
    return data


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
    return out


def _call_llm(prompt: str, retries: int = 2) -> dict | None:
    # 多次重试：LLM 偶有返回非标准 JSON 或漏字段，重试可显著提升稳定性。
    for attempt in range(1, retries + 2):
        try:
            resp = _http_with_deadline(
                "POST",
                f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.deepseek_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 1200,
                    "enable_thinking": True
                },
                timeout=60.0,
                label="DeepSeek",
            )
            if resp is None:
                logger.warning("DeepSeek 请求失败或超时，无法生成 LLM 预测")
                return None
            resp.raise_for_status()
            message = resp.json()['choices'][0]['message']
            content = message.get('content') or ''
            # 兜底：部分推理模型把结果放在 reasoning_content
            if not content.strip() and message.get('reasoning_content'):
                content = message['reasoning_content']
            parsed = _parse_llm_json(content)
            if parsed and _has_required_llm_fields(parsed):
                return _normalize_llm_fields(parsed)
            logger.warning("LLM 返回缺少必要预测字段（第 {} 次）", attempt)
        except Exception as e:
            detail = str(e)
            if hasattr(e, 'response') and e.response is not None:
                try:
                    detail += f" | body: {e.response.text[:500]}"
                except Exception:
                    pass
            logger.warning("LLM 失败（第 {} 次）: {}", attempt, detail)
    return None


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
        "win", "win_pct", "score",
        "handicap_num", "handicap_team", "handicap_pct",
        "ou_line", "ou_type", "ou_pct",
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

    仅查询当前日期；每次调用都会按需请求最新赔率，不做 fixture 级缓存或调用次数限制。
    这里的 ``date`` 是赔率快照的发布日期，而不是比赛日期；未来比赛的赔率只能通过今天
    的快照查询。仅保留真正抓到赔率的日期。

    庄家筛选使用固定白名单（默认 ODDS_BOOKMAKER_WHITELIST，大小写不敏感），只保留
    白名单内的庄家，最终按白名单顺序输出；若某家在窗口内无数据则自动缺失。
    """
    try:
        from datetime import datetime
        whitelist = [b.strip() for b in (bookmaker_whitelist or ODDS_BOOKMAKER_WHITELIST) if b.strip()]
        wl_lower = {b.lower(): b for b in whitelist}  # 规范名（按白名单原样）
        # API-Football 的 date 表示赔率快照日期；始终查询当前日期，兼容未来比赛。
        ordered = [datetime.now().strftime("%Y-%m-%d")]

        # 收集所有庄家数据: {bookmaker_name: {date: entry}}
        bookmaker_odds = {}
        first_api_error = None  # 记录首个 API-Football 错误（如当日配额耗尽），用于向上游暴露
        for date_str in ordered:
            resp: dict[Any, Any] = _api_get("odds", {"fixture": fixture_id, "date": date_str})
            if isinstance(resp, dict) and resp.get("errors"):
                if first_api_error is None:
                    first_api_error = resp["errors"]
                continue  # 该日期无赔率数据
            data = resp.get("response", []) if isinstance(resp, dict) else []
            if not data:
                continue

            for bm in data[0].get("bookmakers", []):
                bm_name = bm.get("name", "未知")
                # 仅保留白名单内的庄家（大小写不敏感）
                key = bm_name.lower()
                if key not in wl_lower:
                    continue
                canon = wl_lower[key]
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
            return None
        return {"odds_data": odds_data}
    except Exception as e:
        logger.debug(f"赔率获取失败: {e}")
    return None


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


def _fetch_weather_text(city_name: str) -> str:
    """获取比赛城市的天气及海拔，转换为供 LLM 使用的文本。"""
    if not city_name:
        return "未提供比赛城市，无法获取天气及海拔数据。"
    if not settings.openweathermap_api_key:
        return "未配置天气 API key，无法获取天气及海拔数据。"

    try:
        from tools.weather import get_weather_and_elevation

        raw = get_weather_and_elevation(city_name, settings.openweathermap_api_key)
        data = json.loads(raw)
        if data.get("status") != "success":
            return "天气及海拔数据获取失败，不能据此推断外部环境。"

        location = data.get("location", {})
        weather = data.get("weather", {})
        elevation = location.get("elevation_meters")
        elevation_text = f"{elevation}米" if elevation is not None else "未知"
        weather_text = (
            f"城市:{location.get('city') or city_name} "
            f"天气:{weather.get('description', '未知')} "
            f"气温:{weather.get('temperature_celsius', '未知')}°C "
            f"体感:{weather.get('feels_like_celsius', '未知')}°C "
            f"最低/最高:{weather.get('temp_min', '未知')}/{weather.get('temp_max', '未知')}°C "
            f"湿度:{weather.get('humidity_percent', '未知')}% "
            f"气压:{weather.get('pressure_hpa', '未知')}hPa "
            f"风速:{weather.get('wind_speed_m_s', '未知')}m/s "
            f"海拔:{elevation_text}"
        )
        return weather_text
    except Exception:  # noqa: BLE001 - 外部环境数据不得阻断主预测流程
        return "天气及海拔数据处理失败，不能据此推断外部环境。"


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
                      lineups_text: str, weather_text: str = "") -> str:
    pw = xgb_result
    top3_str = '  '.join(f"{t['score']}({t['prob']:.0%})" for t in pw['top3'])

    model_probs = {'主胜': pw['win_home'], '平局': pw['win_draw'], '客胜': pw['win_away']}
    model_top = max(model_probs, key=model_probs.get)
    model_top_pct = model_probs[model_top]
    competition_context = _competition_context(fixture)

    return f"""# Role 
    你是一名顶级职业足球量化分析师与战术推演专家（Sharp Analyst）。你的目标是不受大众偏见和机构诱盘影响，基于底层数据、战术克制与市场定价偏差，寻找具有长期正期望值（+EV）的投注价值。

    # 【比赛信息】
    {fixture.get('home_name','')} vs {fixture.get('away_name','')}
    联赛:{fixture.get('league_name','')} 赛季:{fixture.get('season','')}
    赛事属性:{competition_context}
    阵容信息:{lineups_text}
    比赛场地:{fixture.get('venue_city','')}
    比赛地天气及海拔:{weather_text}

    # 核心数据输入（请基于以下信息进行推理）

    *【{fixture['home_name']} 数据】
    积分榜: {home_standings}
    近10场战绩: {home_stats}

    *【{fixture['away_name']} 数据】
    积分榜: {away_standings}
    近10场战绩: {away_stats}

    *【赔率数据】
    {"" if not odds_text else "【市场赔率】各庄家逐日行情(1X2 / 亚盘 / 大小球)，括号内为原始赔率、前面为隐含概率:" + odds_text}

    *【机器模型数据】仅作校准，不是结论
    胜平负: 主{pw['win_home']:.0%} 平{pw['win_draw']:.0%} 客{pw['win_away']:.0%}
    让球参考:{pw['handicap']}  大小球参考:{pw['over25_prob']:.0%}大球
    Top3比分参考: {top3_str}
    模型最高项:{model_top}({model_top_pct:.0%})

    # Analysis Workflow（分析流程）
    严格按照以下 5 个步骤逐步推演：

    ### 战意、背景与外部环境
    - 积分与战意：区分争冠、保级、争欧战、无欲无求或战略放弃。
    - 赛程疲劳度：计算近 15 天比赛密集度、多线作战轮换压力、长途旅行飞行距离。
    - 外部环境：结合比赛地当天的天气（降雨、大风、极端气温）和地理/海拔，评估对两队技术流打法或体能消耗的具体影响。

    ### 虚实辨析与底层数据
    - 重点看球队在相似战术风格对手面前创造的 Open-Play xG。

    ### 战术与对位克制
    - 阵型与打法对冲：如“高位逼抢 vs 后场长传脱困”、“边路传中 vs 禁区防空成功率”、“控球慢节奏 vs 快速反击”。
    - 关键伤停与阵容缺口：若无确定首发，严禁假设最强阵容；必须评估核心轴线（主力中卫/单后腰/核心射手）缺阵引发的战术坍塌风险。

    ### 赔率与市场去噪
    - 赔率本质是市场供需与平衡风险的工具，绝非真实概率。
    - 降权场景：世界杯/国家队、杯赛、友谊赛、青年队、女足或样本量 <5 场时，大幅降低赔率与历史对战的参考权重，主要依赖阵容与基本面。

    ### 价值评估与盘口定位
    - 依据上述分析，估算你心中的“真实合理盘口”。
    - 让球(handicap)：handicap_num，大小球(ou)：ou_line 取赔率中双方赔率最接近的盘口线。对比当前实际盘口（让球 handicap_num、大小球 ou_line）：
    - 寻找市场过热（Public Bias）导致的盘口让步过深或过浅。
    - 识别大小球盘口在极端天气、锋线伤停或保守战术下的价值偏差   

    # 请严格输出JSON格式:
    {{"win":"主胜|平局|客胜",
    "win_pct":"本场预测信心百分比,如85%",
    "score":"三个最可能比分用逗号分隔如2-1,1-1,3-0",
    "handicap_num":"让球数,负数=主队让,正数=客队让,如-1",
    "handicap_team":"主队或客队","handicap_pct":"让球方赢盘概率百分比,如65%",
    "ou_line":"大小球线如2.5(优先取赔率中最均衡盘口线)",
    "ou_type":"大或小",
    "ou_pct":"大小球概率百分比如60%",
    "brief_analysis":"一句话结论(20字内)",
    "core_data":"简述核心动机与赛程影响(100字内)",
    "deep_report":"深度分析(300字内)"}}
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
                   f.date, f.goals_home, f.goals_away,
                   f.home_name, f.away_name, f.home_logo, f.away_logo,
                   f.league_name, f.venue_name, f.venue_city
            FROM fixtures f WHERE f.id = :fid
        """), {"fid": fixture_id}).fetchone()
        if not row:
            raise PredictionDataError(f"比赛不存在 fixture_id={fixture_id}")
        fixture = dict(row._mapping)

        # 2. 赔率（尽早抓取并落库，独立于后续特征/模型/LLM 是否成功，
        #        确保点击「预测」即把可用赔率写入 odds 表）
        odds = _fetch_odds(fixture_id, fixture.get('date'))
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
        weather_text = _fetch_weather_text(fixture.get('venue_city'))

        # 5. LLM
        llm_result = None
        try:
            prompt = _build_llm_prompt(fixture, xgb_result, odds_text,
                                       home_stats, away_stats,
                                       home_standings, away_standings,
                                       lineups_text, weather_text)
            llm_result = _call_llm(prompt)
        except Exception as e:
            logger.debug(f"LLM失败: {e}")
        if llm_result is None:
            logger.warning(f"预测失败 fixture={fixture_id}: LLM 未返回完整预测，跳过入库")
            raise PredictionLLMError("LLM 未返回完整/合规的预测结果（解析失败或字段缺失）")

        # 6. 写库
        _save_prediction(db, fixture, xgb_result, llm_result, odds, model_group)

        # 近10场原始战绩仅本次返回给前端核对，不落库
        result = {
            **xgb_result,
            'llm': {**llm_result, 'home_stats': home_stats, 'away_stats': away_stats},
            'weather': weather_text,
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


def _save_prediction(db, fixture, xgb, llm, odds, model_group):
    llm = llm or {}
    db.execute(text("""
        INSERT INTO predictions (
            fixture_id, home_name, away_name, home_logo, away_logo,
            league_name, match_date, model_group,
            win_home, win_draw, win_away, over25_prob,
            top3_scores, lambda_home, lambda_away, handicap,
            llm_win, llm_score, llm_win_pct,
            llm_brief, llm_core_data, llm_deep_report,
            llm_handicap, llm_over_under,
            llm_handicap_num, llm_handicap_team, llm_handicap_pct,
            llm_ou_line, llm_ou_type, llm_ou_pct,
            created_at, updated_at
        ) VALUES (
            :fid, :hname, :aname, :hlogo, :alogo,
            :lname, :mdate, :mgroup,
            :wh, :wd, :wa, :o25,
            :top3, :lh, :la, :hc,
            :lw, :ls, :lwp,
            :lb, :lcd, :ldr,
            :lhc, :lou,
            :hcn, :hct, :hcp,
            :oun, :out, :oup,
            :now, :now
        )
        ON DUPLICATE KEY UPDATE
            win_home=:wh, win_draw=:wd, win_away=:wa, over25_prob=:o25,
            top3_scores=:top3, lambda_home=:lh, lambda_away=:la, handicap=:hc,
            llm_win=:lw, llm_score=:ls, llm_win_pct=:lwp,
            llm_brief=:lb, llm_core_data=:lcd, llm_deep_report=:ldr,
            llm_handicap=:lhc, llm_over_under=:lou,
            llm_handicap_num=:hcn, llm_handicap_team=:hct, llm_handicap_pct=:hcp,
            llm_ou_line=:oun, llm_ou_type=:out, llm_ou_pct=:oup,
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
        'lb': llm.get('brief_analysis'), 'lcd': llm.get('core_data'),
        'ldr': llm.get('deep_report'),
        'lhc': llm.get('handicap'), 'lou': llm.get('over_under'),
        'hcn': llm.get('handicap_num'), 'hct': llm.get('handicap_team'), 'hcp': llm.get('handicap_pct'),
        'oun': llm.get('ou_line'), 'out': llm.get('ou_type'), 'oup': llm.get('ou_pct'),
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
