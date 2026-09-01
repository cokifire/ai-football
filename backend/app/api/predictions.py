"""预测相关 API 端点"""

import asyncio
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, Query, Depends
from loguru import logger
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.value_bet_service import compute_value_bets
from app.services.calibration_service import get_diagnostics
from prediction.predict import PredictionDataError, PredictionLLMError
from app.core.security import AdminAuth

router = APIRouter()


def _date_to_utc_range(date_str: str) -> tuple[str, str]:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    # 分界时间为当天 10:10（含）
    start = d + timedelta(hours=10, minutes=10)
    end   = d + timedelta(hours=34, minutes=10)
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


@router.post("/predict/{fixture_id}")
async def predict_match(fixture_id: int, _: AdminAuth):
    """手动触发单场预测"""
    logger.info(f"收到单场预测请求: fixture_id={fixture_id}")
    import time
    t0 = time.time()

    def _run():
        from prediction.predict import predict_fixture
        return predict_fixture(fixture_id)

    try:
        result = await asyncio.to_thread(_run)
    except PredictionDataError as e:
        # 数据缺失：比赛不存在 / 特征不足 / 模型缺失
        logger.warning(f"单场预测数据不足 fixture_id={fixture_id}: {e}")
        raise HTTPException(status_code=400, detail=f"数据不足，无法预测：{e}")
    except PredictionLLMError as e:
        # LLM 校验失败：上游大模型未返回合规结果
        logger.warning(f"单场预测 LLM 校验失败 fixture_id={fixture_id}: {e}")
        raise HTTPException(status_code=502, detail=f"LLM 预测生成失败：{e}（可重试）")
    except Exception as e:
        logger.error(f"单场预测异常 fixture_id={fixture_id}: {e}")
        raise HTTPException(status_code=500, detail=f"预测异常: {str(e)}")

    elapsed = time.time() - t0
    logger.info(f"单场预测完成 fixture_id={fixture_id}, 耗时 {elapsed:.1f}s")
    return {"status": "ok", "fixture_id": fixture_id, "elapsed": round(elapsed, 1), "result": result}

    logger.info(f"单场预测完成 fixture_id={fixture_id}, 耗时 {elapsed:.1f}s")
    return {"status": "ok", "fixture_id": fixture_id, "elapsed": round(elapsed, 1), "result": result}


@router.get("/odds/{fixture_id}")
async def get_odds(fixture_id: int, db: Session = Depends(get_db)):
    """从 odds 表读取该 fixture 的全部抓取记录（按抓取时间升序逐条返回）；无数据时 404。"""
    def _query():
        try:
            rows = db.execute(text(
                "SELECT odds_data, created_at FROM odds "
                "WHERE fixture_id = :fid ORDER BY created_at ASC, id ASC"
            ), {"fid": fixture_id}).fetchall()
        except Exception as e:
            # odds 表可能尚未创建（从未抓取过赔率）
            logger.debug(f"读取赔率失败 fixture_id={fixture_id}: {e}")
            try:
                db.rollback()
            except Exception:
                pass
            return None
        return rows

    rows = await asyncio.to_thread(_query)
    if rows is None:
        raise HTTPException(status_code=404, detail="暂无赔率数据")
    if not rows:
        raise HTTPException(status_code=404, detail="暂无赔率数据")

    import json
    records = []
    for row in rows:
        odds_data = row[0]
        if isinstance(odds_data, (str, bytes, bytearray)):
            try:
                odds_data = json.loads(odds_data)
            except Exception:
                odds_data = None
        created_at = row[1]
        records.append({
            "created_at": created_at.isoformat() if created_at else None,
            "odds_data": odds_data,
        })
    return {
        "status": "ok",
        "fixture_id": fixture_id,
        "records": records,
    }


