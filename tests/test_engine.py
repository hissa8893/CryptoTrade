"""Engine-level proofs: no look-ahead, hand-verified trades, cost sanity, drawdown, resume, invariants."""

import json
import math
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from trader.backtest import make_engine, run_backtest
from trader.broker import CostModel
from trader.config import AppConfig, RiskConfig
from trader.data import clean_and_validate
from trader.lookahead import lookahead_proof
from trader.metrics import drawdown_stats
from trader.strategies import s1_donchian
from trader.synthetic import generate

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


def _frame(asset):
    df, rep = clean_and_validate(generate(asset, end=date(2026, 9, 26)), f"{asset}/USD", now=NOW)
    assert rep.ok
    return df


@pytest.fixture(scope="module")
def frames():
    return {f"{a}/USD": _frame(a) for a in ("BTC", "ETH", "SOL", "XRP")}


@pytest.fixture(scope="module")
def btc_result(frames):
    return run_backtest(AppConfig(), frames, ["S1"], symbols=["BTC/USD"], data_source="synthetic")


# ----------------------------------------------------------------------------- look-ahead
def test_lookahead_proof_passes_on_200plus_days(frames):
    rep = lookahead_proof(AppConfig(), {"BTC/USD": frames["BTC/USD"]}, ["S1"], symbols=["BTC/USD"], samples=220)
    assert rep.dates_checked >= 200
    assert rep.dates_with_signals >= 50  # the sample covers real decisions, not just quiet days
    assert rep.passed, rep.mismatches[:1]


def test_lookahead_proof_catches_a_planted_peek(frames, monkeypatch):
    """The proof must FAIL if a strategy peeks one bar ahead (otherwise the proof proves nothing)."""
    real_entry = s1_donchian.S1Donchian.entry

    def peeking_entry(self, sd, i):
        if i + 1 < len(sd.close) and sd.close[i + 1] > sd.close[i] * 1.02:  # uses tomorrow's close
            return real_entry(self, sd, i) or s1_donchian.EntrySignal(sd.symbol, self.name, 1.0, {})
        return real_entry(self, sd, i)

    monkeypatch.setattr(s1_donchian.S1Donchian, "entry", peeking_entry)
    rep = lookahead_proof(AppConfig(), {"BTC/USD": frames["BTC/USD"]}, ["S1"], symbols=["BTC/USD"], samples=60)
    assert not rep.passed and rep.mismatches


def test_every_fill_happens_after_its_decision(btc_result):
    for o in btc_result.journal.orders:
        if o.status == "filled":
            assert o.fill_date > o.created_date  # never on the decision bar
    sig_dates = {s["ref"]: s["bar_date"] for s in btc_result.journal.signals}
    for t in btc_result.journal.trades:
        assert t.entry_date > sig_dates[t.entry_signal_ref]


