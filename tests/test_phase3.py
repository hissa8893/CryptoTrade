"""Phase 3: S2, S3 (vol sizing + rebalancing), partial-fill accounting, look-ahead for every
strategy and the combined portfolio, walk-forward integrity, Monte Carlo, regimes."""

import math
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from trader.backtest import make_engine, run_backtest
from trader.broker import CostModel, SimBroker
from trader.config import AppConfig, RiskConfig, S3Config, StrategiesConfig
from trader.data import clean_and_validate
from trader.lookahead import lookahead_proof
from trader.research import (classify_regimes, monte_carlo, param_grid, run_research, walk_forward,
                             wf_first_date, with_params)
from trader.strategies.base import SymbolData
from trader.strategies.s2_supertrend import S2Supertrend
from trader.strategies.s3_momentum import S3Momentum
from trader.synthetic import generate

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
COSTS = CostModel(0.001, 0.0005, 0.0015, ["BTC", "ETH"])


@pytest.fixture(scope="module")
def frames():
    out = {}
    for a in ("BTC", "ETH", "SOL", "XRP"):
        df, rep = clean_and_validate(generate(a, end=date(2026, 9, 26)), f"{a}/USD", now=NOW)
        assert rep.ok
        out[f"{a}/USD"] = df
    return out


def _sd(strat, df, sym="BTC/USD"):
    sd = SymbolData.from_frame(sym, df)
    sd.ind.update(strat.prepare(df))
    return sd


# ----------------------------------------------------------------------------- S2
def test_s2_enters_only_on_the_bullish_flip_above_the_sma(frames):
    s = S2Supertrend()
    sd = _sd(s, frames["BTC/USD"])
    entries = [i for i in range(len(sd.close)) if s.entry(sd, i)]
    assert entries
    for i in entries:
        assert sd.ind["s2_flip"][i] == 1 and sd.close[i] > sd.ind["s2_sma"][i]
    # a bullish bar that is NOT a flip never triggers an entry
    steady = [i for i in range(len(sd.close)) if sd.ind["s2_dir"][i] == 1 and sd.ind["s2_flip"][i] == 0]
    assert steady and not any(s.entry(sd, i) for i in steady)
    for i in range(len(sd.close)):
        assert (s.exit(sd, i, 0.0) is not None) == (sd.ind["s2_dir"][i] < 0)


# ----------------------------------------------------------------------------- S3
def test_s3_signal_and_vol_target_weight(frames):
    s = S3Momentum()
    sd = _sd(s, frames["BTC/USD"])
    for i in range(250, len(sd.close), 37):
        on = sd.ind["s3_rs"][i] > 0 and sd.ind["s3_rl"][i] > 0 and sd.close[i] > sd.ind["s3_sma"][i]
        assert (s.entry(sd, i) is not None) == bool(on)
        assert (s.exit(sd, i, 0) is not None) == (not on)
        vol = sd.ind["s3_vol"][i]
        assert s.target_weight(sd, i, 4) == pytest.approx(min(1.0, 0.40 / 4 / vol))
        assert s.target_weight(sd, i, 2) == pytest.approx(min(1.0, 0.40 / 2 / vol))


def test_s3_vol_sizing_never_exceeds_the_risk_engine_size(frames):
    """Even with a huge vol target, S3 positions stay within 1% risk and 25% of equity."""
    cfg = AppConfig(strategies=StrategiesConfig(s3=S3Config(target_vol=1.5)))
    eng, *_ = make_engine(cfg, frames, ["S3"], symbols=list(frames))
    worst = []

    def check(d, e):
        eq = e.journal.equity[-1]["equity"] if e.journal.equity and e.journal.equity[-1]["bar_date"] == d else None
        for p in e.broker.positions.values():
            if eq and p.bars_held <= 1:  # right after an entry or add fill
                worst.append(p.qty * e.last_close[p.symbol] / eq)

    eng.run(after_step=check)
    assert worst and max(worst) <= 0.25 * 1.10  # 25% cap (+ one day of price drift after the fill)


def test_s3_rebalances_only_when_target_moves_more_than_threshold(frames):
    res = run_backtest(AppConfig(), frames, ["S3"], symbols=list(frames), data_source="synthetic")
    rebal = [s for s in res.journal.signals if s["signal"] == "rebalance"]
    assert rebal, "expected rebalances on 11 years of 4 coins"
    for s in rebal:
        assert abs(s["indicators"]["change"]) > 0.20
    kinds = {o.reason for o in res.journal.orders}
    assert {"rebalance_up", "rebalance_down"} <= kinds


