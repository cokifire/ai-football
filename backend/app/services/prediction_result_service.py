"""赛后验证回填：扫描 predictions 表，回填实际结果并计算正确性"""

import json
from sqlalchemy import text
from loguru import logger
import httpx

from app.core.config import settings
from app.db.session import SessionLocal

FINISHED = {"FT", "AET", "PEN", "AWD", "WO"}


def backfill_results(db=None):
    own_db = db is None
    if own_db:
        db = SessionLocal()

    try:
        rows = db.execute(text(
            "SELECT fixture_id FROM predictions WHERE actual_home_goals IS NULL"
        )).fetchall()
        if not rows:
            return

        fids = [r[0] for r in rows]
        logger.info(f"待验证预测: {len(fids)} 场")

        updated = 0
        for fid in fids:
            try:
                if _backfill_one(db, fid):
                    updated += 1
            except Exception as e:
                logger.debug(f"  回填失败 fixture={fid}: {e}")

        if updated:
            db.commit()
            logger.info(f"回填完成: {updated}/{len(fids)}")
    finally:
        if own_db:
            db.close()


def _backfill_one(db, fid: int) -> bool:
    row = db.execute(text(
        "SELECT status_short, goals_home, goals_away FROM fixtures WHERE id = :fid"
    ), {"fid": fid}).fetchone()

    if not row:
        return False

    status, gh, ga = row[0], row[1], row[2]

    if status not in FINISHED or gh is None or ga is None:
        try:
            r = httpx.get(
                f"{settings.api_football_base_url}/fixtures",
                headers={"x-apisports-key": settings.api_football_key},
                params={"id": fid},
                timeout=10.0,
            )
            r.raise_for_status()
            items = r.json().get("response", [])
            if items:
                f_info = items[0].get("fixture", {})
                st = (f_info.get("status") or {}).get("short")
                g = items[0].get("goals", {})
                status = st or status
                gh = g.get("home") if g.get("home") is not None else gh
                ga = g.get("away") if g.get("away") is not None else ga
                if st in FINISHED:
                    db.execute(text(
                        "UPDATE fixtures SET status_short=:st, goals_home=:gh, goals_away=:ga WHERE id=:fid"
                    ), {"st": st, "gh": gh, "ga": ga, "fid": fid})
        except Exception as e:
            logger.debug(f"  API 查询失败 fixture={fid}: {e}")
            return False

    if status not in FINISHED or gh is None or ga is None:
        return False

    total = (gh or 0) + (ga or 0)

    # 胜平负
    if gh > ga:
        actual_win = "主胜"
    elif gh == ga:
        actual_win = "平局"
    else:
        actual_win = "客胜"

    # 读取 LLM 预测
    pred = db.execute(text(
        "SELECT llm_win, llm_over_under, llm_handicap, llm_score FROM predictions WHERE fixture_id=:fid"
    ), {"fid": fid}).fetchone()
    if not pred:
        return False

    # 胜平负正确性
    win_correct = 1 if pred[0] == actual_win else 0

    # 大小球正确性
    actual_over = "大球" if total >= 3 else "小球"
    llm_over_under = pred[1] or ""
    if "大" in llm_over_under:
        over25_correct = 1 if actual_over == "大球" else 0
    elif "小" in llm_over_under:
        over25_correct = 1 if actual_over == "小球" else 0
    else:
        over25_correct = 0

    # 让球盘正确性
    handicap_correct = None
    llm_handicap = pred[2] or ""
    if llm_handicap:
        try:
            # 格式: -1.5 主队86% 或 0.5 客队70%
            parts = llm_handicap.split()
            if len(parts) >= 2:
                hc_val = float(parts[0])
                # 负值=主队让，正值=客队让
                adjusted_home = (gh or 0) + hc_val
                if adjusted_home > (ga or 0):
                    handicap_correct = 1
                elif adjusted_home < (ga or 0):
                    handicap_correct = 0
                else:
                    handicap_correct = 1  # 走水也算正确
        except Exception:
            pass

    # Top3 比分（基于 llm_score）
    actual_score = f"{gh}-{ga}"
    score_in_top3 = 0
    if pred[3]:
        try:
            llm_scores = (pred[3] or '').split(',')
            for s in llm_scores:
                s = s.strip().replace(':', '-').replace('：', '-')
                if s == actual_score:
                    score_in_top3 = 1
                    break
        except Exception:
            pass

    db.execute(text("""
        UPDATE predictions SET
            actual_home_goals=:gh, actual_away_goals=:ga,
            win_correct=:wc, over25_correct=:oc,
            handicap_correct=:hcc, score_in_top3=:sc
        WHERE fixture_id=:fid
    """), {
        "gh": gh, "ga": ga,
        "wc": win_correct, "oc": over25_correct,
        "hcc": handicap_correct, "sc": score_in_top3,
        "fid": fid,
    })
    return True
