"""Metrics by hand, DB persistence (all-or-nothing, decision trail), report, CLI."""

import math
import re
from datetime import date, datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from trader.backtest import buy_and_hold, persist_backtest, run_backtest
from trader.broker import CostModel, Trade
from trader.cli import app
from trader.config import AppConfig
from trader.data import clean_and_validate
from trader.db import Database
from trader.metrics import compute_metrics, drawdown_stats, red_flags
from trader.reports import render_backtest_report
from trader.synthetic import generate

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


def _eq(values, start="2024-01-01"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="D", tz="UTC"), dtype=float)


def _t(pnl, r):
    return Trade(1, "BTC/USD", "S1", "a", 100, 1, 90, "b", 100 + pnl, "x", 1.0, 0.5, pnl, pnl / 100, r, 3)


def test_metrics_by_hand():
    eq = _eq([100, 110, 99, 121])  # returns +10%, -10%, +22.22%
    m = compute_metrics(eq, [_t(20, 2.0), _t(-10, -1.0), _t(-5, -0.5)], exposure_days=2)
    assert m.total_return == pytest.approx(0.21)
    assert m.cagr == pytest.approx(1.21 ** (365 / 3) - 1)
    assert m.max_drawdown == pytest.approx(0.10)  # 110 -> 99
    r = [0.1, -0.1, 121 / 99 - 1]
    mean, sd = sum(r) / 3, math.sqrt(sum((x - sum(r) / 3) ** 2 for x in r) / 2)
    assert m.sharpe == pytest.approx(mean / sd * math.sqrt(365))
    assert m.sortino == pytest.approx(mean / math.sqrt(0.01 / 3) * math.sqrt(365))
    assert m.win_rate == pytest.approx(1 / 3)
    assert m.profit_factor == pytest.approx(20 / 15)
    assert m.avg_r == pytest.approx(0.5 / 3)
    assert m.expectancy == pytest.approx(5 / 3)
    assert m.exposure == pytest.approx(0.5)
    assert m.fees == pytest.approx(3.0)


def test_drawdown_duration_and_dates():
    eq = _eq([100, 120, 90, 100, 125, 110])
    dd, days, peak, trough = drawdown_stats(eq)
    assert dd == pytest.approx(0.25) and peak == "2024-01-02" and trough == "2024-01-03"
    assert days == 3  # under water from Jan 3 until the new high on Jan 5


def test_red_flags():
    m = compute_metrics(_eq([100, 101]), [_t(1, 1)] * 5)
    flags = red_flags(m)
    assert any("< 30" in f for f in flags)
    assert any("Out-of-sample" in f for f in red_flags(m, oos_sharpe=0.4, is_sharpe=1.0))


def test_buy_and_hold_includes_costs():
    df = pd.DataFrame({"open": [100.0, 110.0], "close": [105.0, 120.0]},
                      index=pd.date_range("2024-01-01", periods=2, freq="D", tz="UTC"))
    costs = CostModel(0.001, 0.0005, 0.0015, ["BTC"])
    bh = buy_and_hold({"BTC/USD": df}, ["BTC/USD"], "2024-01-01", "2024-01-02", costs, 1000)
    qty = 1000 / (100 * 1.0005 * 1.001)
    assert bh.iloc[-1] == pytest.approx(qty * 120)


@pytest.fixture(scope="module")
def result():
    df, _ = clean_and_validate(generate("BTC", end=date(2026, 9, 26)), "BTC/USD", now=NOW)
    return run_backtest(AppConfig(), {"BTC/USD": df}, ["S1"], symbols=["BTC/USD"], data_source="synthetic")


def test_persist_writes_everything_with_a_decision_trail(tmp_path, home, result):
    db = Database(tmp_path / "t.db")
    db.migrate()
    run_id = persist_backtest(db, home, result)
    j = result.journal
    with db.read() as c:
        count = lambda t: c.execute(text(f"SELECT COUNT(*) FROM {t} WHERE run_id = :r"), {"r": run_id}).scalar()
        assert count("signals") == len(j.signals)
        assert count("trades") == len(j.trades)
        assert count("equity_snapshots") == len(j.equity)
        assert count("risk_events") == len(j.events)
        assert count("orders") == len(j.orders)
        # every closed trade traces back: trade -> entry signal -> risk decision -> filled order
        rows = c.execute(text(
            "SELECT t.id, s.signal, d.risk_result, o.status FROM trades t JOIN signals s ON s.id = t.entry_signal_id "
            "JOIN decisions d ON d.signal_id = s.id JOIN orders o ON o.decision_id = d.id "
            "WHERE t.run_id = :r AND o.side = 'buy'"), {"r": run_id}).fetchall()
        assert len(rows) == len(j.trades)
        assert all(r[1] == "enter" and r[2] in ("pass", "shrunk") and r[3] == "filled" for r in rows)
        run = c.execute(text("SELECT mode, data_source, strategy FROM runs WHERE id = :r"), {"r": run_id}).one()
        assert tuple(run) == ("backtest", "synthetic", "S1")
    db.dispose()


def test_persist_is_all_or_nothing(tmp_path, home, result, monkeypatch):
    db = Database(tmp_path / "t.db")
    db.migrate()
    # a duplicate equity day violates UNIQUE(run_id, bar_date) near the END of the write
    monkeypatch.setattr(result.journal, "equity", result.journal.equity + [dict(result.journal.equity[-1])])
    with pytest.raises(Exception):
        persist_backtest(db, home, result)
    with db.read() as c:
        for t in ("runs", "signals", "trades", "equity_snapshots", "risk_events", "orders"):
            assert c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() == 0, t
    db.dispose()


def test_report_is_self_contained_and_labelled(result):
    html = render_backtest_report(result)
    assert "SYNTHETIC DATA" in html and "do not predict future returns" in html
    assert not re.search(r"""(src|href)=["']https?://""", html)  # no external requests
    assert "Content-Security-Policy" in html
    assert f"Trades ({result.metrics.trades})" in html


def test_cli_backtest_and_lookahead(home):
    runner = CliRunner()
    runner.invoke(app, ["init"])
    home.config_file.write_text(home.config_file.read_text().replace("source: exchange", "source: synthetic"))
    assert runner.invoke(app, ["data", "fetch", "-a", "BTC"]).exit_code == 0
    r = runner.invoke(app, ["backtest", "-s", "S1", "-a", "BTC"])
    assert r.exit_code == 0, r.output
    assert "SYNTHETIC" in r.output and "report:" in r.output
    assert list(home.reports.glob("backtest_S1_BTC_*.html"))
    r = runner.invoke(app, ["verify", "lookahead", "-a", "BTC", "--samples", "20"])
    assert r.exit_code == 0 and "PASS" in r.output, r.output
