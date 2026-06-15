"""训练入口：遍历所有分组，训练并保存模型，输出评估报告"""

import os
import sys
import csv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from loguru import logger
from prediction.training.data import load_features, get_feature_cols, get_groups, train_test_split_temporal
from prediction.training.model import train_group, save_models
from prediction.training.evaluate import evaluate_group

METRICS_PATH = os.path.join(os.path.dirname(__file__), 'metrics.csv')


def run():
    logger.info("加载特征数据...")
    df = load_features()
    logger.info(f"总样本: {len(df):,}")

    feature_cols = get_feature_cols(df)
    logger.info(f"特征数: {len(feature_cols)}")

    # 保存特征列名（推理时使用）
    import pickle
    models_dir = os.path.join(os.path.dirname(__file__), 'models')
    os.makedirs(models_dir, exist_ok=True)
    with open(os.path.join(models_dir, 'feature_cols.pkl'), 'wb') as f:
        pickle.dump(feature_cols, f)

    groups = get_groups(df)
    logger.info(f"分组数: {len(groups)}")

    metrics_rows = []

    for group_key, group_df in groups.items():
        logger.info(f"\n=== {group_key} ({len(group_df):,} 样本) ===")
        df_train, df_test = train_test_split_temporal(group_df)

        if len(df_train) < 100:
            logger.warning(f"  训练集太小，跳过")
            continue

        X_train = df_train[feature_cols]
        X_test = df_test[feature_cols]

        try:
            models = train_group(group_key, df_train, feature_cols)
            save_models(group_key, models)

            results = evaluate_group(models, X_test, df_test)
            win_r = results['win']
            over_r = results['over25']
            range_r = results['goals_range']

            logger.info(
                f"  胜平负 acc={win_r['accuracy_3way']:.3f} rps={win_r['rps']:.3f} | "
                f"大小球 acc={over_r['accuracy']:.3f} auc={over_r['auc']:.3f} | "
                f"进球区间 acc={range_r['accuracy']:.3f}"
            )

            metrics_rows.append({
                'group': group_key,
                'n_train': len(df_train),
                'n_test': len(df_test),
                'win_accuracy': round(win_r['accuracy_3way'], 4),
                'win_rps': round(win_r['rps'], 4),
                'win_logloss': round(win_r['logloss'], 4),
                'over25_accuracy': round(over_r['accuracy'], 4),
                'over25_auc': round(over_r['auc'], 4),
                'goals_range_accuracy': round(range_r['accuracy'], 4),
            })

        except Exception as e:
            logger.error(f"  训练失败: {e}")

    # 写评估报告
    if metrics_rows:
        with open(METRICS_PATH, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=metrics_rows[0].keys())
            writer.writeheader()
            writer.writerows(metrics_rows)
        logger.info(f"\n评估报告已写入: {METRICS_PATH}")

    logger.info("训练完成")


if __name__ == '__main__':
    run()
