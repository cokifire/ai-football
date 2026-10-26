"""XGBoost 模型训练：4个目标（胜平负、大小球、进球区间、Poisson参数）"""

import os
import pickle
from functools import lru_cache
import numpy as np
import pandas as pd
from xgboost import XGBClassifier, XGBRegressor
from sklearn.model_selection import cross_val_score
from loguru import logger

MODELS_DIR = os.path.join(os.path.dirname(__file__), '..', 'models')
os.makedirs(MODELS_DIR, exist_ok=True)

FEATURE_MEDIANS_PATH = os.path.join(MODELS_DIR, 'feature_medians.pkl')


def _to_numeric(X: pd.DataFrame) -> pd.DataFrame:
    """统一数值化：整列非数值（如混入的字符串）会被转成 NaN 而不是报错。"""
    return X.apply(pd.to_numeric, errors='coerce')


def _fill_na(X: pd.DataFrame) -> pd.DataFrame:
    X = _to_numeric(X)
    return X.fillna(X.median(numeric_only=True))


def compute_feature_medians(X_train: pd.DataFrame) -> dict[str, float]:
    """计算**训练段**每个特征的中位数。

    推理侧必须用这一份中位数填充缺失值。此前 train 用全体样本中位数、
    serve 用 `X.median()` 单行中位数（等于没填，NaN 直接进 XGBoost 走缺失分支），
    造成 train/serve 口径不一致：本场 1552800 就有 7 个特征为 NaN。
    """
    med = _to_numeric(X_train).median(numeric_only=True)
    return {str(k): (0.0 if pd.isna(v) else float(v)) for k, v in med.items()}


def fill_na_with_medians(X: pd.DataFrame, medians: dict[str, float] | None) -> pd.DataFrame:
    """用持久化的训练中位数填充缺失特征；无中位数表时退回列中位数（旧行为）。"""
    X = _to_numeric(X)
    if not medians:
        return X.fillna(X.median(numeric_only=True))
    filled = X.fillna(pd.Series(medians, dtype='float64'))
    # 训练中位数里没有的列（特征列变更等）仍需兜底，避免把 NaN 送进模型。
    rest = filled.columns[filled.isna().any()].tolist()
    if rest:
        filled = filled.fillna(filled[rest].median(numeric_only=True))
        filled = filled.fillna(0.0)
    return filled


def save_feature_medians(medians_by_group: dict[str, dict[str, float]]) -> str:
    """持久化 {分组: {特征: 中位数}}，并让推理侧的缓存立即失效。"""
    with open(FEATURE_MEDIANS_PATH, 'wb') as f:
        pickle.dump(medians_by_group, f)
    _load_all_feature_medians.cache_clear()
    logger.info("特征中位数已写入 {}（{} 个分组）", FEATURE_MEDIANS_PATH, len(medians_by_group))
    return FEATURE_MEDIANS_PATH


@lru_cache(maxsize=1)
def _load_all_feature_medians() -> dict[str, dict[str, float]]:
    if not os.path.exists(FEATURE_MEDIANS_PATH):
        return {}
    try:
        with open(FEATURE_MEDIANS_PATH, 'rb') as f:
            return pickle.load(f)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取特征中位数失败 {}: {}", FEATURE_MEDIANS_PATH, e)
        return {}


def load_feature_medians(group_key: str | None) -> dict[str, float] | None:
    """按分组取训练中位数；缺组时用 GLOBAL 兜底，都没有则返回 None。"""
    if not group_key:
        return None
    medians = _load_all_feature_medians()
    if not medians:
        return None
    if group_key in medians:
        return medians[group_key]
    if 'GLOBAL' in medians:
        logger.warning("分组 {} 无专属特征中位数，回退 GLOBAL", group_key)
        return medians['GLOBAL']
    return None


def train_win_model(X_train, y_train, n_estimators=300, learning_rate=0.05,
                    max_depth=5, subsample=0.8, colsample=0.8):
    """M1: 胜平负 3分类"""
    model = XGBClassifier(
        objective='multi:softprob',
        num_class=3,
        n_estimators=n_estimators,
        learning_rate=learning_rate,
        max_depth=max_depth,
        subsample=subsample,
        colsample_bytree=colsample,
        eval_metric='mlogloss',
        random_state=42,
        n_jobs=-1,
    )
    model.fit(_fill_na(X_train), y_train)
    return model


def train_over25_model(X_train, y_train, n_estimators=300, learning_rate=0.05,
                       max_depth=4, subsample=0.8, colsample=0.8):
    """M2: 大小球 2分类"""
    model = XGBClassifier(
        objective='binary:logistic',
        n_estimators=n_estimators,
        learning_rate=learning_rate,
        max_depth=max_depth,
        subsample=subsample,
        colsample_bytree=colsample,
        eval_metric='logloss',
        random_state=42,
        n_jobs=-1,
    )
    model.fit(_fill_na(X_train), y_train)
    return model


def train_goals_range_model(X_train, y_train, n_estimators=300, learning_rate=0.05,
                             max_depth=4, subsample=0.8, colsample=0.8):
    """M3: 进球区间 4分类"""
    model = XGBClassifier(
        objective='multi:softprob',
        num_class=4,
        n_estimators=n_estimators,
        learning_rate=learning_rate,
        max_depth=max_depth,
        subsample=subsample,
        colsample_bytree=colsample,
        eval_metric='mlogloss',
        random_state=42,
        n_jobs=-1,
    )
    model.fit(_fill_na(X_train), y_train)
    return model