def test_rebalance_add_obeys_risk_gates():
    from trader.risk import RiskManager

    rm = RiskManager(RiskConfig(), 10_000)
    base = dict(date="2024-01-01", strategy="S3", symbol="BTC/USD", price=100, add_qty=5, stop=90, equity=10_000,
                max_affordable_qty=1e9, open_risk=0.0)
    assert rm.evaluate_add(**base, regime_ok=False).check == "regime"
    assert rm.evaluate_add(**base, regime_ok=True).result == "pass"
    assert rm.evaluate_add(**dict(base, open_risk=380.0), regime_ok=True).qty == pytest.approx(2.0)  # 20 of heat left / 10
    rm.state.breaker_active = True
    assert rm.evaluate_add(**base, regime_ok=True).check == "breaker"


def test_partial_sell_and_add_accounting():
    b = SimBroker(cash=10_000, costs=COSTS)
    b.queue(symbol="BTC/USD", strategy="S3", side="buy", qty=4.0, reason="entry", created_date="d0", stop_distance=50)
    b.fill_at_open("d1", {"BTC/USD": 1000.0})
    b.queue(symbol="BTC/USD", strategy="S3", side="sell", qty=1.0, reason="rebalance_down", created_date="d1")
    _, fills, trades = b.fill_at_open("d2", {"BTC/USD": 1100.0})
    assert not trades and b.positions[("S3", "BTC/USD")].qty == pytest.approx(3.0)
    b.queue(symbol="BTC/USD", strategy="S3", side="buy", qty=2.0, reason="rebalance_up", created_date="d2")
    b.fill_at_open("d3", {"BTC/USD": 1200.0})
    pos = b.positions[("S3", "BTC/USD")]
    e1, e2 = 1000 * 1.0005, 1200 * 1.0005
    assert pos.entry_px == pytest.approx((3 * e1 + 2 * e2) / 5)  # average in
    assert pos.stop == pytest.approx(e1 - 50)  # an add never changes the stop
    b.queue(symbol="BTC/USD", strategy="S3", side="sell", qty=5.0, reason="momentum_off", created_date="d3")
    _, _, trades = b.fill_at_open("d4", {"BTC/USD": 1150.0})
    t = trades[0]
    assert b.cash == pytest.approx(10_000 + t.pnl)  # flat: every dollar of P&L accounted for
    s1, x = 1100 * 0.9995, 1150 * 0.9995
    by_hand = ((s1 - e1) * 1 - s1 * 0.001) + (x * 5 - 3 * e1 - 2 * e2) - 5 * x * 0.001 - (4 * e1 + 2 * e2) * 0.001
    assert t.pnl == pytest.approx(by_hand)
    assert t.qty == pytest.approx(5.0) and t.r_multiple == pytest.approx(t.pnl / (4 * 50))


def test_rebalance_add_for_a_closed_position_is_cancelled():
    b = SimBroker(cash=10_000, costs=COSTS)
    b.queue(symbol="BTC/USD", strategy="S3", side="buy", qty=1.0, reason="rebalance_up", created_date="d0")
    orders, fills, _ = b.fill_at_open("d1", {"BTC/USD": 1000.0})
    assert orders[0].status == "cancelled" and not fills and b.cash == 10_000 and not b.positions


def test_new_position_without_a_valid_stop_is_refused():
    b = SimBroker(cash=10_000, costs=COSTS)
    b.queue(symbol="BTC/USD", strategy="S1", side="buy", qty=1.0, reason="entry", created_date="d0", stop_distance=None)
    orders, fills, _ = b.fill_at_open("d1", {"BTC/USD": 1000.0})
    assert orders[0].status == "cancelled" and b.cash == 10_000


@pytest.mark.parametrize("strats", [["S1"], ["S2"], ["S3"], ["S1", "S2", "S3"]])
def test_flat_end_accounting_identity_all_strategies(frames, strats):
    res = run_backtest(AppConfig(), frames, strats, symbols=list(frames), data_source="synthetic")
    for e in res.journal.equity:
        assert e["equity"] == pytest.approx(e["cash"] + e["positions_value"], rel=1e-12) and e["cash"] > -1e-6
        assert e["positions"] <= 4
    if not res.open_positions:
        assert res.metrics.end_equity == pytest.approx(10_000 + sum(t.pnl for t in res.journal.trades), rel=1e-9)


# ----------------------------------------------------------------------------- look-ahead
@pytest.mark.parametrize("strats,samples", [(["S2"], 200), (["S3"], 200), (["S1", "S2", "S3"], 120)])
def test_lookahead_proof_all_strategies_all_pairs(frames, strats, samples):
    rep = lookahead_proof(AppConfig(), frames, strats, symbols=list(frames), samples=samples)
    assert rep.dates_checked >= samples * 0.95 and rep.signals_compared > 0
    assert rep.passed, rep.mismatches[:1]