@router.post("/odds/{fixture_id}")
async def fetch_and_save_odds(fixture_id: int, _: AdminAuth, db: Session = Depends(get_db)):
    """手动触发赔率抓取，保存到 odds 表。"""
    logger.info(f"收到赔率抓取请求: fixture_id={fixture_id}")

    def _run():
        from prediction.predict import _fetch_odds, _save_odds
        # 读取比赛日期, 用于锚定赔率搜索窗口(与预测路径保持一致)
        row = db.execute(text("SELECT date FROM fixtures WHERE id = :fid"), {"fid": fixture_id}).fetchone()
        match_date = row[0] if row else None

        # 每次点击都调用 API 抓取最新赔率; _save_odds 内部会比对与已存快照的差异,
        # 仅当赔率发生变动时才写入数据库, 避免重复写入完全相同的数据。
        result = _fetch_odds(fixture_id, match_date)
        # _fetch_odds 在无赔率且存在接口错误时返回 {"__api_error__": ...} 哨兵
        if isinstance(result, dict) and result.get("__api_error__") is not None:
            return result
        if result is None:
            return None
        _save_odds(db, fixture_id, result)
        return result

    try:
        result = await asyncio.to_thread(_run)
    except Exception as e:
        logger.error(f"赔率抓取异常 fixture_id={fixture_id}: {e}")
        raise HTTPException(status_code=500, detail=f"赔率抓取异常: {str(e)}")

    if isinstance(result, dict) and "__api_error__" in result:
        api_err = result["__api_error__"]
        msg = (api_err.get("requests") or api_err.get("message") or str(api_err)) if isinstance(api_err, dict) else str(api_err)
        logger.warning(f"赔率抓取受限 fixture_id={fixture_id}: {msg}")
        raise HTTPException(status_code=429, detail=f"赔率接口受限: {msg}")

    if result is None:
        logger.warning(f"赔率抓取失败 fixture_id={fixture_id}: API 未返回可用赔率数据")
        raise HTTPException(status_code=400, detail="未获取到赔率数据（可能该比赛暂无赔率）")

    logger.info(f"赔率抓取完成 fixture_id={fixture_id}")
    return {
        "status": "ok",
        "fixture_id": fixture_id,
        "updated_at": datetime.now().isoformat(),
        "data": result,
    }


@router.post("/predict-from-odds/{fixture_id}")
async def predict_from_odds_endpoint(fixture_id: int, _: AdminAuth):
    """基于实时赔率（调用 API-Football 查询）进行市场共识预测。"""
    logger.info(f"收到赔率预测请求: fixture_id={fixture_id}")

    def _run():
        from tools.predict_from_odds import predict_from_odds
        return predict_from_odds(fixture_id)

    try:
        result = await asyncio.to_thread(_run)
    except Exception as e:
        logger.error(f"赔率预测异常 fixture_id={fixture_id}: {e}")
        raise HTTPException(status_code=500, detail=f"赔率预测异常: {str(e)}")

    if result is None or result.get("error"):
        raise HTTPException(status_code=400, detail="未获取到赔率数据（可能该比赛暂无赔率或接口受限）")

    return {
        "status": "ok",
        "fixture_id": fixture_id,
        "result": result,
    }


