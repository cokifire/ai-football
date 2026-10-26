"""λ 重训相关的回归测试。

锁住两条不变量：
1. 推理侧缺失特征必须由**持久化中位数**填充，而不是单行 DataFrame 的 median
   （后者等于没填：NaN 直接进 XGBoost 走缺失分支，与训练口径不一致）。
2. λ 路线的择优必须确定：RMSE → 方向一致率 → 固定顺序，不依赖字典遍历顺序。
"""

import pandas as pd
import pytest

from prediction.training import evaluate as ev
from prediction.training import model as tm


TRAIN = pd.DataFrame({
    'h_gf5': [1.0, 2.0, 3.0, 4.0],
    'a_gf5': [0.0, 2.0, 4.0, 6.0],
})


def test_single_row_nan_is_filled_by_persisted_median(monkeypatch):
    """单行推理的 NaN 必须取训练中位数；旧的 _fill_na 会原样留下 NaN。"""
    medians = tm.compute_feature_medians(TRAIN)
    assert medians['h_gf5'] == pytest.approx(2.5)

    single = pd.DataFrame([{'h_gf5': None, 'a_gf5': 9.0}])
    assert pd.isna(tm._fill_na(single)['h_gf5'].iloc[0])  # 旧行为：未被填充

    filled = tm.fill_na_with_medians(single, medians)
    assert filled['h_gf5'].iloc[0] == pytest.approx(2.5)
    assert filled['a_gf5'].iloc[0] == pytest.approx(9.0)  # 非缺失值不被覆盖


def test_no_nan_survives_even_for_unknown_columns(monkeypatch):
    """中位数表里没有的列也要兜底，绝不能把 NaN 送进模型。"""
    medians = {'h_gf5': 2.5}
    single = pd.DataFrame([{'h_gf5': None, 'brand_new_col': None}])
    filled = tm.fill_na_with_medians(single, medians)
    assert not filled.isna().any().any()


def test_load_feature_medians_respects_group_and_global(monkeypatch, tmp_path):
    path = tmp_path / 'feature_medians.pkl'
    monkeypatch.setattr(tm, 'FEATURE_MEDIANS_PATH', str(path))
    tm._load_all_feature_medians.cache_clear()

    assert tm.load_feature_medians('L_61') is None  # 文件不存在时返回 None

    tm.save_feature_medians({'L_61': {'a': 1.0}, 'GLOBAL': {'a': 7.0}})
    assert tm.load_feature_medians('L_61') == {'a': 1.0}
    assert tm.load_feature_medians('L_39') == {'a': 7.0}  # 缺组回退 GLOBAL

    tm._load_all_feature_medians.cache_clear()


class _StubModel:
    def __init__(self, value):
        self.value = value

    def predict(self, X):  # noqa: N803
        return [self.value] * len(X)


def test_evaluate_lambda_baseline_comes_from_train_mean():
    """基线必须是训练段均值，避免测试集信息泄漏。"""
    X_test = pd.DataFrame({'h_gf5': [1.0, 2.0], 'a_gf5': [1.0, 2.0]})
    df_test = pd.DataFrame({'goals_home': [1.0, 3.0], 'goals_away': [2.0, 0.0]})
    # 预测恒等于训练段均值 → RMSE 与基线完全一致
    metrics = ev.evaluate_lambda(_StubModel(2.0), _StubModel(1.0), X_test, df_test,
                                 baselines={'home': 2.0, 'away': 1.0})
    assert metrics['n_test'] == 2
    assert metrics['rmse_mean'] == pytest.approx(metrics['baseline_rmse_mean'])
    assert metrics['rmse_vs_baseline'] == pytest.approx(0.0)
    # λ_home > λ_away 恒定；实际一主胜一客胜 → 方向只对一半
    assert metrics['sign_agreement'] == pytest.approx(0.5)

    # 换成 UNABLE 与基线不同的固定值，差值必须如实体现为正
    worse = ev.evaluate_lambda(_StubModel(0.0), _StubModel(0.0), X_test, df_test,
                               baselines={'home': 2.0, 'away': 1.0})
    assert worse['rmse_vs_baseline'] > 0
    assert worse['sign_agreement'] == pytest.approx(0.0)


def test_select_lambda_variant_prefers_lower_rmse():
    rows = [
        {'variant': 'regularized_squared_error', 'rmse_mean': 1.10, 'sign_agreement': 0.51},
        {'variant': 'poisson_count', 'rmse_mean': 1.00, 'sign_agreement': 0.49},
    ]
    winner, info = ev.select_lambda_variant(rows)
    assert winner == 'poisson_count'
    assert 'rmse_mean 最低' in info['reason']


def test_select_lambda_variant_uses_sign_agreement_when_rmse_close():
    rows = [
        {'variant': 'regularized_squared_error', 'rmse_mean': 1.000, 'sign_agreement': 0.500},
        {'variant': 'poisson_count', 'rmse_mean': 1.005, 'sign_agreement': 0.560},
    ]
    winner, info = ev.select_lambda_variant(rows)
    assert winner == 'poisson_count'
    assert '方向一致率更高' in info['reason']


def test_select_lambda_variant_is_deterministic_on_tie():
    """RMSE 与方向一致率都相当时，按固定顺序取正则化平方误差，与传入顺序无关。"""
    base = [
        {'variant': 'regularized_squared_error', 'rmse_mean': 1.0, 'sign_agreement': 0.5},
        {'variant': 'poisson_count', 'rmse_mean': 1.0, 'sign_agreement': 0.5},
    ]
    assert ev.select_lambda_variant(base)[0] == 'regularized_squared_error'
    assert ev.select_lambda_variant(list(reversed(base)))[0] == 'regularized_squared_error'


def test_select_lambda_variant_handles_empty_rows():
    assert ev.select_lambda_variant([]) == (None, {})
