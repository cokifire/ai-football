"""Auditable, bounded Bayesian helpers used by the pre-match predictor.

This module deliberately contains no network/LLM calls.  External information is
first converted to evidence by :mod:`prediction.predict`; only the small set of
supported evidence types below can move a probability.  Keeping the maths here
pure makes a prediction reproducible from its saved evidence ledger.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
from scipy.stats import poisson
from sqlalchemy import text

BAYES_VERSION = "bayes-v2"
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


def team_elo_prior(db, fixture: dict[str, Any]) -> tuple[dict[str, float] | None, dict[str, Any]]:
    """Read the current persisted ratings for the two teams in a fixture."""
    rows = db.execute(text("""
        SELECT id, name, elo, created_at, updated_at
        FROM team_elos
        WHERE id IN (:home_id, :away_id)
    """), {"home_id": fixture["home_id"], "away_id": fixture["away_id"]}).fetchall()
    values = {row.id: row for row in rows}
    home, away = values.get(fixture["home_id"]), values.get(fixture["away_id"])
    if home is None or away is None:
        missing = []
        if home is None:
            missing.append(str(fixture.get("home_name") or fixture["home_id"]))
        if away is None:
            missing.append(str(fixture.get("away_name") or fixture["away_id"]))
        return None, {"status": "UNAVAILABLE", "reason": "team_elos 缺少球队: " + ", ".join(missing)}
    return elo_probabilities(float(home.elo), float(away.elo)), {
        "status": "AVAILABLE", "source": "team_elos",
        "home_elo": float(home.elo), "away_elo": float(away.elo),
        "home_name": home.name, "away_name": away.name,
        "k": ELO_K, "home_advantage": HOME_ELO_ADVANTAGE,
        "home_updated_at": home.updated_at.isoformat() if home.updated_at else None,
        "away_updated_at": away.updated_at.isoformat() if away.updated_at else None,
    }


def calculate_elo_change(home_elo: float, away_elo: float, home_goals: int, away_goals: int) -> tuple[float, float]:
    """Return zero-sum Elo movements for a completed fixture."""
    expected_home = 1.0 / (1.0 + 10 ** (-(home_elo + HOME_ELO_ADVANTAGE - away_elo) / 400.0))
    actual_home = 1.0 if home_goals > away_goals else (0.5 if home_goals == away_goals else 0.0)
    margin = min(2.0, 1.0 + max(0, abs(int(home_goals) - int(away_goals)) - 1) * 0.15)
    home_delta = ELO_K * margin * (actual_home - expected_home)
    return home_delta, -home_delta


def apply_finished_fixture_elo(db, fixture_id: int) -> bool:
    """Apply one final score once, then record the immutable update ledger.

    Ratings begin at the values already seeded in ``team_elos``.  Fixtures dated
    before a team's seed timestamp are deliberately skipped, preventing old
    history from being applied a second time.
    """
    fixture = db.execute(text("""
        SELECT id, date, status_short, home_id, away_id, home_name, away_name,
               fulltime_home, fulltime_away
        FROM fixtures WHERE id=:fixture_id
    """), {"fixture_id": fixture_id}).fetchone()
    if not fixture or fixture.status_short not in {"FT", "AET", "PEN", "AWD", "WO"}:
        return False
    if fixture.fulltime_home is None or fixture.fulltime_away is None or fixture.home_id is None or fixture.away_id is None:
        return False
    if db.execute(text("SELECT fixture_id FROM team_elo_updates WHERE fixture_id=:fixture_id FOR UPDATE"),
                  {"fixture_id": fixture_id}).fetchone():
        return False

    ratings = db.execute(text("""
        SELECT id, name, elo, created_at FROM team_elos
        WHERE id IN (:home_id, :away_id)
        ORDER BY id FOR UPDATE
    """), {"home_id": fixture.home_id, "away_id": fixture.away_id}).fetchall()
    by_id = {row.id: row for row in ratings}
    home, away = by_id.get(fixture.home_id), by_id.get(fixture.away_id)
    if home is None or away is None:
        return False
    seeded_times = [value for value in (home.created_at, away.created_at) if value is not None]
    seeded_at = max(seeded_times) if seeded_times else None
    if fixture.date is None or (seeded_at is not None and fixture.date < seeded_at):
        return False

    home_before, away_before = float(home.elo), float(away.elo)
    home_delta, away_delta = calculate_elo_change(home_before, away_before, fixture.fulltime_home, fixture.fulltime_away)
    home_after, away_after = round(home_before + home_delta, 2), round(away_before + away_delta, 2)
    db.execute(text("UPDATE team_elos SET elo=:elo WHERE id=:team_id"), {"elo": home_after, "team_id": home.id})
    db.execute(text("UPDATE team_elos SET elo=:elo WHERE id=:team_id"), {"elo": away_after, "team_id": away.id})
    db.execute(text("""
        INSERT INTO team_elo_updates (
            fixture_id, fixture_date, home_team_id, away_team_id,
            home_elo_before, away_elo_before, home_elo_after, away_elo_after,
            home_delta, away_delta, created_at
        ) VALUES (
            :fixture_id, :fixture_date, :home_id, :away_id,
            :home_before, :away_before, :home_after, :away_after,
            :home_delta, :away_delta, NOW()
        )
    """), {
        "fixture_id": fixture.id, "fixture_date": fixture.date,
        "home_id": home.id, "away_id": away.id,
        "home_before": home_before, "away_before": away_before,
        "home_after": home_after, "away_after": away_after,
        "home_delta": round(home_delta, 4), "away_delta": round(away_delta, 4),
    })
    return True


def sync_finished_elos(db) -> int:
    """Apply all newly completed fixtures in kickoff order.

    The ledger primary key makes this safe to invoke from the daily scheduler,
    live fixture updates and result backfill.
    """
    rows = db.execute(text("""
        SELECT f.id
        FROM fixtures f
        LEFT JOIN team_elo_updates u ON u.fixture_id = f.id
        WHERE u.fixture_id IS NULL
          AND f.status_short IN ('FT', 'AET', 'PEN', 'AWD', 'WO')
          AND f.fulltime_home IS NOT NULL AND f.fulltime_away IS NOT NULL
          AND f.date >= (SELECT MIN(created_at) FROM team_elos WHERE created_at IS NOT NULL)
        ORDER BY f.date ASC, f.id ASC
    """)).fetchall()
    updated = 0
    for row in rows:
        if apply_finished_fixture_elo(db, int(row.id)):
            updated += 1
    return updated


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