@router.get("/predictions")
async def get_predictions(
    date: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    category: str | None = Query(None),
    league_id: int | None = Query(None),
    season: int | None = Query(None),
    team: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return await asyncio.to_thread(
        _get_predictions_sync, db, date, date_from, date_to, category, league_id, season, team, page, page_size
    )


@router.get("/predictions/accuracy")
async def get_predictions_accuracy(
    date: str | None = Query(None, description="按单日筛选，如 2026-05-01"),
    date_from: str | None = Query(None, description="起始日期，含，如 2026-05-01"),
    date_to: str | None = Query(None, description="结束日期，含，如 2026-05-31"),
    league_id: int | None = Query(None, description="按联赛筛选"),
    season: int | None = Query(None, description="按赛季（年份）筛选"),
    team: str | None = Query(None, description="按球队名（含）筛选"),
    category: str | None = Query(None, description="按模型分组/类别筛选，如 胜平负/WDL"),
    win_pct_min: float | None = Query(None, description="胜负信心百分比下限，如 60 表示 >=60%"),
    win_pct_max: float | None = Query(None, description="胜负信心百分比上限"),
    ou_pct_min: float | None = Query(None, description="大小球概率百分比下限"),
    ou_pct_max: float | None = Query(None, description="大小球概率百分比上限"),
    hand_pct_min: float | None = Query(None, description="盘口赢盘概率百分比下限"),
    hand_pct_max: float | None = Query(None, description="盘口赢盘概率百分比上限"),
    db: Session = Depends(get_db),
):
    """整体分类预测准确率：胜负 / 大小球 / 盘口 / 比分Top3。

    各类别可单独按置信度（pct）范围筛选，仅影响该类自己的样本与命中统计。
    """
    pct_ranges = {
        "win": (win_pct_min, win_pct_max),
        "over25": (ou_pct_min, ou_pct_max),
        "handicap": (hand_pct_min, hand_pct_max),
    }
    return await asyncio.to_thread(
        _get_accuracy_sync, db, date, date_from, date_to, league_id, season, team, category, pct_ranges
    )


@router.get("/predictions/value-bets/{fixture_id}")
async def get_value_bets(
    fixture_id: int,
    refresh: bool = Query(False, description="true 时先实时抓取赔率再分析"),
    margin: float = Query(0.03, description="判定为正价值的最小 edge 阈值"),
    db: Session = Depends(get_db),
):
    """价值投注筛选：比较模型隐含概率与市场赔率隐含概率，输出有正 edge 的下注建议。

    返回各市场（胜负平/大小球/让盘）的模型概率、市场隐含概率、edge、Kelly 比例与推荐方向。
    """
    return await asyncio.to_thread(compute_value_bets, db, fixture_id, refresh, margin)


@router.get("/predictions/calibration")
async def get_calibration_report():
    """概率校准诊断：返回各市场校准参数与校准前后 Brier 分数对比。

    说明：模型原始概率经 Platt scaling 校准后用于价值投注的 edge/Kelly 计算，
    以消除未校准导致的极端 Kelly 误导（如 95% 自信被夸大为 90% 注码）。
    """
    return {"report": get_diagnostics()}


# 各类别对应的置信度（pct）字段；比分 Top3 没有独立的 pct 字段，故不参与筛选。
ACCURACY_PCT_FIELD = {
    "win": "llm_win_pct",
    "over25": "llm_ou_pct",
    "handicap": "llm_handicap_pct",
}


def _parse_pct(val) -> float | None:
    """pct 字段来自 LLM 输出，存为字符串（如 '75%'），解析为 0-100 的浮点数。"""
    if val is None:
        return None
    try:
        return float(str(val).strip().rstrip('%').strip())
    except (TypeError, ValueError):
        return None


def _get_accuracy_sync(db, date, date_from, date_to, league_id, season, team, category, pct_ranges=None):
    pct_ranges = pct_ranges or {}
    conditions = ["f.fulltime_home IS NOT NULL AND f.fulltime_away IS NOT NULL"]
    params: dict = {}
    if date:
        utc_start, utc_end = _date_to_utc_range(date)
        conditions.append("p.match_date >= :utc_start AND p.match_date < :utc_end")
        params["utc_start"] = utc_start
        params["utc_end"] = utc_end
    if date_from:
        utc_start, _ = _date_to_utc_range(date_from)
        conditions.append("p.match_date >= :utc_start")
        params["utc_start"] = utc_start
    if date_to:
        _, utc_end = _date_to_utc_range(date_to)
        conditions.append("p.match_date < :utc_end")
        params["utc_end"] = utc_end
    if league_id is not None:
        conditions.append("f.league_id = :league_id")
        params["league_id"] = league_id
    if season is not None:
        conditions.append("f.season = :season")
        params["season"] = season
    if team:
        conditions.append("(p.home_name LIKE :team OR p.away_name LIKE :team)")
        params["team"] = f"%{team.strip()}%"
    if category:
        conditions.append("f.category = :category")
        params["category"] = category

    where = "WHERE " + " AND ".join(conditions)

    # 不直接对 *_correct 字段做 COUNT：历史记录可能尚未执行赛后回填，
    # 预测列表接口会用实际比分即时补算这些字段。统计接口也必须使用同一口径，
    # 否则不同联赛（尤其杯赛）会出现样本数少于实际已完赛预测数的情况。
    rows = db.execute(text(f"""
        SELECT p.win_correct, p.over25_correct, p.handicap_correct, p.score_in_top3,
               p.llm_win, p.llm_score, p.llm_ou_type, p.llm_ou_line,
               p.llm_handicap_num, p.llm_handicap_team,
               p.llm_win_pct, p.llm_ou_pct, p.llm_handicap_pct,
               f.fulltime_home AS actual_h, f.fulltime_away AS actual_a
        FROM predictions p
        LEFT JOIN fixtures f ON p.fixture_id = f.id
        {where}
    """), params).fetchall()

    totals = {"win": 0, "over25": 0, "handicap": 0, "score": 0}
    corrects = {"win": 0, "over25": 0, "handicap": 0, "score": 0}
    for row in rows:
        d = dict(row._mapping)
        actual_h = d.get("actual_h")
        actual_a = d.get("actual_a")
        flags = _derive_result_flags(d, actual_h, actual_a)
        for key, flag in zip(("win", "over25", "handicap", "score"), flags):
            # None 表示没有可验证结果，包含大小球/盘口走水，必须排除。
            if flag is None:
                continue
            # 按该类别自己的置信度区间筛选：区间只对当前类别生效，
            # 且 pct 缺失（未解析出数值）的记录不纳入该类别样本。
            pct_min, pct_max = pct_ranges.get(key) or (None, None)
            if pct_min is not None or pct_max is not None:
                pct = _parse_pct(d.get(ACCURACY_PCT_FIELD.get(key)))
                if pct is None:
                    continue
                if pct_min is not None and pct < pct_min:
                    continue
                if pct_max is not None and pct > pct_max:
                    continue
            totals[key] += 1
            corrects[key] += int(flag)

    def item(key: str, label: str):
        total = totals[key]
        correct = corrects[key]
        return {
            "key": key,
            "label": label,
            "total": total,
            "correct": correct,
            "accuracy": None if total == 0 else round(correct / total, 4),
        }

    data = [
        item("win", "胜负"),
        item("over25", "大小球"),
        item("handicap", "盘口"),
        item("score", "比分Top3"),
    ]
    return {"data": data}


def _derive_result_flags(d: dict, actual_h, actual_a):
    """根据 Fixtures.fulltime_* 为预测计算实际结果，确保口径统一。"""
    if actual_h is None or actual_a is None:
        return d.get("win_correct"), d.get("over25_correct"), d.get("handicap_correct"), d.get("score_in_top3")

    win_correct = None
    predicted_win = (d.get("llm_win") or "").strip()
    actual_win = "主胜" if actual_h > actual_a else ("平局" if actual_h == actual_a else "客胜")
    if predicted_win:
        if "主" in predicted_win:
            win_correct = int(actual_win == "主胜")
        elif "平" in predicted_win:
            win_correct = int(actual_win == "平局")
        elif "客" in predicted_win:
            win_correct = int(actual_win == "客胜")

    over_correct = None
    ou_type = (d.get("llm_ou_type") or "").strip()
    try:
        ou_line = float(d.get("llm_ou_line")) if d.get("llm_ou_line") is not None else None
    except (TypeError, ValueError):
        ou_line = None
    if ou_line is not None and ("大" in ou_type or "小" in ou_type):
        total = actual_h + actual_a
        if total != ou_line:
            actual_side = "大" if total > ou_line else "小"
            over_correct = int(actual_side in ou_type)

    handicap_correct = None
    try:
        handicap_line = float(d.get("llm_handicap_num")) if d.get("llm_handicap_num") is not None else None
    except (TypeError, ValueError):
        handicap_line = None
    if handicap_line is not None:
        adjusted_home = actual_h + handicap_line
        home_covers = adjusted_home > actual_a if adjusted_home != actual_a else None
        if home_covers is not None:
            handicap_correct = int(not home_covers if (d.get("llm_handicap_team") or "").strip() == "客队" else home_covers)

    score_correct = None
    if d.get("llm_score"):
        actual_score = f"{actual_h}-{actual_a}"
        scores = str(d["llm_score"]).replace("：", ":").split(",")
        score_correct = int(any(s.strip().replace(":", "-") == actual_score for s in scores))

    return win_correct, over_correct, handicap_correct, score_correct


def _get_predictions_sync(db, date, date_from, date_to, category, league_id, season, team, page, page_size):
    try:
        conditions = []
        params: dict = {}
        if date:
            utc_start, utc_end = _date_to_utc_range(date)
            conditions.append("p.match_date >= :utc_start AND p.match_date < :utc_end")
            params["utc_start"] = utc_start
            params["utc_end"] = utc_end
        if date_from:
            utc_start, _ = _date_to_utc_range(date_from)
            conditions.append("p.match_date >= :date_from_utc")
            params["date_from_utc"] = utc_start
        if date_to:
            _, utc_end = _date_to_utc_range(date_to)
            conditions.append("p.match_date < :date_to_utc")
            params["date_to_utc"] = utc_end
        if category:
            conditions.append("f.category = :category")
            params["category"] = category
        if league_id is not None:
            conditions.append("f.league_id = :league_id")
            params["league_id"] = league_id
        if season is not None:
            conditions.append("f.season = :season")
            params["season"] = season
        if team:
            conditions.append("(p.home_name LIKE :team OR p.away_name LIKE :team)")
            params["team"] = f"%{team.strip()}%"

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        total = db.execute(
            text(f"SELECT COUNT(*) FROM predictions p LEFT JOIN fixtures f ON p.fixture_id = f.id {where}"),
            params
        ).scalar()

        # JOIN 源表（leagues / teams）以使用 _zh 中文字段兜底
        rows = db.execute(text(f"""
            SELECT p.fixture_id, p.match_date, p.model_group,
                   p.win_home, p.win_draw, p.win_away, p.over25_prob,
                   p.top3_scores, p.lambda_home, p.lambda_away, p.handicap,
                   p.win_correct, p.over25_correct, p.handicap_correct, p.score_in_top3,
                   p.llm_win, p.llm_score, p.llm_win_pct,
                   p.llm_brief, p.llm_core_data, p.llm_deep_report,
                   p.llm_handicap_num, p.llm_handicap_team, p.llm_handicap_pct,
                   p.llm_ou_line, p.llm_ou_type, p.llm_ou_pct,
                   p.home_logo, p.away_logo,
                   f.home_id, f.away_id,
                   COALESCE(ht.name_zh, p.home_name) AS home_name,
                   COALESCE(at.name_zh, p.away_name) AS away_name,
                   COALESCE(lg.name_zh, p.league_name) AS league_name,
                   f.status_short, f.category,
                   f.fulltime_home AS actual_h, f.fulltime_away AS actual_a
            FROM predictions p
            LEFT JOIN fixtures f ON p.fixture_id = f.id
            LEFT JOIN teams ht ON f.home_id = ht.id
            LEFT JOIN teams at ON f.away_id = at.id
            LEFT JOIN leagues lg ON f.league_id = lg.id
            {where}
            ORDER BY p.match_date DESC
            LIMIT :limit OFFSET :offset
        """), {**params, "limit": page_size, "offset": (page - 1) * page_size}).fetchall()

        import json as _json
        data = []
        for r in rows:
            d = dict(r._mapping)
            for field in ('top3_scores',):
                if isinstance(d.get(field), str):
                    try:
                        d[field] = _json.loads(d[field])
                    except Exception:
                        pass

            # 实际比分只取 Fixtures 表的 fulltime_*，不使用比赛实时/最终累计比分或旧回填值。
            actual_h = d.get("actual_h")
            actual_a = d.get("actual_a")
            # 只有主客队比分都已落库，才展示完整的结果对比，避免半条比分触发误显示。
            has_result = actual_h is not None and actual_a is not None
            win_correct, over25_correct, handicap_correct, score_in_top3 = _derive_result_flags(d, actual_h, actual_a)

            record = {
                "basic": {
                    "fixture_id": d.get("fixture_id"),
                    "home_id": d.get("home_id"),
                    "away_id": d.get("away_id"),
                    "home_name": d.get("home_name"),
                    "away_name": d.get("away_name"),
                    "home_logo": d.get("home_logo"),
                    "away_logo": d.get("away_logo"),
                    "league_name": d.get("league_name"),
                    "match_date": d["match_date"].isoformat() if d.get("match_date") else None,
                    "status_short": d.get("status_short"),
                    "category": d.get("category"),
                },
                "xgb": {
                    "model_group": d.get("model_group"),
                    "prob": {
                        "home": d.get("win_home"),
                        "draw": d.get("win_draw"),
                        "away": d.get("win_away"),
                    },
                    "over25": {
                        "over": d.get("over25_prob"),
                        "under": 1 - (d.get("over25_prob") or 0),
                    } if d.get("over25_prob") is not None else None,
                    "lambda": {
                        "home": d.get("lambda_home"),
                        "away": d.get("lambda_away"),
                    } if d.get("lambda_home") is not None else None,
                    "top3": d.get("top3_scores"),
                    "handicap": d.get("handicap"),
                },
                "llm": {
                    "win": d.get("llm_win"),
                    "win_pct": d.get("llm_win_pct"),
                    "score": d.get("llm_score"),
                    "handicap_num": d.get("llm_handicap_num"),
                    "handicap_team": d.get("llm_handicap_team"),
                    "handicap_pct": d.get("llm_handicap_pct"),
                    "ou_line": d.get("llm_ou_line"),
                    "ou_type": d.get("llm_ou_type"),
                    "ou_pct": d.get("llm_ou_pct"),
                    "brief": d.get("llm_brief"),
                    "core_data": d.get("llm_core_data"),
                    "deep_report": d.get("llm_deep_report"),
                },
                "result": {
                    "score": f"{actual_h}-{actual_a}" if (has_result and actual_a is not None) else None,
                    "win_correct": win_correct,
                    "over25_correct": over25_correct,
                    "handicap_correct": handicap_correct,
                    "score_in_top3": score_in_top3,
                } if has_result else None,
            }
            data.append(record)

        return {"data": data, "total": total, "page": page, "page_size": page_size}
    finally:
        pass
