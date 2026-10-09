"""Auditable, bounded Bayesian helpers used by the pre-match predictor.

This module deliberately contains no network/LLM calls.  External information is
first converted to evidence by :mod:`prediction.predict`; only the small set of
supported evidence types below can move a probability.  Keeping the maths here
pure makes a prediction reproducible from its saved evidence ledger.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from typing import Any

import numpy as np
from scipy.stats import poisson
from sqlalchemy import text

BAYES_VERSION = "bayes-v1"
ELO_BASE = 1500.0
ELO_K = 20.0
HOME_ELO_ADVANTAGE = 55.0


def _clip(p: float) -> float:
    return min(max(float(p), 1e-6), 1.0 - 1e-6)


def normalize_3way(values: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, float(values.get(k, 0.0))) for k in ("home", "draw", "away"))
    if total <= 0:
        return {"home": 1 / 3, "draw": 1 / 3, "away": 1 / 3}
    return {k: max(0.0, float(values.get(k, 0.0))) / total for k in ("home", "draw", "away")}


def elo_probabilities(home_elo: float, away_elo: float) -> dict[str, float]:
    """Turn an Elo gap into a conservative three-way distribution.

    The draw prior peaks for evenly matched teams and is intentionally bounded:
    sparse local history must not manufacture extreme certainty.
    """
    gap = home_elo + HOME_ELO_ADVANTAGE - away_elo
    decisive_home = 1.0 / (1.0 + 10 ** (-gap / 400.0))
    draw = 0.18 + 0.10 * math.exp(-abs(gap) / 220.0)
    remaining = 1.0 - draw
    return normalize_3way({"home": remaining * decisive_home, "draw": draw,
                           "away": remaining * (1.0 - decisive_home)})


def local_elo_prior(db, fixture: dict[str, Any]) -> tuple[dict[str, float], dict[str, Any]]:
    """Replays only completed matches before kickoff to produce an Elo prior."""
    rows = db.execute(text("""
        SELECT home_id, away_id, goals_home, goals_away, date
        FROM fixtures
        WHERE league_id=:league_id AND date < :kickoff
          AND status_short IN ('FT', 'AET', 'PEN')
          AND goals_home IS NOT NULL AND goals_away IS NOT NULL
        ORDER BY date ASC, id ASC
    """), {"league_id": fixture["league_id"], "kickoff": fixture["date"]}).fetchall()
    ratings: defaultdict[int, float] = defaultdict(lambda: ELO_BASE)
    for row in rows:
        h, a, gh, ga = row[0], row[1], row[2], row[3]
        if h is None or a is None:
            continue
        expected_home = 1.0 / (1.0 + 10 ** (-(ratings[h] + HOME_ELO_ADVANTAGE - ratings[a]) / 400.0))
        actual_home = 1.0 if gh > ga else (0.5 if gh == ga else 0.0)
        margin = min(2.0, 1.0 + max(0, abs(int(gh) - int(ga)) - 1) * 0.15)
        delta = ELO_K * margin * (actual_home - expected_home)
        ratings[h] += delta
        ratings[a] -= delta
    home = ratings[fixture["home_id"]]
    away = ratings[fixture["away_id"]]
    return elo_probabilities(home, away), {"home_elo": round(home, 2), "away_elo": round(away, 2),
                                            "history_matches": len(rows), "k": ELO_K,
                                            "home_advantage": HOME_ELO_ADVANTAGE}


def blend_prior(elo: dict[str, float], xgb: dict[str, float], xgb_weight: float = 0.65) -> dict[str, float]:
    """Geometric opinion pool; preserves all three outcomes and avoids a hard switch."""
    xgb_weight = min(max(xgb_weight, 0.0), 1.0)
    raw = {k: _clip(elo[k]) ** (1 - xgb_weight) * _clip(xgb[k]) ** xgb_weight
           for k in ("home", "draw", "away")}
    return normalize_3way(raw)


def apply_updates(prior: dict[str, float], updates: list[dict[str, Any]]) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """Apply bounded log-odds evidence updates and return the applied audit rows.

    An update has ``side`` (home/away/draw), a signed ``delta`` in probability
    points and an optional absolute ``cap``.  Unsupported/unavailable rows are
    saved but cannot move probabilities.
    """
    logits = {k: math.log(_clip(prior[k])) for k in prior}
    applied = []
    changed = False
    for item in updates:
        record = dict(item)
        if record.get("status") != "AVAILABLE" or record.get("side") not in logits:
            record["applied_delta"] = 0.0
            applied.append(record)
            continue
        cap = abs(float(record.get("cap", 0.05)))
        delta = max(-cap, min(cap, float(record.get("delta", 0.0))))
        # A probability-point shift is represented as a small log-weight;
        # normalisation below distributes the counter-move to other outcomes.
        logits[record["side"]] += delta
        changed = changed or bool(delta)
        record["applied_delta"] = delta
        applied.append(record)
    if not changed:
        # Preserve the caller's already-normalised prior byte-for-byte when
        # evidence was unavailable or informational only.
        return dict(prior), applied
    max_logit = max(logits.values())
    return normalize_3way({k: math.exp(v - max_logit) for k, v in logits.items()}), applied


def poisson_markets(lambda_home: float, lambda_away: float, max_goals: int = 10) -> dict[str, Any]:
    lh, la = max(0.05, min(float(lambda_home), 8.0)), max(0.05, min(float(lambda_away), 8.0))
    home = draw = away = over25 = 0.0
    scores = []
    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            p = float(poisson.pmf(h, lh) * poisson.pmf(a, la))
            scores.append({"score": f"{h}-{a}", "prob": round(p, 4)})
            home += p if h > a else 0.0
            draw += p if h == a else 0.0
            away += p if h < a else 0.0
            over25 += p if h + a >= 3 else 0.0
    scores.sort(key=lambda row: row["prob"], reverse=True)
    return {"1x2": normalize_3way({"home": home, "draw": draw, "away": away}),
            "over25": over25, "top3": scores[:3]}


def market_divergence(odds_data: list[dict] | None, model: dict[str, float]) -> dict[str, Any]:
    """Return de-vig 1X2 consensus for a risk warning; never changes ``model``."""
    points = {"home": [], "draw": [], "away": []}
    for bookmaker in odds_data or []:
        for entry in bookmaker.get("entries") or []:
            vals = (entry.get("home_raw"), entry.get("draw_raw"), entry.get("away_raw"))
            if not all(vals):
                continue
            inv = [1 / float(v) for v in vals]
            total = sum(inv)
            for side, value in zip(points, inv):
                points[side].append(value / total)
    if not all(points.values()):
        return {"status": "UNAVAILABLE", "reason": "无完整去水 1X2 赔率"}
    consensus = {k: float(np.median(v)) for k, v in points.items()}
    gap = {k: round(model[k] - consensus[k], 4) for k in consensus}
    return {"status": "AVAILABLE", "market": consensus, "gap": gap,
            "max_abs_gap": round(max(abs(v) for v in gap.values()), 4),
            "risk": "市场分歧较大" if max(abs(v) for v in gap.values()) >= 0.12 else None}