# ----------------------------------------------------------------------------- walk-forward
@pytest.fixture(scope="module")
def wf(frames):
    two = {k: frames[k] for k in ("BTC/USD", "ETH/USD")}
    return walk_forward(AppConfig(), two, "S2", ["BTC/USD", "ETH/USD"])


def test_walk_forward_never_scores_on_data_used_to_choose(wf):
    grid = param_grid(AppConfig(), "S2")
    prev_oos_end = None
    for w in wf.windows:
        assert w.is_end < w.oos_start  # parameters chosen strictly before the slice they are scored on
        assert date.fromisoformat(w.oos_start) - date.fromisoformat(w.is_end) == pd.Timedelta(days=1).to_pytimedelta()
        assert w.params in grid
        if prev_oos_end:
            assert (date.fromisoformat(w.oos_start) - date.fromisoformat(prev_oos_end)).days == 1  # contiguous, no overlap
        prev_oos_end = w.oos_end
    assert wf.oos_equity.iloc[0] == 10_000 and wf.oos_equity.index.is_monotonic_increasing
    assert wf.oos_equity.index.is_unique


def test_walk_forward_oos_matches_chained_slices(wf):
    total = 1.0
    for w in wf.windows:
        total *= 1 + w.oos_return
    assert wf.oos_metrics.end_equity == pytest.approx(10_000 * total, rel=1e-9)


def test_grid_is_plus_minus_20pct_and_bounded():
    g = param_grid(AppConfig(), "S1")
    assert sorted({x["entry_lookback"] for x in g}) == [16, 20, 24]
    assert sorted({x["chandelier_mult"] for x in g}) == [2.4, 3.0, 3.6]
    assert all(x["short_lookback"] < x["long_lookback"] for x in param_grid(AppConfig(), "S3"))
    with pytest.raises(Exception):
        with_params(AppConfig(), "S1", {"entry_lookback": 1})  # outside validated bounds


# ----------------------------------------------------------------------------- Monte Carlo + regimes
def test_monte_carlo_properties(frames):
    res = run_backtest(AppConfig(), frames, ["S1"], symbols=list(frames), data_source="synthetic")
    eq = {e["bar_date"]: e["equity"] for e in res.journal.equity}
    mc = monte_carlo(res.journal.trades, eq, 10_000, runs=1000, seed=1)
    mc2 = monte_carlo(res.journal.trades, eq, 10_000, runs=1000, seed=1)
    assert mc.dd_samples == mc2.dd_samples  # reproducible
    assert len(mc.dd_samples) == 1000
    assert mc.median_max_dd <= mc.p95_max_dd <= mc.worst_max_dd
    assert all(0 <= x < 1 for x in mc.dd_samples)


def test_regime_classifier_is_causal(frames):
    btc = frames["BTC/USD"]
    full = classify_regimes(btc)
    for t in range(250, len(btc), 97):
        assert classify_regimes(btc.iloc[: t + 1]).iloc[-1] == full.iloc[t]
    assert set(full.unique()) <= {"bull", "bear", "sideways", "unknown"}


def test_run_research_end_to_end_small(frames):
    two = {k: frames[k] for k in ("BTC/USD", "ETH/USD")}
    r = run_research(AppConfig(), two, ["S1", "S3"], ["BTC/USD", "ETH/USD"], data_source="synthetic",
                     sensitivity_grid=False)
    assert set(r.strategies) == {"S1", "S3"} and r.combined is not None
    assert r.zero_cost_check["ok"]
    from trader.research_report import render_research_report

    html = render_research_report(r)
    assert "OUT-OF-SAMPLE" in html and "SYNTHETIC DATA" in html and "Combined" in html


def test_regression_s3_vol_budget_ignores_coins_not_listed_yet(frames):
    """Regression (caught by the look-ahead proof): S3 divided its vol budget by ALL configured
    coins, so in 2017 it already 'knew' SOL (listed 2020) would exist."""
    rep = lookahead_proof(AppConfig(), frames, ["S3"], symbols=list(frames), samples=1)
    t = "2017-01-30"
    from trader.backtest import make_engine

    full, fr, start, _ = make_engine(AppConfig(), frames, ["S3"], symbols=list(frames))
    full.run(until=t)
    trunc, *_ = make_engine(AppConfig(), {s: df.loc[:t] for s, df in fr.items()}, ["S3"], symbols=list(frames), start=start)
    trunc.run()
    assert full.journal.for_date(t) == trunc.journal.for_date(t)
    assert rep.passed
