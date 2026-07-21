"""XGBoost 模型训练：4个目标（胜平负、大小球、进球区间、Poisson参数）"""

import os
import pickle
import numpy as np
import pandas as pd
from xgboost import XGBClassifier, XGBRegressor
import xgboost as xgb
from sklearn.model_selection import cross_val_score
from loguru import logger

MODELS_DIR = os.path.join(os.path.dirname(__file__), '..', 'models')
os.makedirs(MODELS_DIR, exist_ok=True)


class _XGBClassifierPortable:
    """跨平台分类模型包装：直接用 Booster API 预测，规避 pickle 在 Windows/Linux 间
    保存的原生 booster 字节不兼容（加载时会 segfault，导致后端进程崩溃）。"""
    def __init__(self, booster, n_classes: int):
        self.booster = booster
        self.n_classes_ = n_classes

    def predict_proba(self, X):
        d = xgb.DMatrix(np.asarray(X, dtype=np.float32))
        if self.n_classes_ > 2:
            return np.asarray(self.booster.predict(d))  # softmax 概率 (n, n_classes)
        p1 = np.asarray(self.booster.predict(d)).reshape(-1, 1)
        return np.column_stack([1 - p1, p1])

    def predict(self, X):
        return np.argmax(self.predict_proba(X), axis=1)


class _XGBRegressorPortable:
    """跨平台回归模型包装（lambda_home / lambda_away）。"""
    def __init__(self, booster):
        self.booster = booster

    def predict(self, X):
        d = xgb.DMatrix(np.asarray(X, dtype=np.float32))
        return np.asarray(self.booster.predict(d)).ravel()


def _fill_na(X: pd.DataFrame) -> pd.DataFrame:
    X = X.apply(pd.to_numeric, errors='coerce')
    return X.fillna(X.median(numeric_only=True))


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
    """保存为 XGBoost 原生跨平台 JSON 格式（UBJSON），避免 pickle 在
    不同 OS / 编译器间不兼容导致加载时 segfault。"""
    for name, model in models.items():
        path = os.path.join(MODELS_DIR, f'{group_key}_{name}.json')
        model.save_model(path)


def load_models(group_key: str) -> dict | None:
    """优先加载跨平台 .json；若只有 .pkl（且位于同 OS）则回退以兼容旧环境。"""
    names = ['win', 'over25', 'goals_range', 'lambda_home', 'lambda_away']
    models = {}
    for name in names:
        json_path = os.path.join(MODELS_DIR, f'{group_key}_{name}.json')
        pkl_path = os.path.join(MODELS_DIR, f'{group_key}_{name}.pkl')
        if os.path.exists(json_path):
            booster = xgb.Booster()
            booster.load_model(json_path)
            if name in ('lambda_home', 'lambda_away'):
                models[name] = _XGBRegressorPortable(booster)
            else:
                nc = booster.num_class
                models[name] = _XGBClassifierPortable(booster, nc if nc and nc > 1 else 2)
        elif os.path.exists(pkl_path):
            # 同 OS 下的旧格式回退（跨 OS 加载 .pkl 可能 segfault）
            with open(pkl_path, 'rb') as f:
                models[name] = pickle.load(f)
        else:
            return None
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
