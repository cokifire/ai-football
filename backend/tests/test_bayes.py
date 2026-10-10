import math

from prediction.bayes import (
    apply_updates, blend_prior, calculate_elo_change, elo_probabilities,
    market_divergence, normalize_3way, poisson_markets,
)


def test_probabilities_are_normalized():
    probs = normalize_3way({"home": 4, "draw": 2, "away": 4})
    assert math.isclose(sum(probs.values()), 1.0)
    assert probs["draw"] == 0.2


def test_elo_home_advantage_and_blend():
    elo = elo_probabilities(1500, 1500)
    assert elo["home"] > elo["away"]
    final = blend_prior(elo, {"home": 0.2, "draw": 0.2, "away": 0.6})
    assert final["away"] > final["home"]
    assert math.isclose(sum(final.values()), 1.0)


def test_finished_fixture_elo_change_is_zero_sum_and_rewards_home_win():
    home_delta, away_delta = calculate_elo_change(1500, 1500, 2, 0)
    assert home_delta > 0
    assert away_delta < 0
    assert math.isclose(home_delta + away_delta, 0.0)


def test_unavailable_evidence_never_changes_prior():
    prior = {"home": 0.45, "draw": 0.28, "away": 0.27}
    result, audit = apply_updates(prior, [{"type": "possession_trap", "status": "UNAVAILABLE", "side": "home", "delta": .5}])
    assert result == prior
    assert audit[0]["applied_delta"] == 0.0


def test_available_update_is_bounded_and_directional():
    prior = {"home": 0.4, "draw": 0.3, "away": 0.3}
    result, audit = apply_updates(prior, [{"status": "AVAILABLE", "side": "away", "delta": .9, "cap": .03}])
    assert audit[0]["applied_delta"] == .03
    assert result["away"] > prior["away"]
    assert math.isclose(sum(result.values()), 1.0)


def test_poisson_market_and_devig_market_check():
    markets = poisson_markets(1.4, 0.9)
    assert math.isclose(sum(markets["1x2"].values()), 1.0, abs_tol=1e-3)
    odds = [{"bookmaker": "test", "entries": [{"home_raw": 2.0, "draw_raw": 3.5, "away_raw": 4.0}]}]
    check = market_divergence(odds, markets["1x2"])
    assert check["status"] == "AVAILABLE"
    assert math.isclose(sum(check["market"].values()), 1.0)
