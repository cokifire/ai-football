"""
特征提取 pipeline（批量内存版）：
一次性加载所有数据到内存，用 pandas 向量化计算，速度提升 100x+。

特征组：
  A. 近期状态（近5/10场，全场 + 主客场分拆）
  B. 比赛统计均值（射门、控球、xG，近5场）
  C. H2H 历史交锋（近5场）
  D. 积分榜（当赛季，可选）
  E. 赛事背景（联赛、赛季、是否杯赛）

标签：
  label_win       0=客胜 1=平局 2=主胜
  label_over25    1=大球(>=3) 0=小球
  label_goals_range  0=0-1球 1=2-3球 2=4-5球 3=6+球
  goals_home / goals_away  用于 Poisson Top3
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pandas as pd
import numpy as np
from loguru import logger
from sqlalchemy import text

from app.db.session import SessionLocal, engine

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), 'features.csv')

CUP_LEAGUES = {45, 2, 3, 848, 10, 11, 13, 17, 18, 666}
WINDOW_SHORT = 5
WINDOW_LONG = 10


def _load_all_data():
    """一次性加载所有需要的数据"""
    db = SessionLocal()
    try:
        logger.info("加载 fixtures...")
        rows = db.execute(text("""
            SELECT f.id, f.home_id, f.away_id, f.league_id, f.season,
                   f.date, f.goals_home, f.goals_away,
                   f.halftime_home, f.halftime_away,
                   f.home_name, f.away_name, l.enabled
            FROM fixtures f
            JOIN leagues l ON f.league_id = l.id
            WHERE f.status_short IN ('FT','AET','PEN')
              AND f.goals_home IS NOT NULL
            ORDER BY f.date ASC
        """)).fetchall()
        fixtures = pd.DataFrame([dict(r._mapping) for r in rows])
        fixtures['date'] = pd.to_datetime(fixtures['date'])
        logger.info(f"  fixtures: {len(fixtures):,} 行")

        logger.info("加载 fixture_statistics...")
        rows = db.execute(text("""
            SELECT fixture_id, team_id, stat_type, stat_value
            FROM fixture_statistics
            WHERE stat_type IN (
                'expected_goals','Total Shots','Shots on Goal',
                'Ball Possession','Corner Kicks','Passes %','Fouls'
            )
        """)).fetchall()
        stats = pd.DataFrame([dict(r._mapping) for r in rows])
        logger.info(f"  fixture_statistics: {len(stats):,} 行")

        logger.info("加载 standings...")
        rows = db.execute(text("""
            SELECT league_id, season, team_id,
                   `rank`, points, goals_diff,
                   all_played, all_win,
                   home_played, home_win, home_goals_for, home_goals_against,
                   away_played, away_win, away_goals_for, away_goals_against
            FROM standings
        """)).fetchall()
        standings = pd.DataFrame([dict(r._mapping) for r in rows])
        logger.info(f"  standings: {len(standings):,} 行")

    finally:
        db.close()

    return fixtures, stats, standings


def _pivot_stats(stats: pd.DataFrame) -> pd.DataFrame:
    """将 fixture_statistics 宽表化：每行 = (fixture_id, team_id)，列 = 各统计值"""
    def parse_val(s):
        if s is None or s == 'None':
            return np.nan
        try:
            return float(str(s).replace('%', ''))
        except Exception:
            return np.nan

    stats = stats.copy()
    stats['val'] = stats['stat_value'].apply(parse_val)
    pivoted = stats.pivot_table(
        index=['fixture_id', 'team_id'],
        columns='stat_type',
        values='val',
        aggfunc='first',
    ).reset_index()
    pivoted.columns.name = None
    col_map = {
        'expected_goals': 'xg',
        'Total Shots': 'shots',
        'Shots on Goal': 'shots_on',
        'Ball Possession': 'possession',
        'Corner Kicks': 'corners',
        'Passes %': 'passes_pct',
        'Fouls': 'fouls',
    }
    pivoted = pivoted.rename(columns=col_map)
    return pivoted


def _compute_team_form(fixtures: pd.DataFrame, n: int, home_only=False, away_only=False) -> pd.DataFrame:
    """
    对每场比赛，计算主队/客队在该场之前的近 n 场状态。
    返回 DataFrame，index = fixture_id，列 = home_*/away_* 特征。
    """
    # 展开为 (fixture_id, date, team_id, is_home, gf, ga, ht_gf, ht_ga)
    home_rows = fixtures[['id', 'date', 'home_id', 'goals_home', 'goals_away',
                           'halftime_home', 'halftime_away']].copy()
    home_rows.columns = ['fixture_id', 'date', 'team_id', 'gf', 'ga', 'ht_gf', 'ht_ga']
    home_rows['is_home'] = True

    away_rows = fixtures[['id', 'date', 'away_id', 'goals_away', 'goals_home',
                           'halftime_away', 'halftime_home']].copy()
    away_rows.columns = ['fixture_id', 'date', 'team_id', 'gf', 'ga', 'ht_gf', 'ht_ga']
    away_rows['is_home'] = False

    if home_only:
        all_matches = home_rows
    elif away_only:
        all_matches = away_rows
    else:
        all_matches = pd.concat([home_rows, away_rows], ignore_index=True)

    all_matches = all_matches.sort_values('date')
    all_matches['win'] = (all_matches['gf'] > all_matches['ga']).astype(float)
    all_matches['draw'] = (all_matches['gf'] == all_matches['ga']).astype(float)
    all_matches['loss'] = (all_matches['gf'] < all_matches['ga']).astype(float)
    all_matches['pts'] = all_matches['win'] * 3 + all_matches['draw']

    # 对每个 team_id，按时间排序，计算滚动窗口（排除当前行）
    results = []
    for team_id, grp in all_matches.groupby('team_id'):
        grp = grp.sort_values('date').reset_index(drop=True)
        for col in ['win', 'draw', 'loss', 'gf', 'ga', 'ht_gf', 'ht_ga', 'pts']:
            # shift(1) 排除当前场，rolling(n) 取前 n 场
            grp[f'r_{col}'] = grp[col].shift(1).rolling(n, min_periods=3).mean()
        grp[f'r_n'] = grp['win'].shift(1).rolling(n, min_periods=3).count()
        results.append(grp[['fixture_id', 'team_id', 'is_home'] +
                            [f'r_{c}' for c in ['win', 'draw', 'loss', 'gf', 'ga',
                                                 'ht_gf', 'ht_ga', 'pts', 'n']]])

    form_df = pd.concat(results, ignore_index=True)
    return form_df


def _compute_result_sequence(fixtures: pd.DataFrame, n: int = 5) -> pd.DataFrame:
    """
    计算每场比赛前近 n 场的逐场结果序列（从近到远）和动量特征。
    result_1=最近一场, result_n=最早一场；2=胜,1=平,0=负
    momentum = 近3场胜率 - 近5场胜率
    """
    home_rows = fixtures[['id', 'date', 'home_id', 'goals_home', 'goals_away']].copy()
    home_rows.columns = ['fixture_id', 'date', 'team_id', 'gf', 'ga']
    home_rows['is_home'] = True

    away_rows = fixtures[['id', 'date', 'away_id', 'goals_away', 'goals_home']].copy()
    away_rows.columns = ['fixture_id', 'date', 'team_id', 'gf', 'ga']
    away_rows['is_home'] = False

    all_matches = pd.concat([home_rows, away_rows], ignore_index=True)
    all_matches['result'] = np.where(all_matches['gf'] > all_matches['ga'], 2,
                             np.where(all_matches['gf'] == all_matches['ga'], 1, 0))

    results = []
    for team_id, grp in all_matches.groupby('team_id'):
        grp = grp.sort_values('date').reset_index(drop=True)
        win = (grp['result'] == 2).astype(float)

        for i in range(1, n + 1):
            grp[f'seq_{i}'] = grp['result'].shift(i)

        grp['win3'] = win.shift(1).rolling(3, min_periods=1).mean()
        grp['win5'] = win.shift(1).rolling(5, min_periods=3).mean()
        grp['momentum'] = grp['win3'] - grp['win5']

        cols = ['fixture_id', 'team_id', 'is_home', 'momentum'] + [f'seq_{i}' for i in range(1, n + 1)]
        results.append(grp[cols])

    return pd.concat(results, ignore_index=True)


def _compute_match_stats_form(fixtures: pd.DataFrame, stats_pivoted: pd.DataFrame,
                               n: int = 5) -> pd.DataFrame:
    """计算每队在每场比赛前的近 n 场统计均值"""
    # 关联 fixture 日期
    fix_dates = fixtures[['id', 'date']].rename(columns={'id': 'fixture_id'})
    s = stats_pivoted.merge(fix_dates, on='fixture_id', how='left')
    s = s.sort_values('date')

    stat_cols = [c for c in ['xg', 'shots', 'shots_on', 'possession', 'corners',
                              'passes_pct', 'fouls'] if c in s.columns]

    results = []
    for team_id, grp in s.groupby('team_id'):
        grp = grp.sort_values('date').reset_index(drop=True)
        for col in stat_cols:
            grp[f'ms_{col}'] = grp[col].shift(1).rolling(n, min_periods=2).mean()
        results.append(grp[['fixture_id', 'team_id'] + [f'ms_{c}' for c in stat_cols]])

    return pd.concat(results, ignore_index=True)


def _compute_h2h(fixtures: pd.DataFrame, n: int = 5) -> pd.DataFrame:
    """计算每场比赛的 H2H 特征（主队视角），向量化实现"""
    fix = fixtures[['id', 'date', 'home_id', 'away_id',
                    'goals_home', 'goals_away']].copy()
    fix = fix.sort_values('date').reset_index(drop=True)

    # 构建双向历史记录（统一以 team_a < team_b 为 key，方便查找）
    # 对每场比赛，pair_key = (min(home,away), max(home,away))
    fix['pair_key'] = fix.apply(
        lambda r: (min(r['home_id'], r['away_id']), max(r['home_id'], r['away_id'])), axis=1
    )

    results = []
    for pair_key, grp in fix.groupby('pair_key'):
        grp = grp.sort_values('date').reset_index(drop=True)
        hid_ref = pair_key[0]  # 用 pair 中较小的 id 作为参考主队

        # 从主队视角计算 gf/ga
        grp['gf_ref'] = np.where(grp['home_id'] == hid_ref,
                                  grp['goals_home'], grp['goals_away'])
        grp['ga_ref'] = np.where(grp['home_id'] == hid_ref,
                                  grp['goals_away'], grp['goals_home'])
        grp['total'] = grp['gf_ref'] + grp['ga_ref']
        grp['hw'] = (grp['gf_ref'] > grp['ga_ref']).astype(float)
        grp['dw'] = (grp['gf_ref'] == grp['ga_ref']).astype(float)
        grp['over'] = (grp['total'] >= 3).astype(float)

        for i, row in grp.iterrows():
            past = grp[grp['date'] < row['date']].tail(n)
            nn = len(past)
            if nn == 0:
                results.append({
                    'fixture_id': row['id'],
                    'h2h_home_win': np.nan, 'h2h_draw': np.nan,
                    'h2h_avg_goals': np.nan, 'h2h_over25': np.nan,
                    'h2h_n': 0,
                })
                continue

            # 转换回实际主队视角
            actual_home = row['home_id']
            if actual_home == hid_ref:
                hw_rate = past['hw'].mean()
            else:
                hw_rate = 1 - past['hw'].mean() - past['dw'].mean()

            results.append({
                'fixture_id': row['id'],
                'h2h_home_win': hw_rate,
                'h2h_draw': past['dw'].mean(),
                'h2h_avg_goals': past['total'].mean(),
                'h2h_over25': past['over'].mean(),
                'h2h_n': nn,
            })

    return pd.DataFrame(results)


def main():
    logger.info("开始特征提取（批量内存版）...")
    fixtures, stats, standings = _load_all_data()

    # 只取启用联赛
    enabled_fixtures = fixtures[fixtures['enabled'] == 1].copy()
    logger.info(f"启用联赛完赛场次: {len(enabled_fixtures):,}")

    # 1. 近期状态（全场，近5/10场）
    logger.info("计算近期状态（全场 n=5）...")
    form5 = _compute_team_form(enabled_fixtures, n=WINDOW_SHORT)

    logger.info("计算近期状态（全场 n=10）...")
    form10 = _compute_team_form(enabled_fixtures, n=WINDOW_LONG)

    logger.info("计算主场状态（n=5）...")
    form_home5 = _compute_team_form(enabled_fixtures, n=WINDOW_SHORT, home_only=True)

    logger.info("计算客场状态（n=5）...")
    form_away5 = _compute_team_form(enabled_fixtures, n=WINDOW_SHORT, away_only=True)

    logger.info("计算结果序列特征...")
    seq5 = _compute_result_sequence(enabled_fixtures, n=WINDOW_SHORT)

    # 2. 比赛统计
    logger.info("计算比赛统计均值...")
    stats_pivoted = _pivot_stats(stats)
    ms5 = _compute_match_stats_form(enabled_fixtures, stats_pivoted, n=WINDOW_SHORT)

    # 3. H2H
    logger.info("计算 H2H...")
    h2h = _compute_h2h(enabled_fixtures, n=5)

    # 4. 积分榜
    logger.info("处理积分榜...")
    standings_home = standings.rename(columns={
        'team_id': 'home_id',
        'rank': 'h_rank', 'points': 'h_points', 'goals_diff': 'h_goals_diff',
        'home_played': 'h_home_played', 'home_win': 'h_home_win',
        'home_goals_for': 'h_home_gf', 'home_goals_against': 'h_home_ga',
    })
    standings_away = standings.rename(columns={
        'team_id': 'away_id',
        'rank': 'a_rank', 'points': 'a_points', 'goals_diff': 'a_goals_diff',
        'away_played': 'a_away_played', 'away_win': 'a_away_win',
        'away_goals_for': 'a_away_gf', 'away_goals_against': 'a_away_ga',
    })

    # 5. 组装特征
    logger.info("组装特征矩阵...")
    df = enabled_fixtures[['id', 'home_id', 'away_id', 'league_id', 'season',
                            'date', 'goals_home', 'goals_away']].copy()
    df = df.rename(columns={'id': 'fixture_id'})

    # 主队近5场（全场）
    h_form5 = form5[form5['is_home'] == True][
        ['fixture_id', 'team_id'] + [f'r_{c}' for c in
         ['win', 'draw', 'loss', 'gf', 'ga', 'ht_gf', 'ht_ga', 'pts', 'n']]
    ].rename(columns={
        'team_id': 'home_id',
        'r_win': 'h_win5', 'r_draw': 'h_draw5', 'r_loss': 'h_loss5',
        'r_gf': 'h_gf5', 'r_ga': 'h_ga5',
        'r_ht_gf': 'h_ht_gf5', 'r_ht_ga': 'h_ht_ga5',
        'r_pts': 'h_pts5', 'r_n': 'h_n5',
    })
    df = df.merge(h_form5, on=['fixture_id', 'home_id'], how='left')

    # 客队近5场（全场）
    a_form5 = form5[form5['is_home'] == False][
        ['fixture_id', 'team_id'] + [f'r_{c}' for c in
         ['win', 'draw', 'loss', 'gf', 'ga', 'ht_gf', 'ht_ga', 'pts', 'n']]
    ].rename(columns={
        'team_id': 'away_id',
        'r_win': 'a_win5', 'r_draw': 'a_draw5', 'r_loss': 'a_loss5',
        'r_gf': 'a_gf5', 'r_ga': 'a_ga5',
        'r_ht_gf': 'a_ht_gf5', 'r_ht_ga': 'a_ht_ga5',
        'r_pts': 'a_pts5', 'r_n': 'a_n5',
    })
    df = df.merge(a_form5, on=['fixture_id', 'away_id'], how='left')

    # 近10场
    h_form10 = form10[form10['is_home'] == True][
        ['fixture_id', 'team_id', 'r_win', 'r_gf', 'r_ga']
    ].rename(columns={
        'team_id': 'home_id',
        'r_win': 'h_win10', 'r_gf': 'h_gf10', 'r_ga': 'h_ga10',
    })
    df = df.merge(h_form10, on=['fixture_id', 'home_id'], how='left')

    a_form10 = form10[form10['is_home'] == False][
        ['fixture_id', 'team_id', 'r_win', 'r_gf', 'r_ga']
    ].rename(columns={
        'team_id': 'away_id',
        'r_win': 'a_win10', 'r_gf': 'a_gf10', 'r_ga': 'a_ga10',
    })
    df = df.merge(a_form10, on=['fixture_id', 'away_id'], how='left')

    # 主场/客场分拆
    h_home5 = form_home5[
        ['fixture_id', 'team_id', 'r_win', 'r_gf', 'r_ga']
    ].rename(columns={
        'team_id': 'home_id',
        'r_win': 'h_home_win5', 'r_gf': 'h_home_gf5', 'r_ga': 'h_home_ga5',
    })
    df = df.merge(h_home5, on=['fixture_id', 'home_id'], how='left')

    a_away5 = form_away5[
        ['fixture_id', 'team_id', 'r_win', 'r_gf', 'r_ga']
    ].rename(columns={
        'team_id': 'away_id',
        'r_win': 'a_away_win5', 'r_gf': 'a_away_gf5', 'r_ga': 'a_away_ga5',
    })
    df = df.merge(a_away5, on=['fixture_id', 'away_id'], how='left')

    # 差值特征
    df['win_rate_diff'] = df['h_win5'] - df['a_win5']
    df['gf_diff5'] = df['h_gf5'] - df['a_gf5']
    df['pts_diff5'] = df['h_pts5'] - df['a_pts5']

    # 序列特征（主队）
    h_seq = seq5[seq5['is_home'] == True][
        ['fixture_id', 'team_id', 'momentum'] + [f'seq_{i}' for i in range(1, 6)]
    ].rename(columns={
        'team_id': 'home_id', 'momentum': 'h_momentum',
        **{f'seq_{i}': f'h_result_{i}' for i in range(1, 6)}
    })
    df = df.merge(h_seq, on=['fixture_id', 'home_id'], how='left')

    # 序列特征（客队）
    a_seq = seq5[seq5['is_home'] == False][
        ['fixture_id', 'team_id', 'momentum'] + [f'seq_{i}' for i in range(1, 6)]
    ].rename(columns={
        'team_id': 'away_id', 'momentum': 'a_momentum',
        **{f'seq_{i}': f'a_result_{i}' for i in range(1, 6)}
    })
    df = df.merge(a_seq, on=['fixture_id', 'away_id'], how='left')

    # 比赛统计（主队）
    h_ms = ms5.rename(columns={
        'team_id': 'home_id',
        'ms_xg': 'h_xg5', 'ms_shots': 'h_shots5', 'ms_shots_on': 'h_shots_on5',
        'ms_possession': 'h_possession5', 'ms_corners': 'h_corners5',
        'ms_passes_pct': 'h_passes_pct5', 'ms_fouls': 'h_fouls5',
    })
    df = df.merge(h_ms, on=['fixture_id', 'home_id'], how='left')

    # 比赛统计（客队）
    a_ms = ms5.rename(columns={
        'team_id': 'away_id',
        'ms_xg': 'a_xg5', 'ms_shots': 'a_shots5', 'ms_shots_on': 'a_shots_on5',
        'ms_possession': 'a_possession5', 'ms_corners': 'a_corners5',
        'ms_passes_pct': 'a_passes_pct5', 'ms_fouls': 'a_fouls5',
    })
    df = df.merge(a_ms, on=['fixture_id', 'away_id'], how='left')

    df['xg_diff5'] = df.get('h_xg5', np.nan) - df.get('a_xg5', np.nan)

    # H2H
    df = df.merge(h2h, on='fixture_id', how='left')

    # 积分榜
    s_home_cols = ['home_id', 'league_id', 'season', 'h_rank', 'h_points',
                   'h_goals_diff', 'h_home_played', 'h_home_win',
                   'h_home_gf', 'h_home_ga']
    s_home = standings_home[[c for c in s_home_cols if c in standings_home.columns]]
    df = df.merge(s_home, on=['home_id', 'league_id', 'season'], how='left')

    s_away_cols = ['away_id', 'league_id', 'season', 'a_rank', 'a_points',
                   'a_goals_diff', 'a_away_played', 'a_away_win',
                   'a_away_gf', 'a_away_ga']
    s_away = standings_away[[c for c in s_away_cols if c in standings_away.columns]]
    df = df.merge(s_away, on=['away_id', 'league_id', 'season'], how='left')

    # 积分榜衍生
    if 'h_home_played' in df.columns:
        df['h_home_win_rate_s'] = df['h_home_win'] / df['h_home_played'].replace(0, np.nan)
        df['h_home_gf_avg_s'] = df['h_home_gf'] / df['h_home_played'].replace(0, np.nan)
        df['h_home_ga_avg_s'] = df['h_home_ga'] / df['h_home_played'].replace(0, np.nan)
    if 'a_away_played' in df.columns:
        df['a_away_win_rate_s'] = df['a_away_win'] / df['a_away_played'].replace(0, np.nan)
        df['a_away_gf_avg_s'] = df['a_away_gf'] / df['a_away_played'].replace(0, np.nan)
        df['a_away_ga_avg_s'] = df['a_away_ga'] / df['a_away_played'].replace(0, np.nan)

    if 'h_rank' in df.columns and 'a_rank' in df.columns:
        df['rank_diff'] = df['h_rank'] - df['a_rank']
        df['points_diff'] = df.get('h_points', 0) - df.get('a_points', 0)

    # 赛事背景
    df['is_cup'] = df['league_id'].isin(CUP_LEAGUES).astype(int)

    # 标签
    total = df['goals_home'] + df['goals_away']
    df['label_win'] = np.where(df['goals_home'] > df['goals_away'], 2,
                      np.where(df['goals_home'] == df['goals_away'], 1, 0))
    df['label_over25'] = (total >= 3).astype(int)
    df['label_goals_range'] = np.where(total <= 1, 0,
                              np.where(total <= 3, 1,
                              np.where(total <= 5, 2, 3)))

    # 过滤：至少需要近3场数据
    df = df[df['h_n5'] >= 3].copy()
    logger.info(f"过滤后有效样本: {len(df):,}")

    # 删除中间列
    drop_cols = ['h_n5', 'a_n5', 'h_home_played', 'h_home_win', 'h_home_gf',
                 'h_home_ga', 'a_away_played', 'a_away_win', 'a_away_gf', 'a_away_ga',
                 'date', 'enabled']
    df = df.drop(columns=[c for c in drop_cols if c in df.columns])

    df.to_csv(OUTPUT_PATH, index=False)
    logger.info(f"已写入: {OUTPUT_PATH}  ({len(df):,} 行, {len(df.columns)} 列)")


if __name__ == '__main__':
    main()


# ──────────────────────────────────────────────
# 单场推理用：给定 fixture_id，实时提取特征
# ──────────────────────────────────────────────

def _form_stats_single(db, team_id, before_date, home_only=False, away_only=False, n=5):
    if home_only:
        side = "AND f.home_id = :tid"
    elif away_only:
        side = "AND f.away_id = :tid"
    else:
        side = "AND (f.home_id = :tid OR f.away_id = :tid)"

    rows = db.execute(text(f"""
        SELECT home_id, away_id, goals_home, goals_away, halftime_home, halftime_away
        FROM fixtures f
        WHERE {side.replace('AND ', '')}
          AND f.date < :dt
          AND f.status_short IN ('FT','AET','PEN')
          AND f.goals_home IS NOT NULL
        ORDER BY f.date DESC LIMIT :n
    """), {"tid": team_id, "dt": before_date, "n": n}).fetchall()

    if not rows:
        return {}

    wins = draws = losses = gf = ga = ht_gf = ht_ga = 0
    for r in rows:
        is_home = (r[0] == team_id)
        _gf = r[2] if is_home else r[3]
        _ga = r[3] if is_home else r[2]
        _ht_gf = r[4] if is_home else r[5]
        _ht_ga = r[5] if is_home else r[4]
        gf += _gf or 0; ga += _ga or 0
        ht_gf += _ht_gf or 0; ht_ga += _ht_ga or 0
        if _gf > _ga: wins += 1
        elif _gf == _ga: draws += 1
        else: losses += 1

    nn = len(rows)
    return {
        "win_rate": wins / nn, "draw_rate": draws / nn, "loss_rate": losses / nn,
        "goals_for_avg": gf / nn, "goals_against_avg": ga / nn,
        "ht_goals_for_avg": ht_gf / nn, "ht_goals_against_avg": ht_ga / nn,
        "points_avg": (wins * 3 + draws) / nn, "n": nn,
    }


def _match_stats_single(db, team_id, before_date, n=5):
    rows = db.execute(text("""
        SELECT fs.fixture_id, fs.stat_type, fs.stat_value
        FROM fixture_statistics fs
        JOIN fixtures f ON fs.fixture_id = f.id
        WHERE fs.team_id = :tid AND f.date < :dt
          AND f.status_short IN ('FT','AET','PEN')
          AND fs.stat_type IN ('expected_goals','Total Shots','Shots on Goal',
                               'Ball Possession','Corner Kicks','Passes %','Fouls')
        ORDER BY f.date DESC LIMIT :lim
    """), {"tid": team_id, "dt": before_date, "lim": n * 10}).fetchall()

    from collections import defaultdict
    fstats = defaultdict(dict)
    order = []
    for r in rows:
        if r[0] not in fstats and len(order) >= n:
            break
        if r[0] not in fstats:
            order.append(r[0])
        fstats[r[0]][r[1]] = r[2]

    def pf(v):
        if v is None or v == 'None': return None
        try: return float(str(v).replace('%', ''))
        except: return None

    def avg(lst): return sum(lst) / len(lst) if lst else None

    xg, shots, shots_on, poss, corners, passes, fouls = [], [], [], [], [], [], []
    for fid in order:
        s = fstats[fid]
        v = pf(s.get('expected_goals')); xg.append(v) if v is not None else None
        v = pf(s.get('Total Shots')); shots.append(v) if v is not None else None
        v = pf(s.get('Shots on Goal')); shots_on.append(v) if v is not None else None
        v = pf(s.get('Ball Possession')); poss.append(v) if v is not None else None
        v = pf(s.get('Corner Kicks')); corners.append(v) if v is not None else None
        v = pf(s.get('Passes %')); passes.append(v) if v is not None else None
        v = pf(s.get('Fouls')); fouls.append(v) if v is not None else None

    return {
        "xg_avg": avg(xg), "shots_avg": avg(shots), "shots_on_avg": avg(shots_on),
        "possession_avg": avg(poss), "corners_avg": avg(corners),
        "passes_pct_avg": avg(passes), "fouls_avg": avg(fouls),
    }


def _result_sequence_single(db, team_id, before_date, n=5):
    """单场推理用：获取近 n 场逐场结果序列和动量"""
    rows = db.execute(text("""
        SELECT home_id, away_id, goals_home, goals_away
        FROM fixtures
        WHERE (home_id=:tid OR away_id=:tid)
          AND date < :dt AND status_short IN ('FT','AET','PEN')
          AND goals_home IS NOT NULL
        ORDER BY date DESC LIMIT :n
    """), {"tid": team_id, "dt": before_date, "n": n}).fetchall()

    result = {}
    wins = []
    for i, r in enumerate(rows):
        is_home = (r[0] == team_id)
        gf = r[2] if is_home else r[3]
        ga = r[3] if is_home else r[2]
        if gf > ga:
            v, w = 2, 1.0
        elif gf == ga:
            v, w = 1, 0.0
        else:
            v, w = 0, 0.0
        result[f'result_{i+1}'] = v
        wins.append(w)

    # 填充不足 n 场的
    for i in range(len(rows), n):
        result[f'result_{i+1}'] = None

    win3 = sum(wins[:3]) / min(3, len(wins)) if wins else None
    win5 = sum(wins[:5]) / min(5, len(wins)) if len(wins) >= 3 else None
    result['momentum'] = (win3 - win5) if (win3 is not None and win5 is not None) else None
    return result


def _h2h_single(db, home_id, away_id, before_date, n=5):
    rows = db.execute(text("""
        SELECT home_id, away_id, goals_home, goals_away
        FROM fixtures
        WHERE ((home_id=:hid AND away_id=:aid) OR (home_id=:aid AND away_id=:hid))
          AND date < :dt AND status_short IN ('FT','AET','PEN')
          AND goals_home IS NOT NULL
        ORDER BY date DESC LIMIT :n
    """), {"hid": home_id, "aid": away_id, "dt": before_date, "n": n}).fetchall()

    if not rows: return {}
    hw = dw = goals = over = 0
    for r in rows:
        gf = r[2] if r[0] == home_id else r[3]
        ga = r[3] if r[0] == home_id else r[2]
        t = (gf or 0) + (ga or 0)
        goals += t
        if gf > ga: hw += 1
        elif gf == ga: dw += 1
        if t >= 3: over += 1
    nn = len(rows)
    return {"h2h_home_win": hw/nn, "h2h_draw": dw/nn,
            "h2h_avg_goals": goals/nn, "h2h_over25": over/nn, "h2h_n": nn}


def _standings_single(db, team_id, league_id, season, side='home'):
    row = db.execute(text("""
        SELECT `rank`, points, goals_diff,
               home_played, home_win, home_goals_for, home_goals_against,
               away_played, away_win, away_goals_for, away_goals_against
        FROM standings
        WHERE team_id=:tid AND league_id=:lid AND season=:s LIMIT 1
    """), {"tid": team_id, "lid": league_id, "s": season}).fetchone()
    if not row: return {}
    d = dict(row._mapping)
    res = {"rank": d["rank"], "points": d["points"], "goals_diff": d["goals_diff"]}
    if side == 'home' and d.get("home_played"):
        res["home_win_rate_s"] = d["home_win"] / d["home_played"]
        res["home_gf_avg_s"] = (d["home_goals_for"] or 0) / d["home_played"]
        res["home_ga_avg_s"] = (d["home_goals_against"] or 0) / d["home_played"]
    elif side == 'away' and d.get("away_played"):
        res["away_win_rate_s"] = d["away_win"] / d["away_played"]
        res["away_gf_avg_s"] = (d["away_goals_for"] or 0) / d["away_played"]
        res["away_ga_avg_s"] = (d["away_goals_against"] or 0) / d["away_played"]
    return res


def extract_features_for_fixture(db, fixture_row: dict) -> dict | None:
    """单场推理用特征提取（供 predict.py 调用）"""
    fid = fixture_row["id"]
    home_id = fixture_row["home_id"]
    away_id = fixture_row["away_id"]
    league_id = fixture_row["league_id"]
    season = fixture_row["season"]
    match_date = fixture_row["date"]

    hf5 = _form_stats_single(db, home_id, match_date, n=5)
    af5 = _form_stats_single(db, away_id, match_date, n=5)
    if hf5.get("n", 0) < 3 or af5.get("n", 0) < 3:
        return None

    hf10 = _form_stats_single(db, home_id, match_date, n=10)
    af10 = _form_stats_single(db, away_id, match_date, n=10)
    hf_home = _form_stats_single(db, home_id, match_date, home_only=True, n=5)
    af_away = _form_stats_single(db, away_id, match_date, away_only=True, n=5)
    hms = _match_stats_single(db, home_id, match_date)
    ams = _match_stats_single(db, away_id, match_date)
    h2h = _h2h_single(db, home_id, away_id, match_date)
    hs = _standings_single(db, home_id, league_id, season, 'home')
    as_ = _standings_single(db, away_id, league_id, season, 'away')
    h_seq = _result_sequence_single(db, home_id, match_date, n=5)
    a_seq = _result_sequence_single(db, away_id, match_date, n=5)

    return {
        "fixture_id": fid, "league_id": league_id, "season": season,
        "is_cup": 1 if league_id in CUP_LEAGUES else 0,
        "h_win5": hf5.get("win_rate"), "h_draw5": hf5.get("draw_rate"),
        "h_loss5": hf5.get("loss_rate"), "h_gf5": hf5.get("goals_for_avg"),
        "h_ga5": hf5.get("goals_against_avg"), "h_pts5": hf5.get("points_avg"),
        "h_ht_gf5": hf5.get("ht_goals_for_avg"), "h_ht_ga5": hf5.get("ht_goals_against_avg"),
        "a_win5": af5.get("win_rate"), "a_draw5": af5.get("draw_rate"),
        "a_loss5": af5.get("loss_rate"), "a_gf5": af5.get("goals_for_avg"),
        "a_ga5": af5.get("goals_against_avg"), "a_pts5": af5.get("points_avg"),
        "a_ht_gf5": af5.get("ht_goals_for_avg"), "a_ht_ga5": af5.get("ht_goals_against_avg"),
        "h_win10": hf10.get("win_rate"), "h_gf10": hf10.get("goals_for_avg"),
        "h_ga10": hf10.get("goals_against_avg"),
        "a_win10": af10.get("win_rate"), "a_gf10": af10.get("goals_for_avg"),
        "a_ga10": af10.get("goals_against_avg"),
        "h_home_win5": hf_home.get("win_rate"), "h_home_gf5": hf_home.get("goals_for_avg"),
        "h_home_ga5": hf_home.get("goals_against_avg"),
        "a_away_win5": af_away.get("win_rate"), "a_away_gf5": af_away.get("goals_for_avg"),
        "a_away_ga5": af_away.get("goals_against_avg"),
        "win_rate_diff": (hf5.get("win_rate") or 0) - (af5.get("win_rate") or 0),
        "gf_diff5": (hf5.get("goals_for_avg") or 0) - (af5.get("goals_for_avg") or 0),
        "pts_diff5": (hf5.get("points_avg") or 0) - (af5.get("points_avg") or 0),
        "h_xg5": hms.get("xg_avg"), "h_shots5": hms.get("shots_avg"),
        "h_shots_on5": hms.get("shots_on_avg"), "h_possession5": hms.get("possession_avg"),
        "h_corners5": hms.get("corners_avg"), "h_passes_pct5": hms.get("passes_pct_avg"),
        "h_fouls5": hms.get("fouls_avg"),
        "a_xg5": ams.get("xg_avg"), "a_shots5": ams.get("shots_avg"),
        "a_shots_on5": ams.get("shots_on_avg"), "a_possession5": ams.get("possession_avg"),
        "a_corners5": ams.get("corners_avg"), "a_passes_pct5": ams.get("passes_pct_avg"),
        "a_fouls5": ams.get("fouls_avg"),
        "xg_diff5": (hms.get("xg_avg") or 0) - (ams.get("xg_avg") or 0),
        "h2h_home_win": h2h.get("h2h_home_win"), "h2h_draw": h2h.get("h2h_draw"),
        "h2h_avg_goals": h2h.get("h2h_avg_goals"), "h2h_over25": h2h.get("h2h_over25"),
        "h2h_n": h2h.get("h2h_n", 0),
        "h_rank": hs.get("rank"), "h_points": hs.get("points"),
        "h_goals_diff": hs.get("goals_diff"),
        "h_home_win_rate_s": hs.get("home_win_rate_s"),
        "h_home_gf_avg_s": hs.get("home_gf_avg_s"),
        "h_home_ga_avg_s": hs.get("home_ga_avg_s"),
        "a_rank": as_.get("rank"), "a_points": as_.get("points"),
        "a_goals_diff": as_.get("goals_diff"),
        "a_away_win_rate_s": as_.get("away_win_rate_s"),
        "a_away_gf_avg_s": as_.get("away_gf_avg_s"),
        "a_away_ga_avg_s": as_.get("away_ga_avg_s"),
        "rank_diff": (hs.get("rank") or 0) - (as_.get("rank") or 0),
        "points_diff": (hs.get("points") or 0) - (as_.get("points") or 0),
        # 序列特征
        "h_result_1": h_seq.get("result_1"), "h_result_2": h_seq.get("result_2"),
        "h_result_3": h_seq.get("result_3"), "h_result_4": h_seq.get("result_4"),
        "h_result_5": h_seq.get("result_5"), "h_momentum": h_seq.get("momentum"),
        "a_result_1": a_seq.get("result_1"), "a_result_2": a_seq.get("result_2"),
        "a_result_3": a_seq.get("result_3"), "a_result_4": a_seq.get("result_4"),
        "a_result_5": a_seq.get("result_5"), "a_momentum": a_seq.get("momentum"),
    }
