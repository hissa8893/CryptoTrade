"""Backtest orchestration: run the engine over history, compute metrics vs Buy & Hold,
persist the run to SQLite, and write the HTML report."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd
from sqlalchemy import text

from trader import __version__
from trader.broker import CostModel
from trader.config import AppConfig
from trader.db import Database, git_commit
from trader.engine import Engine, Journal
from trader.metrics import Metrics, compute_metrics, red_flags
from trader.paths import Paths
from trader.strategies import build_strategy
from trader.timeutil import now_iso


@dataclass
class Benchmark:
    name: str
    equity: pd.Series
    metrics: Metrics


@dataclass
class BacktestResult:
    label: str
    strategies: list[str]
    symbols: list[str]
    data_source: str
    costs: dict
    params: dict
    equity: pd.Series
    journal: Journal
    metrics: Metrics
    flags: list[str]
    benchmarks: list[Benchmark]
    open_positions: list[dict] = field(default_factory=list)
    run_id: int | None = None
    report_path: str | None = None


def buy_and_hold(frames: dict[str, pd.DataFrame], symbols: list[str], start: str, end: str,
                 costs: CostModel, starting_equity: float) -> pd.Series:
    """Equal-weight buy at the first OPEN on/after `start` (with slippage + fee), hold, mark at close.
    Symbols that start trading later are bought on their first bar (cash waits until then)."""
    idx = pd.date_range(start, end, freq="D", tz="UTC")
    per = starting_equity / len(symbols)
    total = pd.Series(0.0, index=idx)
    for sym in symbols:
        df = frames[sym].loc[start:end]
        if df.empty:
            total += per
            continue
        entry = df["open"].iloc[0] * (1 + costs.slippage(sym))
        qty = per / (entry * (1 + costs.fee_rate))
        value = (df["close"] * qty).reindex(idx)
        value = value.where(value.index >= df.index[0], per).ffill()
        total += value
    return total


def eval_start(cfg: AppConfig, frames: dict[str, pd.DataFrame], strategies, regime_symbol: str | None) -> str:
    """First date where every strategy's indicators and the regime filter are valid."""
    warm = max(s.warmup_bars() for s in strategies)
    starts = [df.index[min(warm, len(df) - 1)] for df in frames.values() if len(df)]
    first = min(starts)
    if cfg.risk.regime_filter_enabled and regime_symbol in frames:
        reg = frames[regime_symbol]
        first = max(first, reg.index[min(cfg.risk.regime_sma - 1, len(reg) - 1)])
    return first.date().isoformat()


def make_engine(
    cfg: AppConfig,
    frames: dict[str, pd.DataFrame],
    strategy_names: list[str],
    *,
    symbols: list[str],
    start: str | None = None,
    end: str | None = None,
    costs: CostModel | None = None,
    starting_equity: float | None = None,
    regime_symbol: str | None = None,
) -> tuple[Engine, dict[str, pd.DataFrame], str, str]:
    """Build the engine exactly as backtests (and the look-ahead proof) use it.
    Returns (engine, frames truncated to `end`, eval start, end)."""
    costs = costs or CostModel.from_config(cfg.costs)
    strategies = [build_strategy(n, cfg) for n in strategy_names]
    regime_symbol = regime_symbol or next((s for s in frames if s.split("/")[0] == cfg.risk.regime_asset), None)
    if end:
        frames = {s: df.loc[:end] for s, df in frames.items()}
    start = max(start or "0000", eval_start(cfg, frames, strategies, regime_symbol))
    end = end or max(df.index[-1] for df in frames.values() if len(df)).date().isoformat()
    # a coin with no history yet (listed after `end`) simply is not tradable in this run
    symbols = [s for s in symbols if len(frames[s])]
    if not symbols:
        raise ValueError(f"none of the requested symbols has data before {end}")
    engine_frames = {s: frames[s] for s in symbols}
    if cfg.risk.regime_filter_enabled and regime_symbol and regime_symbol not in engine_frames:
        engine_frames[regime_symbol] = frames[regime_symbol]
    eng = Engine(strategies, engine_frames, costs=costs, risk_cfg=cfg.risk,
                 starting_equity=starting_equity or cfg.accounts.starting_equity,
                 regime_symbol=regime_symbol, start=start, tradable=set(symbols))
    return eng, frames, start, end