def train_lambda_model(X_train, y_home, y_away, n_estimators=200, learning_rate=0.05,
                       max_depth=4):
    """M4: 预测 λ_home / λ_away（用于 Poisson Top3 比分）"""
    X = _fill_na(X_train)
    m_home = XGBRegressor(
        objective='reg:squarederror',
        n_estimators=n_estimators,
        learning_rate=learning_rate,
        max_depth=max_depth,
        random_state=42,
        n_jobs=-1,
    )
    m_away = XGBRegressor(
        objective='reg:squarederror',
        n_estimators=n_estimators,
        learning_rate=learning_rate,
        max_depth=max_depth,
        random_state=42,
        n_jobs=-1,
    )
    m_home.fit(X, y_home)
    m_away.fit(X, y_away)
    return m_home, m_away


# λ 回归器的候选路线。旧配置（reg:squarederror 且无子采样/列采样/最小叶样本约束）
# 在样本外的相关系数只有 0.12~0.30、方向符号一致率接近抛硬币，RMSE 甚至不如直接
# 猜联赛均值，属典型过拟合（每组约 1000 行训练样本配上 78 个特征、200 棵树）。
LAMBDA_VARIANTS = ('regularized_squared_error', 'poisson_count')

LAMBDA_VARIANT_PARAMS = {
    # 路线一：保留平方误差回归，补齐子采样/列采样/最小叶样本与 L2 正则。
    'regularized_squared_error': {
        'objective': 'reg:squarederror',
        'n_estimators': 200,
        'learning_rate': 0.05,
        'max_depth': 4,
        'subsample': 0.8,
        'colsample_bytree': 0.8,
        'min_child_weight': 5,
        'reg_lambda': 2.0,
    },
    # 路线二：按计数数据建模，直接输出 Poisson 均值 λ（天然非负）。
    # 除 objective 外其余超参与路线一保持一致，保证比较公平。
    'poisson_count': {
        'objective': 'count:poisson',
        'n_estimators': 200,
        'learning_rate': 0.05,
        'max_depth': 4,
        'subsample': 0.8,
        'colsample_bytree': 0.8,
        'min_child_weight': 5,
        'reg_lambda': 2.0,
    },
}


def train_lambda_variant(variant: str, X_train: pd.DataFrame, y_home, y_away,
                         medians: dict[str, float] | None = None):
    """按指定路线训练 λ_home / λ_away。

    两条路线共用同一份训练中位数填充，保证与推理口径一致。
    """
    if variant not in LAMBDA_VARIANT_PARAMS:
        raise ValueError(f"未知 λ 训练路线: {variant}")
    params = dict(LAMBDA_VARIANT_PARAMS[variant])
    X = fill_na_with_medians(X_train, medians)
    logger.info("  λ 路线 {}: objective={} trees={} min_child_weight={} reg_lambda={}",
                variant, params['objective'], params['n_estimators'],
                params['min_child_weight'], params['reg_lambda'])

    models = {}
    for side, y in (('lambda_home', y_home), ('lambda_away', y_away)):
        model = XGBRegressor(random_state=42, n_jobs=-1, **params)
        model.fit(X, y)
        models[side] = model
    return models['lambda_home'], models['lambda_away']


def train_group(group_key: str, df_train: pd.DataFrame,
                feature_cols: list[str]) -> dict:
    """训练一个分组的全部模型，返回模型字典"""
    X = df_train[feature_cols]
    logger.info(f"  [{group_key}] 训练样本: {len(X):,}  特征: {len(feature_cols)}")

    models = {}

    models['win'] = train_win_model(X, df_train['label_win'])
    models['over25'] = train_over25_model(X, df_train['label_over25'])
    models['goals_range'] = train_goals_range_model(X, df_train['label_goals_range'])

    lh, la = train_lambda_model(
        X,
        df_train['goals_home'].clip(0, 10),
        df_train['goals_away'].clip(0, 10),
    )
    models['lambda_home'] = lh
    models['lambda_away'] = la

    return models


def save_models(group_key: str, models: dict):
    for name, model in models.items():
        path = os.path.join(MODELS_DIR, f'{group_key}_{name}.pkl')
        with open(path, 'wb') as f:
            pickle.dump(model, f)


def load_models(group_key: str) -> dict | None:
    names = ['win', 'over25', 'goals_range', 'lambda_home', 'lambda_away']
    models = {}
    for name in names:
        path = os.path.join(MODELS_DIR, f'{group_key}_{name}.pkl')
        if not os.path.exists(path):
            return None
        with open(path, 'rb') as f:
            models[name] = pickle.load(f)
    return models


def load_any_model(group_key: str, feature_cols: list[str]) -> dict | None:
    """按优先级加载：联赛专属 → 大组 → GLOBAL"""
    from prediction.training.data import assign_group
    # 尝试联赛专属
    m = load_models(group_key)
    if m:
        return m, group_key
    # 尝试大组
    if group_key.startswith('L_'):
        lid = int(group_key[2:])
        gname = assign_group(lid)
        m = load_models(gname)
        if m:
            return m, gname
    # GLOBAL 兜底
    m = load_models('GLOBAL')
    if m:
        return m, 'GLOBAL'
    return None, None
