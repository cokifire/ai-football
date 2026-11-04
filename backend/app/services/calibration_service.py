"""模型概率校准服务（Platt scaling）。

问题：模型直接输出的概率（win_home / win_draw / win_away / llm_ou_pct /
llm_handicap_pct）往往未校准（如 win_home 均值 0.57，但实际主胜率仅 ~45%），
导致价值投注的 edge / Kelly 被高估、出现极端 Kelly（如 90%）。

做法：用历史（模型原始概率, 实际赛果）拟合映射，把原始概率映射到校准概率。支持两种
单调映射，拟合时用 5 折交叉验证比较 out-of-fold Brier，取更优者落盘：
    platt    : p_cal = sigmoid( a * logit(p_raw) + b )，logit(p) = log(p/(1-p))
    isotonic : 保序回归（PAVA）得到的分段线性保序曲线，能拟合非参数形状的偏差
两者都保证 p_cal ∈ (0,1) 且单调。

实测（2026-10-30，n≈900/市场）：LLM 声称概率整体过度自信 15~20pp，llm_win 的 ECE
由 0.153 降到 0.025，Brier 由 0.259 降到 0.235；三个 LLM 市场上 platt 的 out-of-fold
Brier 均优于 isotonic（isotonic 只在 ECE 上偶尔微弱领先），故当前全部落在 platt 上。
参数由 5 折 CV 自动选择，样本增长后重跑 tools/calibrate_probs.py 会自动改选方法。

校准参数保存为 JSON，供 value_bet_service 实时调用；diagnostics 记录
校准前后 Brier 分数（越小越准）用于评估。
"""
import json
import math
import os
import random
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


def _ece(preds, targets, bins=8):
    """Expected Calibration Error：按预测概率分桶，加权平均 |桶内均值 - 实际频率|。"""
    if not preds:
        return None
    n = len(preds)
    total = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, p in enumerate(preds) if (lo <= p < hi) or (b == bins - 1 and p == hi)]
        if not idx:
            continue
        mp = sum(preds[i] for i in idx) / len(idx)
        my = sum(targets[i] for i in idx) / len(idx)
        total += len(idx) / n * abs(mp - my)
    return total


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


# ───────────────────────────── 保序回归（isotonic） ─────────────────────────────
def _fit_isotonic(raws, ys):
    """保序回归（PAVA）。

    ``raws`` 为原始概率（不是 logit），返回按 x 递增的分段线性折点
    ``[[x, y], ...]``；样本不足或缺乏正反例时返回 None。
    """
    n = len(raws)
    if n < 10 or not (0 < sum(ys) < n):
        return None
    order = sorted(range(n), key=lambda i: raws[i])
    xs = [raws[i] for i in order]
    ys2 = [float(ys[i]) for i in order]

    # 每个块: [y_sum, count, x_sum]
    blocks = []
    for x, y in zip(xs, ys2):
        blocks.append([y, 1.0, x])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            b1, b2 = blocks.pop(), blocks.pop()
            blocks.append([b1[0] + b2[0], b1[1] + b2[1], b1[2] + b2[2]])
    knots = [[b[2] / b[1], b[0] / b[1]] for b in blocks]
    # PAVA 已保证 y 单调不减；x 取块内均值后仍应保持递增
    knots.sort(key=lambda k: k[0])
    return knots


def _isotonic_predict(knots, p):
    """折点间线性插值，两端截断（与 sklearn out_of_bounds='clip' 一致）。"""
    if not knots:
        return p
    if p <= knots[0][0]:
        out = knots[0][1]
    elif p >= knots[-1][0]:
        out = knots[-1][1]
    else:
        out = knots[-1][1]
        for i in range(1, len(knots)):
            x0, y0 = knots[i - 1]
            x1, y1 = knots[i]
            if p <= x1:
                out = y0 if x1 == x0 else y0 + (y1 - y0) * (p - x0) / (x1 - x0)
                break
    # 极端概率会让 Kelly 失真，统一夹到 [0.01, 0.99]
    return min(max(out, 0.01), 0.99)


def _platt_predict(coef, p):
    return _sigmoid(coef["a"] * _logit(p) + coef["b"])


# ───────────────────────────── 交叉验证选方法 ─────────────────────────────
def _cv_brier(raws, ys, folds=5, seed=0):
    """5 折交叉验证，返回 {方法: out-of-fold Brier}。

    用 out-of-fold（而非训练集拟合值）比较，避免用同一批样本既拟合又评分导致的
    乐观偏差——保序回归尤其容易在训练集上"完美贴合"而在样本外变差。
    """
    n = len(raws)
    idx = list(range(n))
    random.Random(seed).shuffle(idx)
    oof = {"platt": [None] * n, "isotonic": [None] * n}
    for f in range(folds):
        te = idx[f::folds]
        te_set = set(te)
        tr = [i for i in idx if i not in te_set]
        x_tr = [raws[i] for i in tr]
        y_tr = [ys[i] for i in tr]
        platt = _fit_logistic([_logit(v) for v in x_tr], y_tr)
        iso = _fit_isotonic(x_tr, y_tr)
        for i in te:
            oof["platt"][i] = _platt_predict(platt, raws[i]) if platt else raws[i]
            oof["isotonic"][i] = _isotonic_predict(iso, raws[i]) if iso else raws[i]
    return {m: _brier(v, ys) for m, v in oof.items()}