def run_backtest(
    cfg: AppConfig,
    frames: dict[str, pd.DataFrame],
    strategy_names: list[str],
    *,
    symbols: list[str],
    data_source: str,
    start: str | None = None,
    end: str | None = None,
    costs: CostModel | None = None,
    starting_equity: float | None = None,
    regime_symbol: str | None = None,
) -> BacktestResult:
    costs = costs or CostModel.from_config(cfg.costs)
    equity0 = starting_equity or cfg.accounts.starting_equity
    eng, frames, start, end = make_engine(cfg, frames, strategy_names, symbols=symbols, start=start, end=end,
                                          costs=costs, starting_equity=equity0, regime_symbol=regime_symbol)
    strategies = list(eng.strategies.values())
    regime_symbol = eng.regime_symbol
    journal = eng.run()
    eq = pd.Series({pd.Timestamp(e["bar_date"], tz="UTC"): e["equity"] for e in journal.equity}).sort_index()
    exposure = sum(1 for e in journal.equity if e["positions"] > 0)
    m = compute_metrics(eq, journal.trades, exposure)
    benches = []
    for sym in symbols:
        bh = buy_and_hold(frames, [sym], start, end, costs, equity0)
        benches.append(Benchmark(f"Buy & Hold {sym}", bh, compute_metrics(bh, [])))
    if len(symbols) > 1:
        bh = buy_and_hold(frames, symbols, start, end, costs, equity0)
        benches.append(Benchmark("Buy & Hold equal-weight basket", bh, compute_metrics(bh, [])))
    label = "+".join(strategy_names) + " on " + ", ".join(symbols)
    params = {
        "strategies": {s.name: s.params() for s in strategies},
        "risk": cfg.risk.model_dump(),
        "symbols": symbols,
        "regime_symbol": regime_symbol,
        "starting_equity": equity0,
    }
    return BacktestResult(
        label=label, strategies=strategy_names, symbols=symbols, data_source=data_source, costs=costs.describe(),
        params=params, equity=eq, journal=journal, metrics=m, flags=red_flags(m), benchmarks=benches,
        open_positions=[asdict(p) for p in eng.broker.positions.values()],
    )


