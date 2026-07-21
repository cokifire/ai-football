"""把 Windows 上用 pickle 保存的 XGBoost 模型重新序列化为跨平台 JSON 格式。

背景：XGBoost 的原生 Booster 字节是按 OS/编译器绑定的，Windows 上 pickle 的
.pkl 在 Linux 上 pickle.load 时会 segfault，导致后端进程崩溃。XGBoost 的原生
JSON/UBJSON 格式跨平台，可安全拷贝到 Linux 加载。

用法（在 Windows 的原项目里运行，因为 .pkl 只有在同 OS 才 load 得起来）：
    cd backend
    python ../scripts/convert_models.py
生成 backend/prediction/models/*.json，拷贝到 Ubuntu 的同目录即可。
"""
import os
import glob
import pickle

import xgboost as xgb  # noqa: F401  (确保 pickle 反序列化时能找到类定义)

MODELS_DIR = os.path.join(os.path.dirname(__file__), '..', 'prediction', 'models')


def main():
    pkls = sorted(glob.glob(os.path.join(MODELS_DIR, '*.pkl')))
    if not pkls:
        print('未找到任何 .pkl 模型文件，无需转换。')
        return

    ok = 0
    fail = 0
    for pkl_path in pkls:
        json_path = os.path.splitext(pkl_path)[0] + '.json'
        try:
            with open(pkl_path, 'rb') as f:
                model = pickle.load(f)
            # XGBClassifier / XGBRegressor 均提供原生 save_model
            model.save_model(json_path)
            print(f'OK   {os.path.basename(pkl_path)} -> {os.path.basename(json_path)}')
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f'FAIL {os.path.basename(pkl_path)}: {e}')
            fail += 1

    print(f'\n完成：成功 {ok} 个，失败 {fail} 个。')
    if fail == 0:
        print('可将生成的 .json 拷贝到 Linux 后端的 prediction/models/ 目录。')


if __name__ == '__main__':
    main()
