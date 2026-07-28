"""赛后验证回填：扫描 predictions 表，回填实际结果并计算正确性"""

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

    # 读取 LLM 预测（含让球盘结构化字段、大小球盘口类型与盘口线）
    pred = db.execute(text(
        "SELECT llm_win, llm_over_under, llm_handicap, llm_handicap_num, llm_handicap_team, "
        "llm_score, over25_prob, llm_ou_type, llm_ou_line "
        "FROM predictions WHERE fixture_id=:fid"
    ), {"fid": fid}).fetchone()
    if not pred:
        return False

    # 胜平负正确性
    win_correct = 1 if pred[0] == actual_win else 0

    # 大小球正确性：依据 LLM 盘口类型(llm_ou_type)与盘口线(llm_ou_line)，基准为 llm_ou_line
    ou_type = pred[7] or ""
    ou_line = pred[8]
    if ou_type and ou_line is not None:
        try:
            line = float(ou_line)
            if total > line:
                actual_over = "大球"
            elif total < line:
                actual_over = "小球"
            else:
                actual_over = None  # 走水
            if actual_over is None:
                over25_correct = None  # 走水显示 -
            elif "大" in ou_type:
                over25_correct = 1 if actual_over == "大球" else 0
            elif "小" in ou_type:
                over25_correct = 1 if actual_over == "小球" else 0
            else:
                over25_correct = 0
        except (ValueError, TypeError):
            over25_correct = None
    else:
        over25_correct = None

    # 让球盘正确性：优先用结构化字段 llm_handicap_num（负数=主队让，正值=客队让），
    # 回退兼容旧的自由文本 llm_handicap
    handicap_correct = None
    hc_val = None
    if pred[3] is not None:
        try:
            hc_val = float(pred[3])
        except (ValueError, TypeError):
            hc_val = None
    elif pred[2]:
        try:
            parts = (pred[2] or "").split()
            if parts:
                hc_val = float(parts[0])
        except (ValueError, TypeError):
            hc_val = None

    if hc_val is not None:
        # 以「主队视角」计算主队是否赢盘
        adjusted_home = (gh or 0) + hc_val
        if adjusted_home > (ga or 0):
            home_covers = True
        elif adjusted_home < (ga or 0):
            home_covers = False
        else:
            home_covers = None  # 走水

        team = (pred[4] or "").strip()
        if home_covers is None:
            handicap_correct = None
        elif team == "客队":
            # 预测方为客队，结果与主队视角相反
            handicap_correct = 0 if home_covers else 1
        else:
            handicap_correct = 1 if home_covers else 0

    # Top3 比分（基于 llm_score）
    actual_score = f"{gh}-{ga}"
    score_in_top3 = 0
    if pred[5]:
        try:
            llm_scores = (pred[5] or '').split(',')
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
