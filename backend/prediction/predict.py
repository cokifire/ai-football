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
    """将模型可能返回的列表/数值字段规整为字符串，避免写入 TEXT 列时报错。"""
    for k, v in list(parsed.items()):
        if isinstance(v, list):
            parsed[k] = ",".join(str(x) for x in v)
        elif isinstance(v, (int, float)):
            parsed[k] = str(v)
    return parsed


def _call_llm(prompt: str) -> dict | None:
    try:
        import httpx
        resp = httpx.post(
            f"{settings.deepseek_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.deepseek_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.deepseek_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 1200,
                # 关闭推理模型的 thinking，使其直接输出最终 JSON（content 不再为空）
                "enable_thinking": False,
            },
            timeout=40.0,
        )
        resp.raise_for_status()
        message = resp.json()['choices'][0]['message']
        content = message.get('content') or ''
        # 兜底：部分推理模型把结果放在 reasoning_content
        if not content.strip() and message.get('reasoning_content'):
            content = message['reasoning_content']
        parsed = _parse_llm_json(content)
        if parsed and _has_required_llm_fields(parsed):
            return _normalize_llm_fields(parsed)
        logger.warning("LLM 返回缺少必要预测字段")
    except Exception as e:
        detail = str(e)
        if hasattr(e, 'response') and e.response is not None:
            try:
                detail += f" | body: {e.response.text[:500]}"
            except Exception:
                pass
        logger.warning(f"LLM 失败: {detail}")
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
    return all(str(data.get(k) or "").strip() for k in required)


def _fetch_odds(fixture_id: int) -> dict | None:
    """拉取近7天至少5个庄家的赔率变化"""
    try:
        import httpx
        from datetime import datetime, timedelta
        # 收集所有庄家数据: {bookmaker_name: {date: {home_odd, draw_odd, away_odd}}}
        bookmaker_odds = {}
        today = datetime.now()

        for day_offset in range(7):
            date_str = (today - timedelta(days=day_offset)).strftime("%Y-%m-%d")
            r = httpx.get(
                f"{settings.api_football_base_url}/odds",
                headers={"x-apisports-key": settings.api_football_key},
                params={"fixture": fixture_id, "date": date_str},
                timeout=10.0,
            )
            r.raise_for_status()
            data = r.json().get("response", [])
            if not data:
                continue

            for bm in data[0].get("bookmakers", []):
                bm_name = bm.get("name", "未知")
                if bm_name not in bookmaker_odds:
                    bookmaker_odds[bm_name] = {}
                for bet in bm.get("bets", []):
                    if bet.get("name") != "Match Winner":
                        continue
                    vals = {v["value"]: float(v["odd"]) for v in bet.get("values", [])}
                    ho, dr, aw = vals.get("Home"), vals.get("Draw"), vals.get("Away")
                    if not all([ho, dr, aw]):
                        continue
                    t = 1/ho + 1/dr + 1/aw
                    bookmaker_odds[bm_name][date_str] = {
                        "home_odd": round(1/ho / t, 3),
                        "draw_odd": round(1/dr / t, 3),
                        "away_odd": round(1/aw / t, 3),
                        "home_raw": ho, "draw_raw": dr, "away_raw": aw,
                    }
                    break

        if not bookmaker_odds:
            return None

        # 取数据最多的前5个庄家，组织为结构化数据（仅返回 odds_data）
        sorted_bms = sorted(bookmaker_odds.items(), key=lambda x: -len(x[1]))[:5]
        odds_data = []
        for bm_name, dates in sorted_bms:
            entries = []
            for day_offset in range(6, -1, -1):
                key = (today - timedelta(days=day_offset)).strftime("%Y-%m-%d")
                o = dates.get(key)
                if o:
                    entries.append({
                        "date": key,
                        "home_odd": o["home_odd"], "draw_odd": o["draw_odd"], "away_odd": o["away_odd"],
                        "home_raw": o["home_raw"], "draw_raw": o["draw_raw"], "away_raw": o["away_raw"],
                    })
                else:
                    entries.append({"date": key})
            odds_data.append({"bookmaker": bm_name, "entries": entries})

        return {"odds_data": odds_data}
    except Exception as e:
        logger.debug(f"赔率获取失败: {e}")
    return None


