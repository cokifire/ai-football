"""拟合并保存模型概率校准参数（Platt scaling）。

用法:
    python tools/calibrate_probs.py            # 拟合全部市场并保存
    python tools/calibrate_probs.py --report   # 仅打印当前已保存的诊断

输出: calibration.json（含各市场 a/b 参数 + Brier 前后对比）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import SessionLocal
from app.services import calibration_service as cs


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="仅打印已保存的诊断")
    args = ap.parse_args()

    if args.report:
        diag = cs.get_diagnostics()
        if not diag:
            print("尚无校准参数，请先运行本脚本拟合。")
            return
        _print_report(diag)
        return

    db = SessionLocal()
    try:
        payload = cs.fit_and_save(db)
    finally:
        db.close()

    print(f"校准参数已保存至: {cs.CALIBRATION_PATH}\n")
    _print_report(payload["diagnostics"])


def _print_report(diag):
    print(f"{'市场':<10}{'n':>5}{'基准率':>8}{'Brier_raw':>11}{'Brier_cal':>11}{'改善':>8}  参数(a,b)")
    print("-" * 78)
    for key, d in diag.items():
        if not d.get("fitted"):
            print(f"{key:<10}{d.get('n',0):>5}{'':>8}{'':>11}{'':>11}{'':>8}  {d.get('reason','')}")
            continue
        raw = d["brier_raw"]
        cal = d["brier_cal"]
        imp = (raw - cal) / raw * 100 if raw else 0
        print(f"{key:<10}{d['n']:>5}{d['base_rate']:>8.3f}{raw:>11.4f}{cal:>11.4f}{imp:>7.1f}%  "
              f"a={d['a']}, b={d['b']}")


if __name__ == "__main__":
    main()
