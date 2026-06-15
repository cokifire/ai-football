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


def evaluate_group(models: dict, X_test, df_test) -> dict:
    results = {}
    results['win'] = evaluate_win(models['win'], X_test, df_test['label_win'])
    results['over25'] = evaluate_over25(models['over25'], X_test, df_test['label_over25'])
    results['goals_range'] = evaluate_goals_range(
        models['goals_range'], X_test, df_test['label_goals_range']
    )
    return results
