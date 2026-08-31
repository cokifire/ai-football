"""赛后验证回填：扫描 predictions 表，回填实际结果并计算正确性

轮询保护（防止无限消耗 API-Football 配额）：
  1. 已为 UNPLAYABLE 终态（取消/腰斩/延期等）的比赛，扫描 SQL 直接排除，
     不再请求 API；
  2. 只扫描 VERIFY_WINDOW_DAYS 天内的比赛，更早的不再轮询。
  原 verify_attempts/verify_skipped/verify_note 三列已删除，放弃态改由
  fixtures.status_short 在查询层过滤实现。
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
# 扫描 SQL 直接排除这些状态，避免每轮白发一次 GET /fixtures?id=<id>。
UNPLAYABLE = {"CANC", "ABD", "PST", "SUSP", "INT", "TBD"}

# 只回填最近 N 天的比赛，更早的不再轮询
VERIFY_WINDOW_DAYS = 7


def backfill_results(db=None):
    own_db = db is None
    if own_db:
        db = SessionLocal()

    try:
        cutoff = datetime.now() - timedelta(days=VERIFY_WINDOW_DAYS)

        # 超出时间窗的历史遗留记录：扫描 SQL 已用 match_date < cutoff 排除，
        # 这里仅做日志统计，不修改任何数据
        stale = db.execute(text(
            """SELECT COUNT(*) FROM predictions p
               JOIN fixtures f ON f.id = p.fixture_id
               WHERE f.status_short NOT IN :unplayable
                 AND p.win_correct IS NULL AND p.over25_correct IS NULL
                 AND p.handicap_correct IS NULL AND p.score_in_top3 IS NULL
                 AND p.match_date IS NOT NULL
                 AND p.match_date < :cutoff"""
        ), {"unplayable": tuple(sorted(UNPLAYABLE)), "cutoff": cutoff}).scalar() or 0
        if stale:
            logger.info(f"回填: {stale} 场超出 {VERIFY_WINDOW_DAYS} 天验证时间窗，已自然停止轮询")

        rows = db.execute(text(
            """SELECT p.fixture_id FROM predictions p
               JOIN fixtures f ON f.id = p.fixture_id
               WHERE f.status_short NOT IN :unplayable
                 AND (p.match_date IS NULL OR p.match_date >= :cutoff)
                 AND (p.win_correct IS NULL OR p.over25_correct IS NULL
                      OR p.handicap_correct IS NULL OR p.score_in_top3 IS NULL)
               ORDER BY p.match_date"""
        ), {"unplayable": tuple(sorted(UNPLAYABLE)), "cutoff": cutoff}).fetchall()
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


def _backfill_one(db, fid: int) -> bool:
    row = db.execute(text(
        "SELECT status_short, fulltime_home, fulltime_away FROM fixtures WHERE id = :fid"
    ), {"fid": fid}).fetchone()

    if not row:
        # fixtures 表无此比赛，跳过（下一轮窗口过期后自然排除）
        logger.warning(f"  回填跳过 fixture={fid}: fixtures 表缺失该比赛")
        return False

    status, gh, ga = row[0], row[1], row[2]

    # 本地状态已是不可完成终态 -> 不在扫描内（SQL 已排除），此处为兜底
    if status in UNPLAYABLE:
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
                    return False
        except Exception as e:
            logger.warning(f"  API 查询失败 fixture={fid}: {e}")
            return False

    if status not in FINISHED or gh is None or ga is None:
        # 比赛可能还没开始/正在进行，属正常情况，下一轮继续
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
        "SELECT llm_win, llm_handicap_num, llm_handicap_team, "
        "llm_score, over25_prob, llm_ou_type, llm_ou_line "
        "FROM predictions WHERE fixture_id=:fid"
    ), {"fid": fid}).fetchone()
    if not pred:
        return False

    # 胜平负正确性
    win_correct = 1 if pred[0] == actual_win else 0

    # 大小球正确性：依据 LLM 盘口类型(llm_ou_type)与盘口线(llm_ou_line)，基准为 llm_ou_line
    ou_type = pred[5] or ""
    ou_line = pred[6]
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

    # 让球盘正确性：用结构化字段 llm_handicap_num（负数=主队让，正值=客队让）
    handicap_correct = None
    hc_val = None
    if pred[1] is not None:
        try:
            hc_val = float(pred[1])
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

        team = str(pred[4]).strip() if pred[4] is not None else ""
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
            handicap_correct=:hcc, score_in_top3=:sc
        WHERE fixture_id=:fid
    """), {
        "gh": gh, "ga": ga,
        "wc": win_correct, "oc": over25_correct,
        "hcc": handicap_correct, "sc": score_in_top3,
        "fid": fid,
    })
    return True
