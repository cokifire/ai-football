"""赛后验证回填：扫描 predictions 表，回填实际结果并计算正确性

轮询保护（防止无限消耗 API-Football 配额）：
  1. UNPLAYABLE 状态（取消/腰斩/延期等）直接标记放弃，不再请求 API；
  2. verify_attempts 超过 MAX_VERIFY_ATTEMPTS 后放弃；
  3. 只扫描 VERIFY_WINDOW_DAYS 天内的比赛，更早的直接放弃。
"""

from datetime import datetime, timedelta

from sqlalchemy import text
from loguru import logger

from app.core.config import settings
from app.core.api_football import api_football_get_sync
from app.db.session import SessionLocal

# 已完赛、有最终比分的状态
FINISHED = {"FT", "AET", "PEN", "AWD", "WO"}

# 永远不会产生有效比分的终态：取消 / 腰斩 / 延期 / 中断 / 待定。
# 这类比赛必须直接放弃，否则每轮都会白发一次 GET /fixtures?id=<id>。
UNPLAYABLE = {"CANC", "ABD", "PST", "SUSP", "INT", "TBD"}

# 单场最多尝试验证次数，兜底任何未预料的状态
MAX_VERIFY_ATTEMPTS = 5

# 只回填最近 N 天的比赛，更早的不再轮询
VERIFY_WINDOW_DAYS = 7


def backfill_results(db=None):
    own_db = db is None
    if own_db:
        db = SessionLocal()

    try:
        cutoff = datetime.now() - timedelta(days=VERIFY_WINDOW_DAYS)

        # 超出时间窗的历史遗留记录：一次性放弃，不再进入轮询
        stale = db.execute(text(
            """SELECT fixture_id FROM predictions
               WHERE verify_skipped = 0
                 AND win_correct IS NULL AND over25_correct IS NULL
                 AND handicap_correct IS NULL AND score_in_top3 IS NULL
                 AND match_date IS NOT NULL
                 AND match_date < :cutoff"""
        ), {"cutoff": cutoff}).fetchall()
        for (fid,) in stale:
            _give_up(db, fid, f"超出 {VERIFY_WINDOW_DAYS} 天验证时间窗")
        if stale:
            db.commit()
            logger.info(f"回填: {len(stale)} 场超出时间窗，已停止轮询")

        rows = db.execute(text(
            """SELECT fixture_id FROM predictions
               WHERE verify_skipped = 0
                 AND (match_date IS NULL OR match_date >= :cutoff)
                 AND (win_correct IS NULL OR over25_correct IS NULL
                      OR handicap_correct IS NULL OR score_in_top3 IS NULL)
               ORDER BY match_date"""
        ), {"cutoff": cutoff}).fetchall()
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
                logger.warning(f"  回填失败 fixture={fid}: {e}")

        db.commit()
        logger.info(f"回填完成: {updated}/{len(fids)}")
    finally:
        if own_db:
            db.close()


def _give_up(db, fid: int, reason: str):
    """标记该预测放弃赛后验证，后续扫描不再选中它。"""
    db.execute(text(
        "UPDATE predictions SET verify_skipped = 1, verify_note = :note WHERE fixture_id = :fid"
    ), {"note": reason[:255], "fid": fid})
    logger.info(f"  放弃验证 fixture={fid}: {reason}")


def _bump_attempts(db, fid: int, reason: str):
    """累计尝试次数；达到上限则放弃，防止任何未预料状态造成无限轮询。"""
    db.execute(text(
        "UPDATE predictions SET verify_attempts = verify_attempts + 1 WHERE fixture_id = :fid"
    ), {"fid": fid})
    n = db.execute(text(
        "SELECT verify_attempts FROM predictions WHERE fixture_id = :fid"
    ), {"fid": fid}).scalar()
    if n is not None and n >= MAX_VERIFY_ATTEMPTS:
        _give_up(db, fid, f"{reason}（已尝试 {n} 次，达上限）")


def _backfill_one(db, fid: int) -> bool:
    row = db.execute(text(
        "SELECT status_short, fulltime_home, fulltime_away FROM fixtures WHERE id = :fid"
    ), {"fid": fid}).fetchone()

    if not row:
        # fixtures 表无此比赛，累计尝试次数，超限则放弃
        _bump_attempts(db, fid, "fixtures 表缺失该比赛")
        return False

    status, gh, ga = row[0], row[1], row[2]

    # 本地状态已是不可完成终态 -> 直接放弃，不发 API 请求
    if status in UNPLAYABLE:
        _give_up(db, fid, f"比赛状态 {status}，无有效比分")
        return False

    if status not in FINISHED or gh is None or ga is None:
        try:
            r = api_football_get_sync(
                "fixtures",
                params={"id": fid},
                timeout=10.0,
            )
            r.raise_for_status()
            items = r.json().get("response", [])
            if items:
                f_info = items[0].get("fixture", {})
                st = (f_info.get("status") or {}).get("short")
                g = (items[0].get("score") or {}).get("fulltime") or {}
                status = st or status
                gh = g.get("home") if g.get("home") is not None else gh
                ga = g.get("away") if g.get("away") is not None else ga
                if st in FINISHED or st in UNPLAYABLE:
                    # 无论完赛还是取消，都把最新状态写回本地，
                    # 这样下一轮不必再发请求即可判定
                    db.execute(text(
                        "UPDATE fixtures SET status_short=:st, fulltime_home=:gh, fulltime_away=:ga WHERE id=:fid"
                    ), {"st": st, "gh": gh, "ga": ga, "fid": fid})
                if st in UNPLAYABLE:
                    _give_up(db, fid, f"API 返回状态 {st}，无有效比分")
                    return False
        except Exception as e:
            logger.warning(f"  API 查询失败 fixture={fid}: {e}")
            _bump_attempts(db, fid, "API 查询多次失败")
            return False

    if status not in FINISHED or gh is None or ga is None:
        # 比赛可能还没开始/正在进行，属正常情况；累计次数兜底未预料状态
        _bump_attempts(db, fid, f"状态 {status} 始终未达终态")
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
            win_correct=:wc, over25_correct=:oc,
            handicap_correct=:hcc, score_in_top3=:sc,
            verify_skipped=0, verify_note=NULL
        WHERE fixture_id=:fid
    """), {
        "gh": gh, "ga": ga,
        "wc": win_correct, "oc": over25_correct,
        "hcc": handicap_correct, "sc": score_in_top3,
        "fid": fid,
    })
    return True