# ----------------------------------------------------------------------------- hand verification
def _wilder_atr(h, l, c, n):
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    out = [math.nan] * len(c)
    out[n - 1] = sum(tr[:n]) / n
    for i in range(n, len(c)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def test_three_trades_verified_by_hand(btc_result, frames):
    """Independently recompute entry, size, initial stop, trailing stop, exit, fees, P&L and R
    for three trades using plain Python on the raw candles."""
    df = frames["BTC/USD"]
    d = [x.date().isoformat() for x in df.index]
    o, h, l, c = (df[k].tolist() for k in ("open", "high", "low", "close"))
    atr14, atr20 = _wilder_atr(h, l, c, 14), _wilder_atr(h, l, c, 20)
    fee, slip = 0.001, 0.0005
    trades = btc_result.journal.trades
    picks = {
        "first trade": trades[0],
        "trailing-stop exit": next(t for t in trades if t.exit_reason == "stop" and t.pnl > 0),
        "donchian exit": next(t for t in trades if t.exit_reason == "donchian_exit"),
    }
    equity = 10_000.0
    closed_before = {t.trade_no: sum(x.pnl for x in trades if x.trade_no < t.trade_no) for t in trades}
    for name, t in picks.items():
        e = d.index(t.entry_date)
        sig = e - 1  # decided at the previous close
        eq = equity + closed_before[t.trade_no]  # single strategy/symbol: flat between trades -> equity = cash
        # entry: next open + slippage
        entry_px = o[e] * (1 + slip)
        assert t.entry_px == pytest.approx(entry_px, rel=1e-12), name
        # size: min(1% risk / (2.5 ATR14), 25% of equity, cash)
        dist = 2.5 * atr14[sig]
        qty = min(eq * 0.01 / dist, eq * 0.25 / c[sig], eq / (c[sig] * (1 + slip) * (1 + fee)))
        assert t.qty == pytest.approx(qty, rel=1e-12), name
        # initial stop from the FILL price
        assert t.initial_stop == pytest.approx(entry_px - dist, rel=1e-12), name
        # walk the trade: chandelier trailing stop = max(stop, highest high since entry - 3 x ATR20)
        x = d.index(t.exit_date)
        stop, hh = entry_px - dist, o[e]
        for k in range(e, x):
            if k > e:
                assert l[k] > stop and o[k] > stop, f"{name}: should have stopped out on {d[k]}"
            hh = max(hh, h[k])
            stop = max(stop, hh - 3 * atr20[k])
        if t.exit_reason == "stop":
            raw = o[x] if o[x] <= stop else stop
            assert l[x] <= stop
        else:  # close-based exit decided at x-1, filled at the open of x
            assert t.exit_reason == "donchian_exit"
            assert c[x - 1] < min(l[x - 11 : x - 1])  # close below the prior 10-day low
            raw = o[x]
        exit_px = raw * (1 - slip)
        assert t.exit_px == pytest.approx(exit_px, rel=1e-12), name
        fees = qty * entry_px * fee + qty * exit_px * fee
        pnl = (exit_px - entry_px) * qty - fees
        assert t.fees == pytest.approx(fees, rel=1e-12), name
        assert t.pnl == pytest.approx(pnl, rel=1e-12), name
        assert t.r_multiple == pytest.approx(pnl / (qty * dist), rel=1e-12), name


# ----------------------------------------------------------------------------- sanity checks
def test_zero_cost_run_earns_more_than_with_cost_run(frames, btc_result):
    zero = run_backtest(AppConfig(), frames, ["S1"], symbols=["BTC/USD"], data_source="synthetic", costs=CostModel.zero())
    assert zero.metrics.end_equity > btc_result.metrics.end_equity
    assert zero.metrics.fees == 0 and btc_result.metrics.fees > 0


def test_max_drawdown_matches_equity_curve(btc_result):
    eq = np.array([e["equity"] for e in btc_result.journal.equity])
    peak, worst = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        worst = max(worst, 1 - v / peak)
    assert btc_result.metrics.max_drawdown == pytest.approx(worst, rel=1e-12)
    # and the engine's own per-day drawdown column agrees with the curve
    assert max(e["drawdown_pct"] for e in btc_result.journal.equity) == pytest.approx(worst, rel=1e-9)


def test_equity_identity_and_no_negative_cash(btc_result):
    for e in btc_result.journal.equity:
        assert e["equity"] == pytest.approx(e["cash"] + e["positions_value"], rel=1e-12)
        assert e["cash"] >= -1e-9


def test_entries_only_in_bull_regime(frames, btc_result):
    btc = frames["BTC/USD"]
    sma = btc["close"].rolling(200).mean()
    passed = [x for x in btc_result.journal.decisions if x["final_action"] == "buy"]
    assert passed
    sig = {s["ref"]: s for s in btc_result.journal.signals}
    for dcs in passed:
        day = pd.Timestamp(sig[dcs["signal_ref"]]["bar_date"], tz="UTC")
        assert btc.loc[day, "close"] > sma.loc[day]
    blocked = [x for x in btc_result.journal.decisions if x["check"] == "regime"]
    assert blocked  # the filter actually did something on this data


def test_stops_never_move_down(frames):
    eng, *_ = make_engine(AppConfig(), frames, ["S1"], symbols=["BTC/USD", "ETH/USD"])
    last = {}

    def check(d, e):
        for key, p in e.broker.positions.items():
            k = (key, p.trade_no)
            if k in last:
                assert p.stop >= last[k] - 1e-12, f"stop moved down for {k} on {d}"
            assert p.stop >= p.initial_stop - 1e-12
            last[k] = p.stop

    eng.run(after_step=check)
    assert last


def test_resume_from_saved_state_matches_uninterrupted_run(frames):
    cfg = AppConfig()
    full, *_ = make_engine(cfg, frames, ["S1"], symbols=list(frames))
    full.run()
    cut = full.dates[len(full.dates) // 2]
    a, *_ = make_engine(cfg, frames, ["S1"], symbols=list(frames))
    a.run(until=cut)
    saved = json.loads(json.dumps(a.state_dict()))  # what a crash-recovery restart would read back
    b, *_ = make_engine(cfg, frames, ["S1"], symbols=list(frames))
    b.load_state(saved)
    b.run()
    tail = [e for e in full.journal.equity if e["bar_date"] > cut]
    assert b.journal.equity == tail
    assert [t.trade_no for t in b.journal.trades] == [t.trade_no for t in full.journal.trades if t.exit_date > cut]
    assert b.state_dict() == full.state_dict()


def test_max_positions_and_ranking_across_symbols(frames):
    cfg = AppConfig(risk=RiskConfig(max_positions=1))
    res = run_backtest(cfg, frames, ["S1"], symbols=list(frames), data_source="synthetic")
    by_day = {}
    for e in res.journal.equity:
        assert e["positions"] <= 1
    sig = {s["ref"]: s for s in res.journal.signals}
    for dcs in res.journal.decisions:
        s = sig[dcs["signal_ref"]]
        if s["signal"] == "enter":
            by_day.setdefault(s["bar_date"], []).append((s["strength"], dcs))
    contested = [v for v in by_day.values() if len(v) > 1 and any(x[1]["final_action"] == "buy" for x in v)]
    assert contested, "expected days with more signals than slots"
    for v in contested:
        winner = max(v, key=lambda x: x[0])
        assert winner[1]["final_action"] == "buy"  # strongest signal got the slot
        assert all(x[1]["check"] == "max_positions" for x in v if x is not winner)


def test_combined_portfolio_respects_heat_at_decision(frames):
    res = run_backtest(AppConfig(), frames, ["S1"], symbols=list(frames), data_source="synthetic")
    for dcs in res.journal.decisions:
        if dcs["final_action"] == "buy":
            assert dcs["final_qty"] > 0
    assert res.metrics.trades > 0
