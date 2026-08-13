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
    db: Session = Depends(get_db),
):
    """整体分类预测准确率：胜负 / 大小球 / 盘口 / 比分Top3。"""
    return await asyncio.to_thread(
        _get_accuracy_sync, db, date, date_from, date_to, league_id, season, team, category
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


def _get_accuracy_sync(db, date, date_from, date_to, league_id, season, team, category):
    conditions = ["(p.actual_home_goals IS NOT NULL OR f.goals_home IS NOT NULL)"]
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

    row = db.execute(text(f"""
        SELECT
            COUNT(p.win_correct)      AS win_total,
            SUM(p.win_correct)        AS win_correct_cnt,
            COUNT(p.over25_correct)   AS over_total,
            SUM(p.over25_correct)     AS over_correct_cnt,
            COUNT(p.handicap_correct) AS hand_total,
            SUM(p.handicap_correct)   AS hand_correct_cnt,
            COUNT(p.score_in_top3)    AS score_total,
            SUM(p.score_in_top3)      AS score_correct_cnt
        FROM predictions p
        LEFT JOIN fixtures f ON p.fixture_id = f.id
        {where}
    """), params).fetchone()

    d = dict(row._mapping)

    def item(key: str, label: str, total, correct):
        total = int(total or 0)
        correct = int(correct or 0)
        return {
            "key": key,
            "label": label,
            "total": total,
            "correct": correct,
            "accuracy": None if total == 0 else round(correct / total, 4),
        }

    data = [
        item("win", "胜负", d["win_total"], d["win_correct_cnt"]),
        item("over25", "大小球", d["over_total"], d["over_correct_cnt"]),
        item("handicap", "盘口", d["hand_total"], d["hand_correct_cnt"]),
        item("score", "比分Top3", d["score_total"], d["score_correct_cnt"]),
    ]
    return {"data": data}


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
                   p.llm_handicap, p.llm_over_under,
                   p.llm_handicap_num, p.llm_handicap_team, p.llm_handicap_pct,
                   p.llm_ou_line, p.llm_ou_type, p.llm_ou_pct,
                   p.home_logo, p.away_logo,
                   f.home_id, f.away_id,
                   COALESCE(ht.name_zh, p.home_name) AS home_name,
                   COALESCE(at.name_zh, p.away_name) AS away_name,
                   COALESCE(lg.name_zh, p.league_name) AS league_name,
                   f.status_short, f.category,
                   f.goals_home AS actual_h, f.goals_away AS actual_a,
                   p.actual_home_goals AS p_ah, p.actual_away_goals AS p_aa
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

            # 实际比分优先取 predictions 表已回填的值，回退 fixtures
            actual_h = d.get("p_ah") if d.get("p_ah") is not None else d.get("actual_h")
            actual_a = d.get("p_aa") if d.get("p_aa") is not None else d.get("actual_a")
            has_result = actual_h is not None

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
                    "handicap": d.get("llm_handicap"),
                    "handicap_num": d.get("llm_handicap_num"),
                    "handicap_team": d.get("llm_handicap_team"),
                    "handicap_pct": d.get("llm_handicap_pct"),
                    "over_under": d.get("llm_over_under"),
                    "ou_line": d.get("llm_ou_line"),
                    "ou_type": d.get("llm_ou_type"),
                    "ou_pct": d.get("llm_ou_pct"),
                    "brief": d.get("llm_brief"),
                    "core_data": d.get("llm_core_data"),
                    "deep_report": d.get("llm_deep_report"),
                },
                "result": {
                    "score": f"{actual_h}-{actual_a}" if (has_result and actual_a is not None) else None,
                    "win_correct": d.get("win_correct"),
                    "over25_correct": d.get("over25_correct"),
                    "handicap_correct": d.get("handicap_correct"),
                    "score_in_top3": d.get("score_in_top3"),
                } if has_result else None,
            }
            data.append(record)

        return {"data": data, "total": total, "page": page, "page_size": page_size}
    finally:
        pass
