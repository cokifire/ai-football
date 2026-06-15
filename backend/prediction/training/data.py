"""训练数据加载：读取 features.csv，按联赛分组，返回训练/测试集"""

import os
import pandas as pd
import numpy as np

FEATURES_PATH = os.path.join(os.path.dirname(__file__), '..', 'features.csv')

# 不参与模型输入的列
NON_FEATURE_COLS = {
    'fixture_id', 'league_id', 'season', 'home_id', 'away_id',
    'goals_home', 'goals_away',
    'label_win', 'label_over25', 'label_goals_range',
}

# 联赛分组（数据量不足时归入大组）
GROUP_MAP = {
    # 五大联赛独立
    39: 'PL', 140: 'LALIGA', 135: 'SERIEA', 78: 'BUNDESLIGA', 61: 'LIGUE1',
    # 欧洲二级
    40: 'EU2', 41: 'EU2', 62: 'EU2', 79: 'EU2', 94: 'EU2',
    88: 'EU2', 89: 'EU2', 144: 'EU2', 106: 'EU2', 103: 'EU2',
    113: 'EU2', 197: 'EU2', 283: 'EU2', 235: 'EU2', 333: 'EU2',
    141: 'EU2', 204: 'EU2', 203: 'EU2',
    # 欧洲杯赛
    2: 'EU_CUP', 3: 'EU_CUP', 848: 'EU_CUP', 45: 'EU_CUP',
    # 美洲
    253: 'AMERICAS', 128: 'AMERICAS', 71: 'AMERICAS', 262: 'AMERICAS',
    265: 'AMERICAS', 11: 'AMERICAS', 13: 'AMERICAS',
    # 亚洲/大洋洲
    98: 'ASIA', 99: 'ASIA', 292: 'ASIA', 293: 'ASIA', 296: 'ASIA',
    274: 'ASIA', 169: 'ASIA', 307: 'ASIA', 17: 'ASIA', 18: 'ASIA',
    188: 'ASIA', 323: 'ASIA',
    # 其他欧洲
    244: 'EU_OTHER', 666: 'EU_OTHER', 10: 'EU_OTHER',
}

MIN_SAMPLES = 300  # 低于此数量归入 GLOBAL


def load_features() -> pd.DataFrame:
    df = pd.read_csv(FEATURES_PATH)
    return df


def get_feature_cols(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if c not in NON_FEATURE_COLS]


def assign_group(league_id: int) -> str:
    return GROUP_MAP.get(league_id, 'GLOBAL')


def get_groups(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """返回 {group_name: DataFrame}，包含各联赛独立组 + 大组 + GLOBAL"""
    df = df.copy()
    df['_group'] = df['league_id'].apply(assign_group)

    groups: dict[str, pd.DataFrame] = {}

    # 先按联赛 ID 独立分组（数据量足够的）
    for lid, sub in df.groupby('league_id'):
        if len(sub) >= MIN_SAMPLES:
            key = f'L_{lid}'
            groups[key] = sub

    # 大组（归并小联赛）
    for gname in df['_group'].unique():
        sub = df[df['_group'] == gname]
        if len(sub) >= MIN_SAMPLES:
            groups[gname] = sub

    # GLOBAL 兜底
    groups['GLOBAL'] = df

    return groups


def train_test_split_temporal(df: pd.DataFrame, test_ratio: float = 0.2):
    """按时间顺序切分，后 test_ratio 作为测试集"""
    n = len(df)
    split = int(n * (1 - test_ratio))
    return df.iloc[:split].copy(), df.iloc[split:].copy()