# ───────────────────────────── 历史样本采集 ─────────────────────────────
def collect_samples(db):
    """从 predictions 表采集各市场（原始概率, 实际赛果）样本。

    返回 {key: {"x": [...logit...], "raw": [...p_raw...], "y": [...0/1...]}}
    """
    rows = db.execute(text("""
        SELECT win_home, win_draw, win_away,
               llm_ou_type, llm_ou_line, llm_ou_pct, over25_prob,
               llm_handicap_num, llm_handicap_team, llm_handicap_pct,
               llm_win, llm_win_pct,
               f.fulltime_home AS actual_home_goals,
               f.fulltime_away AS actual_away_goals
        FROM predictions p
        JOIN fixtures f ON f.id = p.fixture_id
        WHERE f.fulltime_home IS NOT NULL AND f.fulltime_away IS NOT NULL
          AND f.status_short IN ('FT', 'AET', 'PEN')
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
        # LLM 自己对所选胜负的信心（llm_win_pct），与宿主 1x2_* 是两个独立信源
        "llm_win": {"x": [], "raw": [], "y": []},
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
        actual_win = "主胜" if home_win else ("平局" if draw else "客胜")
        for key, occ in (("1x2_home", home_win), ("1x2_draw", draw), ("1x2_away", away_win)):
            raw = m["win_home"] if key == "1x2_home" else (
                m["win_draw"] if key == "1x2_draw" else m["win_away"])
            if raw is None:
                continue
            raw = float(raw)
            samples[key]["raw"].append(raw)
            samples[key]["x"].append(_logit(raw))
            samples[key]["y"].append(1 if occ else 0)

        # LLM 胜负判断：它自己选的那一侧是否真实发生
        llm_win = (m["llm_win"] or "").strip()
        llm_win_pct = pf(m["llm_win_pct"])
        if llm_win and llm_win_pct is not None:
            samples["llm_win"]["raw"].append(llm_win_pct)
            samples["llm_win"]["x"].append(_logit(llm_win_pct))
            samples["llm_win"]["y"].append(1 if llm_win == actual_win else 0)

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
        if o25 is not None and ou_line is not None:
            # ou_line 可能是 "-"（无赔率时 LLM 的占位），不能直接 float()
            try:
                is_25 = float(ou_line) == 2.5
            except (ValueError, TypeError):
                is_25 = False
            if is_25:
                o25 = float(o25)
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
        knots = _fit_isotonic(s["raw"], s["y"])
        if coef is None and knots is None:
            diagnostics[key] = {"n": len(s["raw"]), "fitted": False,
                                "reason": "缺乏正负样本"}
            continue

        # 用 out-of-fold Brier 选方法，避免只看训练集拟合效果
        cv = _cv_brier(s["raw"], s["y"])
        method = min(cv, key=cv.get)
        if method == "isotonic" and knots is None:
            method = "platt"
        if method == "platt" and coef is None:
            method = "isotonic"

        if method == "isotonic":
            params[key] = {"method": "isotonic", "knots": [[round(x, 4), round(y, 4)]
                                                             for x, y in knots]}
            cal = [_isotonic_predict(knots, p) for p in s["raw"]]
        else:
            params[key] = {"method": "platt", "a": coef["a"], "b": coef["b"]}
            cal = [_platt_predict(coef, p) for p in s["raw"]]

        diagnostics[key] = {
            "n": len(s["raw"]),
            "fitted": True,
            "method": method,
            "a": round(coef["a"], 4) if coef else None,
            "b": round(coef["b"], 4) if coef else None,
            "knots": len(knots) if knots else None,
            "base_rate": round(sum(s["y"]) / len(s["y"]), 4),
            "brier_raw": round(_brier(s["raw"], s["y"]), 4),
            "brier_cal": round(_brier(cal, s["y"]), 4),
            "ece_raw": round(_ece(s["raw"], s["y"]), 4),
            "ece_cal": round(_ece(cal, s["y"]), 4),
            "brier_cv_platt": round(cv["platt"], 4),
            "brier_cv_isotonic": round(cv["isotonic"], 4),
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
    param = cal.get("params", {}).get(key)
    if not param:
        return raw_p
    # 兼容旧格式：只有 a/b、没有 method 字段的一律按 platt 处理
    if param.get("method") == "isotonic":
        return _isotonic_predict(param.get("knots") or [], raw_p)
    return _sigmoid(param["a"] * _logit(raw_p) + param["b"])


def get_diagnostics(path=CALIBRATION_PATH):
    return load_calibration(path).get("diagnostics", {})
