"""Write an engine Journal to SQLite. Used by BOTH backtests and live paper runs, so the
database looks the same whichever produced it. Call inside a transaction (db.tx()).

Plain INSERTs on purpose: the unique constraints make an accidental second write of the
same day fail loudly (and roll the whole day back) instead of silently duplicating.
Orders are the exception: they are inserted when queued and updated when filled."""

from __future__ import annotations

import json
from collections import Counter

from sqlalchemy import text
from sqlalchemy.engine import Connection

from trader.engine import Engine, Journal


def write_journal(c: Connection, run_id: int, run_key: str, j: Journal, created: str) -> None:
    sig_ids: dict[int, int] = {}
    for s in j.signals:
        r = c.execute(text(
            "INSERT INTO signals (run_id, ref, bar_date, symbol, strategy, signal, strength, indicators_json, created_at) "
            "VALUES (:r, :ref, :d, :sym, :st, :sig, :stren, :ind, :c)"),
            {"r": run_id, "ref": s["ref"], "d": s["bar_date"], "sym": s["symbol"], "st": s["strategy"],
             "sig": s["signal"], "stren": s["strength"], "ind": json.dumps(s["indicators"]), "c": created})
        sig_ids[s["ref"]] = r.lastrowid

    def signal_id(ref):
        if ref is None:
            return None
        if ref not in sig_ids:  # written on an earlier day
            sig_ids[ref] = c.execute(text("SELECT id FROM signals WHERE run_id = :r AND ref = :ref"),
                                     {"r": run_id, "ref": ref}).scalar()
        return sig_ids[ref]

    dec_ids: dict[int, int] = {}
    for d in j.decisions:
        r = c.execute(text(
            "INSERT INTO decisions (signal_id, ref, risk_result, risk_reason, final_action, final_qty, created_at) "
            "VALUES (:s, :ref, :rr, :why, :a, :q, :c)"),
            {"s": signal_id(d["signal_ref"]), "ref": d["ref"], "rr": d["risk_result"], "why": d["risk_reason"],
             "a": d["final_action"], "q": d["final_qty"], "c": created})
        dec_ids[d["ref"]] = r.lastrowid

    for o in j.orders:
        c.execute(text(
            "INSERT INTO orders (run_id, ext_id, decision_id, symbol, strategy, side, qty, reason, stop_price, "
            "created_bar_date, fill_bar_date, status, fill_px, fee, slippage, filled_qty, created_at) VALUES "
            "(:r, :x, :dec, :sym, :st, :side, :q, :why, :stop, :cd, :fd, :status, :px, :fee, :slip, :fq, :c) "
            "ON CONFLICT (run_id, ext_id) DO UPDATE SET fill_bar_date = excluded.fill_bar_date, "
            "status = excluded.status, fill_px = excluded.fill_px, fee = excluded.fee, "
            "slippage = excluded.slippage, filled_qty = excluded.filled_qty"),
            {"r": run_id, "x": o.id, "dec": dec_ids.get(o.decision_ref), "sym": o.symbol, "st": o.strategy,
             "side": o.side, "q": o.qty, "why": o.reason, "stop": o.stop_distance, "cd": o.created_date,
             "fd": o.fill_date, "status": o.status, "px": o.fill_px, "fee": o.fee, "slip": o.slippage,
             "fq": o.filled_qty, "c": created})

    for t in j.trades:
        c.execute(text(
            "INSERT INTO trades (run_id, symbol, strategy, entry_ts, entry_px, qty, initial_stop, exit_ts, exit_px, "
            "exit_reason, fees, slippage, pnl, pnl_pct, r_multiple, entry_signal_id, exit_signal_id) VALUES "
            "(:r, :sym, :st, :et, :ep, :q, :istop, :xt, :xp, :why, :fees, :slip, :pnl, :pct, :rm, :es, :xs)"),
            {"r": run_id, "sym": t.symbol, "st": t.strategy, "et": t.entry_date, "ep": t.entry_px, "q": t.qty,
             "istop": t.initial_stop, "xt": t.exit_date, "xp": t.exit_px, "why": t.exit_reason, "fees": t.fees,
             "slip": t.slippage, "pnl": t.pnl, "pct": t.pnl_pct, "rm": t.r_multiple,
             "es": signal_id(t.entry_signal_ref), "xs": signal_id(t.exit_signal_ref)})

    if j.equity:
        c.execute(text(
            "INSERT INTO equity_snapshots (run_id, bar_date, equity, cash, positions_value, open_risk, peak_equity, "
            "drawdown_pct) VALUES (:r, :d, :e, :cash, :pv, :orisk, :peak, :dd)"),
            [{"r": run_id, "d": e["bar_date"], "e": e["equity"], "cash": e["cash"], "pv": e["positions_value"],
              "orisk": e["open_risk"], "peak": e["peak_equity"], "dd": e["drawdown_pct"]} for e in j.equity])

    seen: Counter = Counter()
    rows = []
    for e in j.events:
        k = (e.date, e.type, e.symbol, e.strategy)
        seen[k] += 1
        rows.append({"r": run_id, "ts": f"{e.date}T00:00:00+00:00", "d": e.date, "t": e.type, "sev": e.severity,
                     "sym": e.symbol, "st": e.strategy, "m": e.message, "det": json.dumps(e.details, default=float),
                     "k": f"{run_key}:{e.date}:{e.type}:{e.symbol}:{e.strategy}:{seen[k]}"})
    if rows:
        c.execute(text(
            "INSERT INTO risk_events (run_id, ts, bar_date, type, severity, symbol, strategy, message, details_json, "
            "dedupe_key) VALUES (:r, :ts, :d, :t, :sev, :sym, :st, :m, :det, :k)"), rows)


def save_engine_state(c: Connection, run_id: int, eng: Engine, updated: str) -> None:
    """Persist the engine's full state (cash, positions, stops, pending orders, risk state) and
    mirror open positions into the positions table (dashboard + crash-recovery cross-check)."""
    c.execute(text(
        "INSERT INTO run_state (run_id, last_bar_date, state_json, updated_at) VALUES (:r, :d, :s, :u) "
        "ON CONFLICT (run_id) DO UPDATE SET last_bar_date = excluded.last_bar_date, state_json = excluded.state_json, "
        "updated_at = excluded.updated_at"),
        {"r": run_id, "d": eng.last_date, "s": json.dumps(eng.state_dict()), "u": updated})
    write_positions(c, run_id, eng.state_dict()["broker"]["positions"], eng.last_close, updated)


def write_positions(c: Connection, run_id: int, positions: list[dict], last_close: dict, updated: str) -> None:
    c.execute(text("DELETE FROM positions WHERE run_id = :r"), {"r": run_id})
    for p in positions:
        c.execute(text(
            "INSERT INTO positions (run_id, symbol, strategy, qty, avg_entry_px, entry_ts, initial_stop, current_stop, "
            "highest_high, last_price, state_json, updated_at) VALUES (:r, :sym, :st, :q, :px, :et, :istop, :stop, "
            ":hh, :lp, :state, :u)"),
            {"r": run_id, "sym": p["symbol"], "st": p["strategy"], "q": p["qty"], "px": p["entry_px"],
             "et": p["entry_date"], "istop": p["initial_stop"], "stop": p["stop"], "hh": p["highest_high"],
             "lp": last_close.get(p["symbol"]), "state": json.dumps(p), "u": updated})
