"""预测相关 API 端点"""

import asyncio
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, Query, Depends
from loguru import logger
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import get_db

router = APIRouter()


def _date_to_utc_range(date_str: str) -> tuple[str, str]:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    # 分界时间为当天 10:10（含）
    start = d + timedelta(hours=10, minutes=10)
    end   = d + timedelta(hours=34, minutes=10)
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


@router.post("/predict/{fixture_id}")
async def predict_match(fixture_id: int):
    """手动触发单场预测"""
    logger.info(f"收到单场预测请求: fixture_id={fixture_id}")
    import time
    t0 = time.time()

    def _run():
        from prediction.predict import predict_fixture
        return predict_fixture(fixture_id)

    try:
        result = await asyncio.to_thread(_run)
    except Exception as e:
        logger.error(f"单场预测异常 fixture_id={fixture_id}: {e}")
        raise HTTPException(status_code=500, detail=f"预测异常: {str(e)}")

    elapsed = time.time() - t0
    if result is None:
        logger.warning(f"单场预测返回空 fixture_id={fixture_id}, 耗时 {elapsed:.1f}s")
        raise HTTPException(status_code=400, detail="预测失败（数据不足或比赛不存在）")

    logger.info(f"单场预测完成 fixture_id={fixture_id}, 耗时 {elapsed:.1f}s")
    return {"status": "ok", "fixture_id": fixture_id, "elapsed": round(elapsed, 1), "result": result}


@router.post("/odds/{fixture_id}")
async def fetch_and_save_odds(fixture_id: int, db: Session = Depends(get_db)):
    """手动触发赔率抓取，保存到 odds 表（每次点击都覆盖旧数据）。"""
    logger.info(f"收到赔率抓取请求: fixture_id={fixture_id}")

    def _run():
        from prediction.predict import _fetch_odds, _save_odds
        result = _fetch_odds(fixture_id)
        if result is None:
            return None
        _save_odds(db, fixture_id, result)
        return result

    try:
        result = await asyncio.to_thread(_run)
    except Exception as e:
        logger.error(f"赔率抓取异常 fixture_id={fixture_id}: {e}")
        raise HTTPException(status_code=500, detail=f"赔率抓取异常: {str(e)}")

    if result is None:
        raise HTTPException(status_code=400, detail="未获取到赔率数据（可能该比赛暂无赔率）")

    logger.info(f"赔率抓取完成 fixture_id={fixture_id}")
    return {
        "status": "ok",
        "fixture_id": fixture_id,
        "updated_at": datetime.now().isoformat(),
        "data": result,
    }


@router.get("/predictions")
async def get_predictions(
    date: str | None = Query(None),
    category: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return await asyncio.to_thread(
        _get_predictions_sync, db, date, category, page, page_size
    )


def _get_predictions_sync(db, date, category, page, page_size):
    try:
        conditions = []
        params: dict = {}
        if date:
            utc_start, utc_end = _date_to_utc_range(date)
            conditions.append("p.match_date >= :utc_start AND p.match_date < :utc_end")
            params["utc_start"] = utc_start
            params["utc_end"] = utc_end
        if category:
            conditions.append("f.category = :category")
            params["category"] = category

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
                   p.llm_win, p.llm_score, p.llm_win_pct,
                   p.llm_brief, p.llm_core_data, p.llm_deep_report,
                   p.llm_handicap, p.llm_over_under,
                   p.llm_handicap_num, p.llm_handicap_team, p.llm_handicap_pct,
                   p.llm_ou_line, p.llm_ou_type, p.llm_ou_pct,
                   p.home_logo, p.away_logo,
                   COALESCE(ht.name_zh, p.home_name) AS home_name,
                   COALESCE(at.name_zh, p.away_name) AS away_name,
                   COALESCE(lg.name_zh, p.league_name) AS league_name,
                   f.status_short, f.category,
                   f.goals_home AS actual_h, f.goals_away AS actual_a
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

            record = {
                "basic": {
                    "fixture_id": d.get("fixture_id"),
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
                    "score": f"{d['actual_h']}-{d['actual_a']}" if d.get("actual_h") is not None else None,
                    "win_correct": d.get("win_correct"),
                    "over25_correct": d.get("over25_correct"),
                    "handicap_correct": d.get("handicap_correct"),
                    "top3_correct": d.get("score_in_top3"),
                } if d.get("actual_h") is not None else None,
            }
            data.append(record)

        return {"data": data, "total": total, "page": page, "page_size": page_size}
    finally:
        pass