# ------------------------------------------------------------------------------ persistence
def persist_backtest(db: Database, paths: Paths, res: BacktestResult) -> int:
    """Write the whole run in ONE transaction (all or nothing)."""
    j = res.journal
    run_key = f"bt:{uuid.uuid4().hex[:12]}"
    created = now_iso()
    with db.tx() as c:
        c.execute(text(
            "INSERT INTO runs (run_key, mode, strategy, params_json, start, \"end\", created_at, git_commit, app_version, "
            "starting_equity, data_source) VALUES (:k, 'backtest', :s, :p, :st, :en, :c, :g, :v, :eq, :src)"),
            {"k": run_key, "s": "+".join(res.strategies), "p": json.dumps(res.params | {"costs": res.costs}),
             "st": res.metrics.start, "en": res.metrics.end, "c": created, "g": git_commit(paths.root),
             "v": __version__, "eq": res.metrics.start_equity, "src": res.data_source})
        run_id = c.execute(text("SELECT id FROM runs WHERE run_key = :k"), {"k": run_key}).scalar_one()
        sig_ids, dec_ids = {}, {}
        for s in j.signals:
            r = c.execute(text(
                "INSERT INTO signals (run_id, bar_date, symbol, strategy, signal, strength, indicators_json, created_at) "
                "VALUES (:r, :d, :sym, :st, :sig, :stren, :ind, :c)"),
                {"r": run_id, "d": s["bar_date"], "sym": s["symbol"], "st": s["strategy"], "sig": s["signal"],
                 "stren": s["strength"], "ind": json.dumps(s["indicators"]), "c": created})
            sig_ids[s["ref"]] = r.lastrowid
        for d in j.decisions:
            r = c.execute(text(
                "INSERT INTO decisions (signal_id, risk_result, risk_reason, final_action, final_qty, created_at) "
                "VALUES (:s, :rr, :why, :a, :q, :c)"),
                {"s": sig_ids[d["signal_ref"]], "rr": d["risk_result"], "why": d["risk_reason"],
                 "a": d["final_action"], "q": d["final_qty"], "c": created})
            dec_ids[d["ref"]] = r.lastrowid
        if j.orders:
            c.execute(text(
                "INSERT INTO orders (run_id, decision_id, symbol, strategy, side, qty, reason, stop_price, "
                "created_bar_date, fill_bar_date, status, fill_px, fee, slippage, created_at) VALUES (:r, :dec, :sym, "
                ":st, :side, :q, :why, :stop, :cd, :fd, :status, :px, :fee, :slip, :c)"),
                [{"r": run_id, "dec": dec_ids.get(o.decision_ref), "sym": o.symbol, "st": o.strategy, "side": o.side,
                  "q": o.qty, "why": o.reason, "stop": o.stop_distance, "cd": o.created_date, "fd": o.fill_date,
                  "status": o.status, "px": o.fill_px, "fee": o.fee, "slip": o.slippage, "c": created}
                 for o in j.orders])
        if j.trades:
            c.execute(text(
                "INSERT INTO trades (run_id, symbol, strategy, entry_ts, entry_px, qty, initial_stop, exit_ts, exit_px, "
                "exit_reason, fees, slippage, pnl, pnl_pct, r_multiple, entry_signal_id, exit_signal_id) VALUES "
                "(:r, :sym, :st, :et, :ep, :q, :istop, :xt, :xp, :why, :fees, :slip, :pnl, :pct, :rm, :es, :xs)"),
                [{"r": run_id, "sym": t.symbol, "st": t.strategy, "et": t.entry_date, "ep": t.entry_px, "q": t.qty,
                  "istop": t.initial_stop, "xt": t.exit_date, "xp": t.exit_px, "why": t.exit_reason, "fees": t.fees,
                  "slip": t.slippage, "pnl": t.pnl, "pct": t.pnl_pct, "rm": t.r_multiple,
                  "es": sig_ids.get(t.entry_signal_ref), "xs": sig_ids.get(t.exit_signal_ref)} for t in j.trades])
        c.execute(text(
            "INSERT INTO equity_snapshots (run_id, bar_date, equity, cash, positions_value, open_risk, peak_equity, "
            "drawdown_pct) VALUES (:r, :d, :e, :cash, :pv, :orisk, :peak, :dd)"),
            [{"r": run_id, "d": e["bar_date"], "e": e["equity"], "cash": e["cash"], "pv": e["positions_value"],
              "orisk": e["open_risk"], "peak": e["peak_equity"], "dd": e["drawdown_pct"]} for e in j.equity])
        if j.events:
            c.execute(text(
                "INSERT INTO risk_events (run_id, ts, bar_date, type, severity, symbol, strategy, message, details_json, "
                "dedupe_key) VALUES (:r, :ts, :d, :t, :sev, :sym, :st, :m, :det, :k)"),
                [{"r": run_id, "ts": f"{e.date}T00:00:00+00:00", "d": e.date, "t": e.type, "sev": e.severity,
                  "sym": e.symbol, "st": e.strategy, "m": e.message, "det": json.dumps(e.details, default=float),
                  "k": f"{run_key}:{i}"} for i, e in enumerate(j.events)])
        for p in res.open_positions:
            c.execute(text(
                "INSERT INTO positions (run_id, symbol, strategy, qty, avg_entry_px, entry_ts, initial_stop, current_stop, "
                "highest_high, last_price, state_json, updated_at) VALUES (:r, :sym, :st, :q, :px, :et, :istop, :stop, "
                ":hh, :lp, :state, :u)"),
                {"r": run_id, "sym": p["symbol"], "st": p["strategy"], "q": p["qty"], "px": p["entry_px"],
                 "et": p["entry_date"], "istop": p["initial_stop"], "stop": p["stop"], "hh": p["highest_high"],
                 "lp": None, "state": json.dumps(p, default=float), "u": created})
    res.run_id = run_id
    return run_id


def trade_rows(res: BacktestResult) -> list[dict]:
    return [asdict(t) for t in res.journal.trades]


def as_float(x) -> float | None:
    return None if x is None or (isinstance(x, float) and not np.isfinite(x)) else float(x)
