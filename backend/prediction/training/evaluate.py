"""评估指标：accuracy、AUC、RPS"""

import numpy as np
from sklearn.metrics import accuracy_score, roc_auc_score, log_loss


def accuracy_3way(y_true, probs):
    pred = np.argmax(probs, axis=1)
    return accuracy_score(y_true, pred)


def rps_3way(y_true, probs):
    """Ranked Probability Score（越低越好）"""
    n = len(y_true)
    total = 0.0
    for i in range(n):
        true_label = int(y_true.iloc[i] if hasattr(y_true, 'iloc') else y_true[i])
        cum_pred = np.cumsum(probs[i])
        cum_true = np.cumsum([1 if j == true_label else 0 for j in range(3)])
        total += np.sum((cum_pred - cum_true) ** 2) / 2
    return total / n


def evaluate_win(model, X_test, y_test) -> dict:
    from prediction.training.model import _fill_na
    X = _fill_na(X_test)
    probs = model.predict_proba(X)
    return {
        'accuracy_3way': accuracy_3way(y_test, probs),
        'rps': rps_3way(y_test, probs),
        'logloss': log_loss(y_test, probs),
        'n_test': len(y_test),
    }


def evaluate_over25(model, X_test, y_test) -> dict:
    from prediction.training.model import _fill_na
    X = _fill_na(X_test)
    probs = model.predict_proba(X)[:, 1]
    pred = (probs >= 0.5).astype(int)
    return {
        'accuracy': accuracy_score(y_test, pred),
        'auc': roc_auc_score(y_test, probs),
        'n_test': len(y_test),
    }


def evaluate_goals_range(model, X_test, y_test) -> dict:
    from prediction.training.model import _fill_na
    X = _fill_na(X_test)
    pred = model.predict(X)
    return {
        'accuracy': accuracy_score(y_test, pred),
        'n_test': len(y_test),
    }


def evaluate_lambda(model_home, model_away, X_test, df_test,
                    baselines: dict | None = None,
                    medians: dict | None = None) -> dict:
    """λ 回归器的样本外指标。

    baselines 必须是**训练段**的主/客场均进球（不能取测试集均值，否则泄漏）。
    关注的不是 λ 的单点误差，而是它能否给出正确的强弱方向：
    sign_agreement = sign(λ_home - λ_away) 与 sign(进球差) 的一致率。
    """
    from prediction.training.model import fill_na_with_medians

    X = fill_na_with_medians(X_test, medians)
    gh = np.asarray(df_test['goals_home'], dtype=float)
    ga = np.asarray(df_test['goals_away'], dtype=float)
    lh = np.asarray(model_home.predict(X), dtype=float)
    la = np.asarray(model_away.predict(X), dtype=float)

    baselines = baselines or {}
    base_h = float(baselines.get('home', gh.mean()))
    base_a = float(baselines.get('away', ga.mean()))

    rmse_home = float(np.sqrt(np.mean((lh - gh) ** 2)))
    rmse_away = float(np.sqrt(np.mean((la - ga) ** 2)))
    base_rmse_home = float(np.sqrt(np.mean((base_h - gh) ** 2)))
    base_rmse_away = float(np.sqrt(np.mean((base_a - ga) ** 2)))

    def _safe_corr(pred, actual) -> float:
        # 预测退化为常数（std=0）时 np.corrcoef 会返回 NaN，报告里用 0.0 表示无相关性。
        if float(np.std(pred)) < 1e-12 or float(np.std(actual)) < 1e-12:
            return 0.0
        return float(np.corrcoef(pred, actual)[0, 1])

    return {
        'n_test': len(gh),
        'rmse_home': round(rmse_home, 4),
        'rmse_away': round(rmse_away, 4),
        'rmse_mean': round((rmse_home + rmse_away) / 2, 4),
        'baseline_rmse_mean': round((base_rmse_home + base_rmse_away) / 2, 4),
        'rmse_vs_baseline': round((rmse_home + rmse_away) / 2
                                  - (base_rmse_home + base_rmse_away) / 2, 4),
        'corr_home': round(_safe_corr(lh, gh), 4),
        'corr_away': round(_safe_corr(la, ga), 4),
        'sign_agreement': round(float(np.mean(np.sign(lh - la) == np.sign(gh - ga))), 4),
        'mean_lambda_home': round(float(lh.mean()), 4),
        'mean_lambda_away': round(float(la.mean()), 4),
    }


# RMSE 与方向一致率都接近时的固定偏好顺序，保证择优可复现而不是人工挑选。
LAMBDA_VARIANT_PREFERENCE = ('regularized_squared_error', 'poisson_count')


def select_lambda_variant(metric_rows: list[dict]) -> tuple[str | None, dict]:
    """按固定规则从候选路线中选一个：先看 RMSE，差距不足 1% 再看方向一致率。"""
    valid = [r for r in metric_rows if r.get('rmse_mean') is not None]
    if not valid:
        return None, {}

    base = min(r['rmse_mean'] for r in valid)
    tol = 0.01 * max(base, 1e-9)
    pool = [r for r in valid if r['rmse_mean'] <= base + tol]

    if len(pool) == 1:
        winner, reason = pool[0], 'rmse_mean 最低'
    else:
        top = max(r['sign_agreement'] for r in pool)
        tied = [r for r in pool if abs(r['sign_agreement'] - top) < 1e-9]
        if len(tied) == 1:
            winner, reason = tied[0], 'RMSE 差距不足 1%，方向一致率更高'
        else:
            winner = None
            for name in LAMBDA_VARIANT_PREFERENCE:
                winner = next((r for r in tied if r['variant'] == name), None)
                if winner:
                    break
            winner = winner or tied[0]
            reason = 'RMSE 与方向一致率均相当，按固定顺序取 ' + winner['variant']

    return winner['variant'], {'winner': winner, 'reason': reason,
                               'compared': [r['variant'] for r in metric_rows]}


def evaluate_group(models: dict, X_test, df_test) -> dict:
    results = {}
    results['win'] = evaluate_win(models['win'], X_test, df_test['label_win'])
    results['over25'] = evaluate_over25(models['over25'], X_test, df_test['label_over25'])
    results['goals_range'] = evaluate_goals_range(
        models['goals_range'], X_test, df_test['label_goals_range']
    )
    return results
