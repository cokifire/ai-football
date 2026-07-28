"""模型概率校准服务（Platt scaling）。

问题：模型直接输出的概率（win_home / win_draw / win_away / llm_ou_pct /
llm_handicap_pct）往往未校准（如 win_home 均值 0.57，但实际主胜率仅 ~45%），
导致价值投注的 edge / Kelly 被高估、出现极端 Kelly（如 90%）。

做法：用历史（模型原始概率, 实际赛果）拟合逻辑回归，把原始概率映射到校准概率：
    p_cal = sigmoid( a * logit(p_raw) + b )
其中 logit(p) = log(p/(1-p))。这是 Platt scaling 在概率特征上的标准形式，
保证 p_cal ∈ (0,1) 且单调。

校准参数保存为 JSON，供 value_bet_service 实时调用；diagnostics 记录
校准前后 Brier 分数（越小越准）用于评估。
"""
import json
import math
import os
from sqlalchemy import text

# 校准参数存放路径（可用环境变量覆盖）
CALIBRATION_PATH = os.environ.get(
    "CALIBRATION_PATH",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "calibration.json"),
)


# ───────────────────────────── 基础数学 ─────────────────────────────
def _logit(p):
    p = min(max(p, 1e-6), 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def _sigmoid(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _brier(preds, targets):
    """Brier 分数 = mean((p - y)^2)。"""
    if not preds:
        return None
    return sum((p - y) ** 2 for p, y in zip(preds, targets)) / len(preds)


# ───────────────────────────── 拟合逻辑回归 ─────────────────────────────
def _nll(xs, ys, a, b, lam):
    """负对数似然（平滑目标 + L2 正则），用于线搜索。"""
    s = 0.0
    for x, y in zip(xs, ys):
        p = min(max(_sigmoid(a * x + b), 1e-12), 1.0 - 1e-12)
        s += -(y * math.log(p) + (1.0 - y) * math.log(1.0 - p))
    s += lam * (a * a + b * b)
    return s


def _fit_logistic(xs, ys):
    """单特征 (x=logit(p_raw)) 逻辑回归校准（Platt scaling），梯度下降 + 回溯线搜索。

    直接以实际二值赛果 y∈{0,1} 做最大似然拟合，配轻微 L2 正则避免过拟合导致极端 Kelly。
    返回 {"a": a, "b": b} 或 None（样本不足 / 缺乏正反例）。
    """
    n = len(xs)
    if n < 10:
        return None
    pos = sum(ys)
    if pos == 0 or pos == n:  # 需要正反例都存在
        return None

    a, b = 0.0, 0.0
    lam = 1e-3  # L2 正则
    lr = 0.5
    for _ in range(3000):
        g_a = g_b = 0.0
        for x, y in zip(xs, ys):
            p = _sigmoid(a * x + b)
            g_a += (p - y) * x
            g_b += (p - y)
        g_a += 2.0 * lam * a
        g_b += 2.0 * lam * b

        cur = _nll(xs, ys, a, b, lam)
        step = lr
        na, nb = a - step * g_a, b - step * g_b
        # 回溯线搜索，保证 NLL 下降
        for _ in range(40):
            if _nll(xs, ys, na, nb, lam) <= cur:
                break
            step *= 0.5
            na, nb = a - step * g_a, b - step * g_b
        a, b = na, nb
        if step * math.sqrt(g_a * g_a + g_b * g_b) < 1e-6:
            break
    return {"a": a, "b": b}


# ───────────────────────────── 历史样本采集 ─────────────────────────────
def collect_samples(db):
    """从 predictions 表采集各市场（原始概率, 实际赛果）样本。

    返回 {key: {"x": [...logit...], "raw": [...p_raw...], "y": [...0/1...]}}
    """
    rows = db.execute(text("""
        SELECT win_home, win_draw, win_away,
               llm_ou_type, llm_ou_line, llm_ou_pct, over25_prob,
               llm_handicap_num, llm_handicap_team, llm_handicap_pct,
               actual_home_goals, actual_away_goals
        FROM predictions
        WHERE actual_home_goals IS NOT NULL AND actual_away_goals IS NOT NULL
    """)).fetchall()

    def pf(v):
        if v is None:
            return None
        s = str(v).strip().replace("%", "").replace("％", "")
        try:
            f = float(s)
        except (ValueError, TypeError):
            return None
        return f / 100.0 if f > 1 else f

    samples = {
        "1x2_home": {"x": [], "raw": [], "y": []},
        "1x2_draw": {"x": [], "raw": [], "y": []},
        "1x2_away": {"x": [], "raw": [], "y": []},
        "ou": {"x": [], "raw": [], "y": []},
        "ou25": {"x": [], "raw": [], "y": []},
        "ah": {"x": [], "raw": [], "y": []},
    }

    for r in rows:
        m = r._mapping
        gh = m["actual_home_goals"]
        ga = m["actual_away_goals"]
        total = (gh or 0) + (ga or 0)

        # 1X2 三侧各自是否真实发生
        home_win = (gh or 0) > (ga or 0)
        draw = (gh or 0) == (ga or 0)
        away_win = (gh or 0) < (ga or 0)
        for key, occ in (("1x2_home", home_win), ("1x2_draw", draw), ("1x2_away", away_win)):
            raw = m["win_home"] if key == "1x2_home" else (
                m["win_draw"] if key == "1x2_draw" else m["win_away"])
            if raw is None:
                continue
            raw = float(raw)
            samples[key]["raw"].append(raw)
            samples[key]["x"].append(_logit(raw))
            samples[key]["y"].append(1 if occ else 0)

        # 大小球：模型所选方向是否真实命中
        ou_type = (m["llm_ou_type"] or "").strip()
        ou_line = m["llm_ou_line"]
        ou_pct = pf(m["llm_ou_pct"])
        if ou_type and ou_line is not None:
            try:
                line = float(ou_line)
                is_over = "大" in ou_type
                # 走水（total==line）排除，不参与二值校准
                if total != line and ou_pct is not None:
                    actual_over = total > line
                    won = actual_over if is_over else (not actual_over)
                    samples["ou"]["raw"].append(ou_pct)
                    samples["ou"]["x"].append(_logit(ou_pct))
                    samples["ou"]["y"].append(1 if won else 0)
            except (ValueError, TypeError):
                pass

        # 大小球 2.5 专用（fallback 路径）
        o25 = m["over25_prob"]
        if o25 is not None and ou_line is not None and float(ou_line) == 2.5:
            o25 = float(o25)
            if total != 2.5:  # 2.5 无走水
                samples["ou25"]["raw"].append(o25)
                samples["ou25"]["x"].append(_logit(o25))
                samples["ou25"]["y"].append(1 if total > 2.5 else 0)

        # 让球盘：模型所选方向是否赢盘
        hc = m["llm_handicap_num"]
        team = (m["llm_handicap_team"] or "").strip()
        hc_pct = pf(m["llm_handicap_pct"])
        if hc is not None and team and hc_pct is not None:
            try:
                hc_val = float(hc)
                adj_home = (gh or 0) + hc_val
                if adj_home > (ga or 0):
                    home_covers = True
                elif adj_home < (ga or 0):
                    home_covers = False
                else:
                    home_covers = None  # 走水，排除
                if home_covers is not None:
                    if team == "客队":
                        won = not home_covers
                    else:
                        won = home_covers
                    samples["ah"]["raw"].append(hc_pct)
                    samples["ah"]["x"].append(_logit(hc_pct))
                    samples["ah"]["y"].append(1 if won else 0)
            except (ValueError, TypeError):
                pass

    return samples


# ───────────────────────────── 拟合 + 保存 ─────────────────────────────
def fit_and_save(db, path=CALIBRATION_PATH):
    """拟合所有市场校准参数，保存 JSON，返回诊断报告。"""
    samples = collect_samples(db)
    params = {}
    diagnostics = {}
    for key, s in samples.items():
        if len(s["raw"]) < 10:
            diagnostics[key] = {"n": len(s["raw"]), "fitted": False,
                                "reason": "样本不足"}
            continue
        coef = _fit_logistic(s["x"], s["y"])
        if coef is None:
            diagnostics[key] = {"n": len(s["raw"]), "fitted": False,
                                "reason": "缺乏正负样本"}
            continue
        # 校准后概率
        cal = [_sigmoid(coef["a"] * x + coef["b"]) for x in s["x"]]
        params[key] = coef
        diagnostics[key] = {
            "n": len(s["raw"]),
            "fitted": True,
            "a": round(coef["a"], 4),
            "b": round(coef["b"], 4),
            "base_rate": round(sum(s["y"]) / len(s["y"]), 4),
            "brier_raw": round(_brier(s["raw"], s["y"]), 4),
            "brier_cal": round(_brier(cal, s["y"]), 4),
        }

    payload = {"params": params, "diagnostics": diagnostics}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload


# ───────────────────────────── 加载 + 应用 ─────────────────────────────
_cache = None


def load_calibration(path=CALIBRATION_PATH):
    global _cache
    if _cache is not None:
        return _cache
    if not os.path.exists(path):
        _cache = {"params": {}, "diagnostics": {}}
        return _cache
    try:
        with open(path, "r", encoding="utf-8") as f:
            _cache = json.load(f)
    except Exception:
        _cache = {"params": {}, "diagnostics": {}}
    return _cache


def apply_calibration(key, raw_p, path=CALIBRATION_PATH):
    """返回校准后的概率；无该 key 的校准参数时原样返回。"""
    if raw_p is None:
        return None
    cal = load_calibration(path)
    coef = cal.get("params", {}).get(key)
    if not coef:
        return raw_p
    return _sigmoid(coef["a"] * _logit(raw_p) + coef["b"])


def get_diagnostics(path=CALIBRATION_PATH):
    return load_calibration(path).get("diagnostics", {})
