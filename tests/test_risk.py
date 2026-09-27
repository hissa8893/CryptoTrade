"""RiskManager: sizing, stop precedence, heat, max positions, daily loss cap, breaker, cooldown."""

import pytest

from trader.config import RiskConfig
from trader.risk import RiskManager

CFG = RiskConfig()


def ev(rm, **kw):
    base = dict(date="2024-06-01", strategy="S1", symbol="BTC/USD", price=100.0, atr=2.0, equity=10_000.0,
                max_affordable_qty=1e9, open_risk=0.0, open_slots=4, regime_ok=True)
    base.update(kw)
    return rm.evaluate_entry(**base)


def test_risk_based_size_is_one_percent_of_equity():
    d = ev(RiskManager(CFG, 10_000))
    # stop distance = 2.5 x ATR 2 = 5 ; qty = 10,000 x 1% / 5 = 20 ; notional 2,000 (< 25% cap)
    assert d.result == "pass" and d.stop_distance == pytest.approx(5.0) and d.qty == pytest.approx(20.0)
    assert d.qty * d.stop_distance == pytest.approx(100.0)


def test_size_capped_at_25pct_of_equity():
    d = ev(RiskManager(CFG, 10_000), atr=0.2)  # risk qty 200 -> notional 20,000 > 25% (2,500)
    assert d.result == "shrunk" and d.qty == pytest.approx(25.0) and d.sizing["binding"] == "max_position"


def test_size_capped_by_cash():
    d = ev(RiskManager(CFG, 10_000), max_affordable_qty=3.0)
    assert d.result == "shrunk" and d.qty == 3.0 and d.sizing["binding"] == "cash"


def test_strategy_cap_can_only_shrink():
    rm = RiskManager(CFG, 10_000)
    assert ev(rm, strategy_cap_qty=7.0).qty == 7.0
    assert ev(rm, strategy_cap_qty=1e6).qty == pytest.approx(20.0)  # never above the risk size


def test_heat_shrinks_then_blocks():
    rm = RiskManager(CFG, 10_000)
    d = ev(rm, open_risk=350.0)  # 4% of 10k = 400 allowed; 50 left -> qty 10
    assert d.result == "shrunk" and d.qty == pytest.approx(10.0) and d.sizing.get("heat_limited")
    d = ev(rm, open_risk=400.0)
    assert d.result == "blocked" and d.check == "heat"


def test_max_positions():
    d = ev(RiskManager(CFG, 10_000), open_slots=0)
    assert d.result == "blocked" and d.check == "max_positions"


def test_regime_blocks_and_unknown_regime_blocks():
    rm = RiskManager(CFG, 10_000)
    assert ev(rm, regime_ok=False).check == "regime"
    assert ev(rm, regime_ok=None).check == "regime"
    off = RiskManager(RiskConfig(regime_filter_enabled=False), 10_000)
    assert ev(off, regime_ok=False).result == "pass"


def test_check_order_first_failure_is_reported():
    rm = RiskManager(CFG, 10_000)
    rm.state.breaker_active = True
    d = ev(rm, regime_ok=False, open_slots=0)
    assert d.check == "regime"  # regime is checked before max positions and the breaker


def test_stops_only_move_up():
    assert RiskManager.ratchet_stop(90.0, 95.0) == 95.0
    assert RiskManager.ratchet_stop(95.0, 80.0) == 95.0  # a lower trailing stop never loosens it
    assert RiskManager.ratchet_stop(95.0, None) == 95.0
    assert RiskManager.ratchet_stop(95.0, float("nan")) == 95.0


def test_daily_loss_cap_blocks_next_days_entries_only():
    rm = RiskManager(CFG, 10_000)
    rm.on_close("2024-06-01", 10_000)
    events = rm.on_close("2024-06-02", 9_690)  # -3.1%
    assert [e.type for e in events] == ["daily_loss_cap"] and events[0].severity == "urgent"
    assert ev(rm, date="2024-06-02").check == "daily_loss"  # decided today -> would fill tomorrow
    rm.on_close("2024-06-03", 9_700)
    assert ev(rm, date="2024-06-03").result == "pass"
    assert rm.on_close("2024-06-04", 9_700 * 0.971) == []  # -2.9%: under the cap


def test_circuit_breaker_trigger_and_release():
    rm = RiskManager(CFG, 10_000)
    rm.on_close("d0", 12_000)  # new peak
    # walk down < 3%/day so only the drawdown rule is exercised
    path = [11_700, 11_400, 11_100, 10_800, 10_500, 10_300]  # 10,300 = 14.17% below peak
    for i, eq in enumerate(path):
        assert rm.on_close(f"a{i}", eq) == []
    assert not rm.state.breaker_active
    ev_on = rm.on_close("d3", 10_150)  # 15.42% below peak
    assert [e.type for e in ev_on] == ["circuit_breaker_on"] and rm.state.breaker_active
    assert ev(rm, date="d4").check == "breaker"
    assert rm.on_close("d4", 10_400) == [] and rm.on_close("d5", 10_700) == []  # 10.8% below: still on
    assert rm.state.breaker_active
    ev_off = rm.on_close("d6", 10_850)  # 9.58% below peak -> released
    assert [e.type for e in ev_off] == ["circuit_breaker_off"] and not rm.state.breaker_active
    assert ev(rm, date="d7").result == "pass"


def test_losing_streak_cooldown():
    rm = RiskManager(CFG, 10_000)
    for i, day in enumerate(["2024-06-01", "2024-06-02", "2024-06-03"]):
        assert rm.on_trade_closed("S1", -10.0, day) == []
    events = rm.on_trade_closed("S1", -10.0, "2024-06-04")  # 4th loss in a row
    assert events[0].type == "losing_streak_cooldown" and "2024-06-09" in events[0].message
    assert ev(rm, date="2024-06-08").check == "cooldown"
    assert ev(rm, date="2024-06-09").result == "pass"
    assert ev(rm, date="2024-06-05", strategy="S2").result == "pass"  # only the losing strategy pauses


def test_a_win_resets_the_streak():
    rm = RiskManager(CFG, 10_000)
    for pnl in (-1, -1, -1, +5, -1, -1, -1):
        assert rm.on_trade_closed("S1", pnl, "2024-06-01") == []


def test_tiny_positions_are_skipped():
    d = ev(RiskManager(CFG, 10_000), max_affordable_qty=0.05)  # $5 of BTC-equivalent
    assert d.result == "blocked" and d.check == "size"


def test_filled_gap_bar_never_entered():
    assert ev(RiskManager(CFG, 10_000), filled_bar=True).result == "blocked"


def test_state_roundtrip():
    rm = RiskManager(CFG, 10_000)
    rm.on_close("d1", 9_000)
    rm.on_trade_closed("S1", -5, "2024-01-01")
    rm2 = RiskManager.from_dict(CFG, rm.to_dict())
    assert rm2.to_dict() == rm.to_dict()
