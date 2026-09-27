"""SimBroker: next-open fills with slippage + fees, resting stops, gap-through-stop, cash limits."""

import pytest

from trader.broker import CostModel, SimBroker

COSTS = CostModel(fee_rate=0.001, slip_major=0.0005, slip_other=0.0015, major_assets=["BTC", "ETH"])


def test_buy_fills_at_next_open_with_slippage_and_fee():
    b = SimBroker(cash=10_000, costs=COSTS)
    b.queue(symbol="BTC/USD", strategy="S1", side="buy", qty=1.0, reason="entry", created_date="2024-01-01",
            stop_distance=50.0)
    assert b.positions == {}  # nothing fills at the decision bar
    _, fills, _ = b.fill_at_open("2024-01-02", {"BTC/USD": 1000.0})
    f = fills[0]
    assert f.px == pytest.approx(1000.5)  # 1000 x (1 + 0.05%)
    assert f.fee == pytest.approx(1000.5 * 0.001)
    assert b.cash == pytest.approx(10_000 - 1000.5 - 1.0005)
    pos = b.positions[("S1", "BTC/USD")]
    assert pos.initial_stop == pos.stop == pytest.approx(1000.5 - 50.0)  # stop = entry FILL - distance


def test_other_assets_use_higher_slippage():
    assert COSTS.slippage("SOL/USD") == 0.0015 and COSTS.slippage("XRP/USD") == 0.0015
    assert COSTS.slippage("ETH/USD") == 0.0005


def test_sell_and_pnl_accounting():
    b = SimBroker(cash=10_000, costs=COSTS)
    b.queue(symbol="BTC/USD", strategy="S1", side="buy", qty=2.0, reason="entry", created_date="d0", stop_distance=100)
    b.fill_at_open("d1", {"BTC/USD": 1000.0})
    b.queue(symbol="BTC/USD", strategy="S1", side="sell", qty=2.0, reason="donchian_exit", created_date="d5")
    _, fills, trades = b.fill_at_open("d6", {"BTC/USD": 1200.0})
    t = trades[0]
    entry_px, exit_px = 1000 * 1.0005, 1200 * 0.9995
    fees = 2 * entry_px * 0.001 + 2 * exit_px * 0.001
    assert t.pnl == pytest.approx((exit_px - entry_px) * 2 - fees)
    assert t.r_multiple == pytest.approx(t.pnl / (2 * 100))
    assert b.cash == pytest.approx(10_000 + t.pnl)  # flat again: cash = start + P&L
    assert t.slippage == pytest.approx(2 * 1000 * 0.0005 + 2 * 1200 * 0.0005)


def test_stop_fills_at_stop_price_when_low_touches():
    b = SimBroker(cash=10_000, costs=COSTS)
    b.queue(symbol="BTC/USD", strategy="S1", side="buy", qty=1.0, reason="entry", created_date="d0", stop_distance=100)
    b.fill_at_open("d1", {"BTC/USD": 1000.0})
    stop = b.positions[("S1", "BTC/USD")].stop
    fills, trades = b.check_stops("d2", {"BTC/USD": (990.0, 1000.0, stop - 5, 995.0)})
    assert fills[0].raw_px == pytest.approx(stop) and trades[0].exit_reason == "stop"
    assert fills[0].px == pytest.approx(stop * 0.9995)


def test_gap_through_stop_fills_at_the_open_not_the_stop():
    b = SimBroker(cash=10_000, costs=COSTS)
    b.queue(symbol="BTC/USD", strategy="S1", side="buy", qty=1.0, reason="entry", created_date="d0", stop_distance=100)
    b.fill_at_open("d1", {"BTC/USD": 1000.0})
    stop = b.positions[("S1", "BTC/USD")].stop  # ~900.5
    fills, trades = b.check_stops("d2", {"BTC/USD": (850.0, 860.0, 840.0, 855.0)})  # opens below the stop
    assert fills[0].raw_px == 850.0 and trades[0].exit_reason == "stop_gap"
    assert trades[0].exit_px == pytest.approx(850 * 0.9995)
    assert trades[0].r_multiple < -1  # a gap makes the loss worse than 1R


def test_entry_day_stop_uses_low_only():
    b = SimBroker(cash=10_000, costs=COSTS)
    b.queue(symbol="BTC/USD", strategy="S1", side="buy", qty=1.0, reason="entry", created_date="d0", stop_distance=10)
    b.fill_at_open("d1", {"BTC/USD": 1000.0})
    stop = b.positions[("S1", "BTC/USD")].stop
    fills, trades = b.check_stops("d1", {"BTC/USD": (1000.0, 1010.0, stop - 1, 1005.0)})
    assert trades[0].exit_reason == "stop" and fills[0].raw_px == pytest.approx(stop)


def test_buy_shrinks_to_available_cash_at_fill():
    b = SimBroker(cash=1_000, costs=COSTS)
    b.queue(symbol="BTC/USD", strategy="S1", side="buy", qty=1.0, reason="entry", created_date="d0", stop_distance=10)
    orders, fills, _ = b.fill_at_open("d1", {"BTC/USD": 1500.0})  # opened far above the planned price
    assert fills[0].qty == pytest.approx(1000 / (1500 * 1.0005 * 1.001))
    assert b.cash == pytest.approx(0.0, abs=1e-9)
    assert orders[0].qty == 1.0 and orders[0].filled_qty == pytest.approx(fills[0].qty)  # plan kept, fill recorded
    assert "shrunk at fill" in fills[0].note


def test_sell_order_for_already_stopped_position_is_cancelled():
    b = SimBroker(cash=10_000, costs=COSTS)
    b.queue(symbol="BTC/USD", strategy="S1", side="buy", qty=1.0, reason="entry", created_date="d0", stop_distance=10)
    b.fill_at_open("d1", {"BTC/USD": 1000.0})
    b.check_stops("d2", {"BTC/USD": (1000.0, 1000.0, 900.0, 950.0)})
    b.queue(symbol="BTC/USD", strategy="S1", side="sell", qty=1.0, reason="donchian_exit", created_date="d2")
    orders, fills, trades = b.fill_at_open("d3", {"BTC/USD": 950.0})
    assert orders[0].status == "cancelled" and not fills and not trades


def test_state_roundtrip():
    b = SimBroker(cash=10_000, costs=COSTS)
    b.queue(symbol="BTC/USD", strategy="S1", side="buy", qty=1.0, reason="entry", created_date="d0", stop_distance=10)
    b.fill_at_open("d1", {"BTC/USD": 1000.0})
    b.queue(symbol="BTC/USD", strategy="S1", side="sell", qty=1.0, reason="x", created_date="d1")
    b2 = SimBroker.from_dict(b.to_dict(), COSTS)
    assert b2.to_dict() == b.to_dict()


def test_zero_cost_model():
    z = CostModel.zero()
    assert z.is_zero and z.slippage("BTC/USD") == 0 and z.fee_rate == 0