def _odds_to_text(odds_data: list) -> str:
    """将结构化赔率数据转换为可读文本，供 LLM 提示词使用。"""
    from datetime import datetime as _dt
    lines = []
    for bm in odds_data:
        lines.append(f"  {bm['bookmaker']}:")
        for e in bm['entries']:
            try:
                d = _dt.strptime(e['date'], "%Y-%m-%d").strftime("%m/%d")
            except Exception:
                d = e['date']
            if e.get('home_odd') is not None:
                lines.append(f"    {d}: 主{e['home_odd']:.0%}({e['home_raw']}) 平{e['draw_odd']:.0%}({e['draw_raw']}) 客{e['away_odd']:.0%}({e['away_raw']})")

    return "\n" + "\n".join(lines)


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
        import httpx
        r = httpx.get(
            f"{settings.api_football_base_url}/fixtures",
            headers={"x-apisports-key": settings.api_football_key},
            params={"team": team_id, "last": 10},
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json().get("response", [])
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


def _fetch_lineups_text(fixture_id: int, home_id: int, away_id: int) -> str:
    """拉取确认首发；未公布时返回阵容不确定提示。"""
    try:
        import httpx
        r = httpx.get(
            f"{settings.api_football_base_url}/fixtures/lineups",
            headers={"x-apisports-key": settings.api_football_key},
            params={"fixture": fixture_id},
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json().get("response", [])
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
        "最终判断应优先依据球队状态、主客场、积分背景和近5场双方比赛历史。"
    )


def _build_llm_prompt(fixture: dict, xgb_result: dict, odds_text: str,
                      home_stats: str, away_stats: str,
                      home_standings: str, away_standings: str,
                      lineups_text: str) -> str:
    pw = xgb_result
    top3_str = '  '.join(f"{t['score']}({t['prob']:.0%})" for t in pw['top3'])

    model_probs = {'主胜': pw['win_home'], '平局': pw['win_draw'], '客胜': pw['win_away']}
    model_top = max(model_probs, key=model_probs.get)
    model_top_pct = model_probs[model_top]
    competition_context = _competition_context(fixture)

    return f"""你是一位专业足球分析师.请对以下比赛进行独立分析并给出最终预测.

    【比赛信息】
    {fixture['home_name']} vs {fixture['away_name']}
    联赛:{fixture['league_name']} 赛季:{fixture['season']}
    赛事属性:{competition_context}
    阵容信息:{lineups_text}

    【{fixture['home_name']} 完整数据】
    积分榜: {home_standings}
    近10场战绩: {home_stats}

    【{fixture['away_name']} 完整数据】
    积分榜: {away_standings}
    近10场战绩: {away_stats}

    {"" if not odds_text else "【市场赔率】" + odds_text}

    【机器模型参考】仅作校准，不是结论
    胜平负: 主{pw['win_home']:.0%} 平{pw['win_draw']:.0%} 客{pw['win_away']:.0%}
    让球参考:{pw['handicap']}  大小球参考:{pw['over25_prob']:.0%}大球
    Top3比分参考: {top3_str}
    模型最高项:{model_top}({model_top_pct:.0%})

    【重要规则】
    1. 必须先基于球队状态、对手含金量、主客场表现、积分背景、赛事属性和赛程动机独立判断，再参考机器模型做校准。
    2. 不要因为模型最高项是{model_top}就默认选择它；如果球队数据、赛程背景或赔率信号不支持，应选择其他结果。
    3. 重点检查平局和冷门可能性，不要为了迎合模型概率而排除低概率但合理的结果。
    4. 模型概率只表示历史数据下的统计参考，不能替代你的最终判断。
    5. 赔率只代表市场价格和热度，不代表真实胜率；世界杯、杯赛、国家队、友谊赛、青年队、女足或样本不足时，赔率权重必须降低。
    6. 世界杯/国家队比赛若没有确认首发，不要假设固定阵容；必须考虑轮换、伤停、战术调整、体能和临场动机，并降低置信度。
    7. 置信度要反映真实不确定性；数据冲突、杯赛、友谊赛、世界杯、阵容未确认或样本不足时应降低置信度。

    请严格输出JSON格式:
    {{"win":"主胜|平局|客胜","win_pct":"本场预测信心百分比,如85%","score":"三个最可能比分用逗号分隔如2-1,1-1,3-0","handicap_num":"让球数,负数=主队让,正数=客队让,如-1","handicap_team":"主队或客队","handicap_pct":"让球方赢盘概率百分比,如65%","ou_line":"大小球线如2.5","ou_type":"大或小","ou_pct":"大小球概率百分比如60%","brief_analysis":"一句话结论(20字内)","core_data":"主客队数据对比(100字内)","deep_report":"攻防对比/数据支撑(200字内)"}}"""


def predict_fixture(fixture_id: int, db=None) -> dict | None:
    own_db = db is None
    if own_db:
        db = SessionLocal()

    try:
        # 1. 读取比赛
        row = db.execute(text("""
            SELECT f.id, f.home_id, f.away_id, f.league_id, f.season,
                   f.date, f.goals_home, f.goals_away,
                   f.home_name, f.away_name, f.home_logo, f.away_logo,
                   f.league_name
            FROM fixtures f WHERE f.id = :fid
        """), {"fid": fixture_id}).fetchone()
        if not row:
            return None
        fixture = dict(row._mapping)

        # 2. 特征 + XGBoost
        feat = extract_features_for_fixture(db, fixture)
        if feat is None:
            return None

        feature_cols = _load_feature_cols()
        models, model_group = _load_best_models(fixture['league_id'])
        if models is None:
            return None

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

        # 3. 赔率
        odds = _fetch_odds(fixture_id)
        odds_text = _odds_to_text(odds["odds_data"]) if odds and odds.get("odds_data") else ""

        # 4. 全量数据
        home_stats = _fetch_team_recent_via_api(fixture['home_id'])
        away_stats = _fetch_team_recent_via_api(fixture['away_id'])
        home_standings = _fetch_standings_text(db, fixture['home_id'], fixture['league_id'], fixture['season'])
        away_standings = _fetch_standings_text(db, fixture['away_id'], fixture['league_id'], fixture['season'])
        lineups_text = _fetch_lineups_text(fixture_id, fixture['home_id'], fixture['away_id'])

        # 5. LLM
        llm_result = None
        try:
            prompt = _build_llm_prompt(fixture, xgb_result, odds_text,
                                       home_stats, away_stats,
                                       home_standings, away_standings,
                                       lineups_text)
            llm_result = _call_llm(prompt)
        except Exception as e:
            logger.debug(f"LLM失败: {e}")
        if llm_result is None:
            logger.warning(f"预测失败 fixture={fixture_id}: LLM 未返回完整预测，跳过入库")
            return None

        # 6. 写库
        _save_prediction(db, fixture, xgb_result, llm_result, odds, model_group)

        result = {**xgb_result, 'llm': llm_result, 'model_group': model_group}
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


def _save_odds(db, fixture_id: int, odds_result: dict) -> None:
    """将赔率数据写入 odds 表，每次点击都覆盖旧数据（仅保存 odds_data）。"""
    odds_data = odds_result.get("odds_data")
    now = datetime.now()
    # 确保表存在（与现有 raw SQL 管理表的方式保持一致）
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS odds (
            fixture_id INT PRIMARY KEY,
            odds_data JSON,
            updated_at DATETIME
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """))
    db.execute(text("""
        INSERT INTO odds (fixture_id, odds_data, updated_at)
        VALUES (:fid, :odata, :now)
        ON DUPLICATE KEY UPDATE
            odds_data = :odata,
            updated_at = :now
    """), {
        "fid": fixture_id,
        "odata": json.dumps(odds_data, ensure_ascii=False),
        "now": now,
    })
    db.commit()



