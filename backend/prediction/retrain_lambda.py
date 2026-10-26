"""λ 专用训练入口：只重训 lambda_home / lambda_away，不触碰分类器。

背景：现有 λ 回归器在线上的表现为「主胜 82.6% 却给出客让 0.5 球」，
样本外实测方向与抛硬币无异（各联赛组符号一致率 0.48~0.53，RMSE 与猜均值相当），
根因是过拟合 + train/serve 缺失值口径不一致。本脚本做两件事：

1. 对每条候选路线（正则化平方误差 / 泊松计数回归）各训一版，用时间后 20% 的
   样本外指标（RMSE + 方向符号一致率）按固定规则择优。
2. 持久化各分组的**训练段**特征中位数，推理侧复用同一份，
   不再用单行 DataFrame 的 median（那等于没填充）。

产物（直接写入 prediction/models/）：
  {group}_lambda_home.pkl / {group}_lambda_away.pkl   仅 λ 模型，分类器不变
  feature_medians.pkl                                 各组训练中位数
  ../lambda_metrics.csv / ../lambda_selection.json    评估明细与择优结果

用法：
    PYTHONPATH=backend python -m prediction.retrain_lambda
    PYTHONPATH=backend python -m prediction.retrain_lambda --groups L_61,L_39
"""

import argparse
import csv
import json
import os
import pickle
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from loguru import logger  # noqa: E402

from prediction.training.data import (  # noqa: E402
    load_features, get_groups, train_test_split_temporal,
)
from prediction.training.evaluate import evaluate_lambda, select_lambda_variant  # noqa: E402
from prediction.training.model import (  # noqa: E402
    LAMBDA_VARIANTS, LAMBDA_VARIANT_PARAMS, MODELS_DIR, compute_feature_medians,
    save_feature_medians, save_models, train_lambda_variant,
)

FEATURE_COLS_PATH = os.path.join(MODELS_DIR, 'feature_cols.pkl')
REPORT_DIR = os.path.dirname(os.path.abspath(__file__))
METRICS_PATH = os.path.join(REPORT_DIR, 'lambda_metrics.csv')
SELECTION_PATH = os.path.join(REPORT_DIR, 'lambda_selection.json')

MIN_TRAIN_ROWS = 100


def _load_persisted_feature_cols() -> list[str]:
    """复用现役特征列清单，保证与未重训的分类器使用**完全相同**的特征顺序。

    新版 features.csv 若缺少任一既有用特征列，直接报错而不是静默错位。
    """
    if not os.path.exists(FEATURE_COLS_PATH):
        raise FileNotFoundError(
            f"缺少 {FEATURE_COLS_PATH}；请先跑一次完整训练（prediction/train.py）"
        )
    with open(FEATURE_COLS_PATH, 'rb') as f:
        cols = pickle.load(f)
    if not cols:
        raise ValueError("feature_cols.pkl 为空")
    return list(cols)


def _write_reports(metric_rows: list[dict], selection: dict) -> None:
    if metric_rows:
        fields = list(metric_rows[0].keys())
        with open(METRICS_PATH, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(metric_rows)
        logger.info("评估明细已写入 {}", METRICS_PATH)
    with open(SELECTION_PATH, 'w', encoding='utf-8') as f:
        json.dump(selection, f, ensure_ascii=False, indent=2)
    logger.info("择优结果已写入 {}", SELECTION_PATH)


def run(only_groups: list[str] | None = None) -> dict:
    df = load_features()
    logger.info("总样本: {:,}", len(df))

    feature_cols = _load_persisted_feature_cols()
    missing = [c for c in feature_cols if c not in df.columns]
    if missing:
        raise ValueError(
            f"features.csv 缺少 {len(missing)} 个在用特征列（如 {missing[:5]}），"
            f"继续训练会导致特征错位，已中止"
        )
    logger.info("复用特征列 {} 个（与现役分类器一致）", len(feature_cols))

    groups = get_groups(df)
    if only_groups:
        unknown = [g for g in only_groups if g not in groups]
        if unknown:
            raise ValueError(f"未知分组: {unknown}；可用分组: {sorted(groups)}")
        groups = {g: groups[g] for g in only_groups}
    logger.info("分组数: {}", len(groups))

    metric_rows: list[dict] = []
    medians_by_group: dict[str, dict[str, float]] = {}
    selection: dict = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'targets': ['lambda_home', 'lambda_away'],
        'variants': {k: dict(v) for k, v in LAMBDA_VARIANT_PARAMS.items()},
        'test_ratio': 0.2,
        'min_train_rows': MIN_TRAIN_ROWS,
        'groups': {},
        'skipped': {},
        'failed': {},
    }

    for group_key, group_df in groups.items():
        df_train, df_test = train_test_split_temporal(group_df)
        if len(df_train) < MIN_TRAIN_ROWS:
            logger.warning("  [{}] 训练集 {} 行，低于 {} 行下限，跳过",
                           group_key, len(df_train), MIN_TRAIN_ROWS)
            selection['skipped'][group_key] = f'train_rows={len(df_train)}'
            continue
        if df_test.empty:
            logger.warning("  [{}] 无测试样本，跳过", group_key)
            selection['skipped'][group_key] = 'empty_test'
            continue

        X_train, X_test = df_train[feature_cols], df_test[feature_cols]
        medians = compute_feature_medians(X_train)
        medians_by_group[group_key] = medians

        y_home_train = df_train['goals_home'].clip(0, 10)
        y_away_train = df_train['goals_away'].clip(0, 10)
        # 基线只能取训练段均值，避免测试集信息泄漏。
        baselines = {'home': float(y_home_train.mean()), 'away': float(y_away_train.mean())}

        logger.info("=== {} (train {} / test {}) ===",
                    group_key, len(df_train), len(df_test))

        rows, trained = [], {}
        for variant in LAMBDA_VARIANTS:
            try:
                model_home, model_away = train_lambda_variant(
                    variant, X_train, y_home_train, y_away_train, medians)
            except Exception as e:  # noqa: BLE001
                logger.error("  [{}] 路线 {} 训练失败: {}", group_key, variant, e)
                selection['failed'][f'{group_key}:{variant}'] = str(e)
                continue

            metrics = evaluate_lambda(model_home, model_away, X_test, df_test,
                                      baselines, medians)
            metrics.update({'group': group_key, 'variant': variant,
                            'n_train': len(df_train)})
            rows.append(metrics)
            trained[variant] = (model_home, model_away)
            logger.info("  {} -> rmse_mean={} (baseline {}) sign={} corr_h={} corr_a={}",
                        variant, metrics['rmse_mean'], metrics['baseline_rmse_mean'],
                        metrics['sign_agreement'], metrics['corr_home'], metrics['corr_away'])

        if not rows:
            selection['failed'].setdefault(group_key, '所有候选路线均训练失败')
            continue

        metric_rows.extend(rows)
        winner, info = select_lambda_variant(rows)
        logger.info("  [{}] 选用 {}: {}", group_key, winner, info['reason'])

        # 直接覆盖现役 λ 模型；分类器文件不在这个 dict 里，不会被改动。
        save_models(group_key, {
            'lambda_home': trained[winner][0],
            'lambda_away': trained[winner][1],
        })
        selection['groups'][group_key] = {
            'variant': winner,
            'reason': info['reason'],
            'n_train': len(df_train),
            'n_test': rows[0]['n_test'],
            'metrics': next(r for r in rows if r['variant'] == winner),
            'compared': info['compared'],
        }

    if medians_by_group:
        save_feature_medians(medians_by_group)

    _write_reports(metric_rows, selection)
    logger.info("λ 重训完成：{} 个分组更新，{} 跳过，{} 失败",
                len(selection['groups']), len(selection['skipped']),
                len(selection['failed']))
    return selection


def main() -> None:
    parser = argparse.ArgumentParser(description='只重训 λ_home / λ_away')
    parser.add_argument('--groups', default='',
                        help='逗号分隔的分组名，如 L_61,L_39；留空则全部')
    args = parser.parse_args()
    only = [g.strip() for g in args.groups.split(',') if g.strip()] or None
    run(only)


if __name__ == '__main__':
    main()
