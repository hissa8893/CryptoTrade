"""Read-only data layer for the dashboard. Every number the page shows comes from here, so
tests can check each one against a direct database query."""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd
from sqlalchemy import text

from trader import indicators as ind
from trader.backtest import buy_and_hold
from trader.broker import CostModel
from trader.config import AppConfig
from trader.data import MarketData, freshness_problem
from trader.db import Database
from trader.metrics import compute_metrics
from trader.paths import Paths
from trader.timeutil import now_utc

ORDER = ["S1", "S2", "S3", "PORTFOLIO"]
LABELS = {"S1": "S1 Donchian", "S2": "S2 Supertrend", "S3": "S3 Momentum", "PORTFOLIO": "Portfolio (S1+S2+S3)"}
RANGES = {"7d": 7, "30d": 30, "90d": 90, "all": None}
EXIT_LABELS = {
    "stop": ("■", "stop hit"), "stop_gap": ("▼", "gapped through stop"), "donchian_exit": ("↘", "channel exit"),
    "chandelier_exit": ("↘", "trailing exit"), "supertrend_flip": ("↘", "trend flipped"),
    "momentum_off": ("↘", "momentum off"),
}
NEAR_STOP = 0.03  # highlight positions within 3% of their stop


def _fmt_ind(k: str, v: float) -> str:
    """Indicator snapshot for humans: returns/volatility/weights as %, prices with commas."""
    name = k.replace("_", " ")
    if k.startswith("ret") or k in ("vol", "change", "weight"):
        return f"{name} {v * 100:+.2f}%" if k != "vol" and k != "weight" else f"{name} {v * 100:.1f}%"
    return f"{name} {v:,.6g}"


def _age_hours(ts: str | None) -> float | None:
    if not ts:
        return None
    return (now_utc() - datetime.fromisoformat(ts)).total_seconds() / 3600


class Dashboard:
    def __init__(self, cfg: AppConfig, paths: Paths, db: Database, md: MarketData, runtime=None, next_run=None):
        self.cfg, self.paths, self.db, self.md = cfg, paths, db, md
        self.runtime = runtime
        self.next_run = next_run or (lambda: None)
        self._frames_cache: tuple[float, dict] | None = None

    # -- market data (cached until the parquet files change) --------------------------------------
    def frames(self) -> dict[str, pd.DataFrame]:
        files = list(self.md.cache.dir.glob("*_1d.parquet")) if self.md.cache.dir.exists() else []
        stamp = max((f.stat().st_mtime for f in files), default=0.0)
        if self._frames_cache is None or self._frames_cache[0] != stamp:
            self._frames_cache = (stamp, self.md.load_all())
        return self._frames_cache[1]

    # -- accounts ------------------------------------------------------------------------------
    def accounts(self) -> list[dict]:
        prefix = f"paper:{self.md.source}:"
        with self.db.read() as c:
            rows = c.execute(text("SELECT id, run_key, start, starting_equity FROM runs WHERE mode = 'paper' "
                                  "AND run_key LIKE :p"), {"p": prefix + "%"}).fetchall()
        out = [{"run_id": r[0], "key": r[1], "name": r[1][len(prefix):], "start": r[2], "starting_equity": r[3]}
               for r in rows]
        out.sort(key=lambda a: ORDER.index(a["name"]) if a["name"] in ORDER else 99)
        for a in out:
            a["label"] = LABELS.get(a["name"], a["name"])
        return out

    def account(self, name: str | None) -> dict | None:
        accts = self.accounts()
        if not accts:
            return None
        by = {a["name"]: a for a in accts}
        return by.get((name or "").upper()) or by.get("PORTFOLIO") or accts[0]

    def _state(self, run_id: int) -> dict | None:
        with self.db.read() as c:
            row = c.execute(text("SELECT state_json FROM run_state WHERE run_id = :r"), {"r": run_id}).fetchone()
        return json.loads(row[0]) if row else None

    # -- status bar ----------------------------------------------------------------------------
    def status(self, acct: dict | None) -> dict:
        with self.db.read() as c:
            last_ok = c.execute(text("SELECT bar_date, finished_at FROM job_runs WHERE status = 'ok' "
                                     "ORDER BY bar_date DESC LIMIT 1")).fetchone()
            last_job = c.execute(text("SELECT bar_date, status, error, attempts FROM job_runs "
                                      "ORDER BY bar_date DESC LIMIT 1")).fetchone()
        hb = self.db.get_kv("heartbeat")
        busy = bool(self.runtime and self.runtime.busy)
        age = _age_hours(last_ok[1]) if last_ok else None
        stale_h = self.cfg.scheduler.heartbeat_stale_hours
        level, note = "ok", None
        if last_ok is None:
            level, note = ("warn", "first run in progress") if busy else ("bad", "no successful run yet")
        elif age is not None and age > stale_h:
            level, note = "bad", f"last successful run is {age:.0f} h old (limit {stale_h:.0f} h)"
        if last_job and last_job[1] in ("skipped", "failed") and level == "ok":
            level, note = "warn", f"last attempt {last_job[1]} ({last_job[0]}): {last_job[2]}"
        hb_age = _age_hours(hb)
        if hb_age is not None and hb_age > 0.25 and level == "ok":  # scheduler thread silent for 15 min
            level, note = "warn", f"no heartbeat for {hb_age * 60:.0f} min"
        return {
            "level": level, "note": note, "busy": busy,
            "last_ok_day": last_ok[0] if last_ok else None, "last_ok_at": last_ok[1] if last_ok else None,
            "last_ok_age_h": age, "next_run": self.next_run(), "heartbeat": hb,
            "data": self.data_feed(), "risk": self.risk_state(acct), "synthetic": self.md.source == "synthetic",
            "now": now_utc().isoformat(),
        }

    def data_feed(self) -> dict:
        syms = self.md.symbols()
        if not syms:
            return {"level": "bad", "text": "no price data downloaded yet"}
        frames = self.frames()
        problems = []
        last = None
        for s in syms:
            df = frames.get(s)
            if df is None or df.empty:
                problems.append(f"{s.split('/')[0]}: invalid or missing")
                continue
            p = freshness_problem(df)
            if p:
                problems.append(f"{s.split('/')[0]}: {p}")
            d = df.index[-1].date()
            last = d if last is None else min(last, d)
        src = "SYNTHETIC data" if self.md.source == "synthetic" else self.md.source.capitalize()
        if problems:
            return {"level": "warn", "text": f"{src} · " + "; ".join(problems[:2])}
        return {"level": "ok", "text": f"{src} · fresh through {last:%b %d}".replace(" 0", " ")}

    def risk_state(self, acct: dict | None) -> dict:
        if acct is None:
            return {"level": "ok", "text": "No paper account yet"}
        st = self._state(acct["run_id"])
        if not st:
            return {"level": "ok", "text": "Normal — entries allowed"}
        r, day = st["risk"], st["last_date"]
        if r.get("breaker_active"):
            return {"level": "bad", "text": "Circuit breaker on — new entries blocked"}
        if r.get("daily_loss_block_date") == day:
            return {"level": "bad", "text": "Daily loss cap hit — no new entries today"}
        paused = [f"{s} paused until {u}" for s, u in (r.get("cooldown_until") or {}).items() if day and u > day]
        regime = self.regime_bull(day)
        if regime is False and self.cfg.risk.regime_filter_enabled:
            return {"level": "warn", "text": f"Bear market regime ({self.cfg.risk.regime_asset} below its "
                                             f"{self.cfg.risk.regime_sma}-day average) — no new longs"
                                             + (f"; {', '.join(paused)}" if paused else "")}
        if paused:
            return {"level": "warn", "text": "Losing-streak pause: " + ", ".join(paused)}
        # capacity limits: not alarms, but new entries ARE blocked while they hold (say so honestly)
        head = self.headline(acct)
        if head and head["positions"] >= self.cfg.risk.max_positions:
            return {"level": "ok", "text": f"All {self.cfg.risk.max_positions} position slots in use — no new entries"}
        if head and head["open_risk_pct"] >= self.cfg.risk.max_portfolio_heat:
            return {"level": "ok", "text": f"Open risk {head['open_risk_pct'] * 100:.2f}% has reached the "
                                           f"{self.cfg.risk.max_portfolio_heat * 100:.2f}% limit — no new entries"}
        return {"level": "ok", "text": "Normal — entries allowed"}

    def regime_bull(self, day: str | None) -> bool | None:
        btc = next((df for s, df in self.frames().items() if s.split("/")[0] == self.cfg.risk.regime_asset), None)
        if btc is None or day is None:
            return None
        sma = ind.sma(btc["close"], self.cfg.risk.regime_sma)
        ts = pd.Timestamp(day, tz="UTC")
        if ts not in btc.index or not math.isfinite(sma.loc[ts]):
            return None
        return bool(btc.loc[ts, "close"] > sma.loc[ts])

    # -- headline ------------------------------------------------------------------------------
    def headline(self, acct: dict | None) -> dict | None:
        if acct is None:
            return None
        with self.db.read() as c:
            rows = c.execute(text("SELECT bar_date, equity, open_risk, drawdown_pct, peak_equity FROM equity_snapshots "
                                  "WHERE run_id = :r ORDER BY bar_date DESC LIMIT 2"), {"r": acct["run_id"]}).fetchall()
            npos = c.execute(text("SELECT COUNT(*) FROM positions WHERE run_id = :r"), {"r": acct["run_id"]}).scalar()
        if not rows:
            return None
        day, eq, orisk, dd, peak = rows[0]
        prev = rows[1][1] if len(rows) > 1 else acct["starting_equity"]
        start = acct["starting_equity"]
        return {"day": day, "equity": eq, "day_pnl": eq - prev, "day_pnl_pct": eq / prev - 1,
                "total_return": eq / start - 1, "drawdown": dd, "peak": peak, "positions": npos,
                "open_risk_pct": orisk / eq if eq else 0.0, "starting_equity": start}

    # -- equity vs buy & hold --------------------------------------------------------------------
    def equity_series(self, acct: dict | None, rng: str = "all") -> dict | None:
        if acct is None:
            return None
        with self.db.read() as c:
            rows = c.execute(text("SELECT bar_date, equity FROM equity_snapshots WHERE run_id = :r ORDER BY bar_date"),
                             {"r": acct["run_id"]}).fetchall()
        if not rows:
            return None
        eq = pd.Series({pd.Timestamp(d, tz="UTC"): v for d, v in rows})
        frames = self.frames()
        syms = [s for s in self.md.symbols() if s in frames]
        bh = None
        if syms:
            bh = buy_and_hold(frames, syms, rows[0][0], rows[-1][0], CostModel.from_config(self.cfg.costs),
                              acct["starting_equity"]).reindex(eq.index).ffill()
        days = RANGES.get(rng)
        if days:
            cut = eq.index[-1] - pd.Timedelta(days=days - 1)
            eq = eq[eq.index >= cut]
            bh = bh[bh.index >= cut] if bh is not None else None
        dd = -(1 - eq / eq.cummax())
        out = {"dates": [d.date().isoformat() for d in eq.index], "equity": list(eq.values), "dd": list(dd.values),
               "range": rng}
        if bh is not None and bh.notna().all():
            out["bh"] = list(bh.values)
            out["bh_dd"] = list(-(1 - bh / bh.cummax()))
        return out

    # -- open positions ------------------------------------------------------------------------
    def positions(self, acct: dict | None) -> list[dict]:
        if acct is None:
            return []
        with self.db.read() as c:
            rows = c.execute(text("SELECT symbol, strategy, qty, avg_entry_px, entry_ts, initial_stop, current_stop, "
                                  "last_price FROM positions WHERE run_id = :r ORDER BY symbol, strategy"),
                             {"r": acct["run_id"]}).fetchall()
        out = []
        for sym, strat, qty, entry, entry_ts, istop, stop, last in rows:
            price = last if last is not None else entry
            dist = (price - stop) / price if price else 0.0
            out.append({"symbol": sym, "strategy": strat, "qty": qty, "entry_px": entry, "entry_date": entry_ts,
                        "price": price, "unrealized": (price - entry) * qty, "unrealized_pct": price / entry - 1,
                        "stop": stop, "initial_stop": istop, "stop_moved": stop > istop + 1e-12,
                        "dist_to_stop": dist, "near_stop": dist <= NEAR_STOP,
                        "key": f"{strat}:{sym}", "sig": f"{qty:.10g}:{stop:.10g}"})
        return out

    # -- closed trades + decision trail --------------------------------------------------------------
    def trades(self, acct: dict | None, limit: int = 20) -> list[dict]:
        if acct is None:
            return []
        with self.db.read() as c:
            rows = c.execute(text(
                "SELECT id, symbol, strategy, entry_ts, entry_px, qty, initial_stop, exit_ts, exit_px, exit_reason, "
                "fees, pnl, pnl_pct, r_multiple, entry_signal_id, exit_signal_id FROM trades WHERE run_id = :r "
                "AND exit_ts IS NOT NULL ORDER BY exit_ts DESC, id DESC LIMIT :n"), {"r": acct["run_id"], "n": limit}).fetchall()
            out = []
            for (tid, sym, strat, et, ep, qty, istop, xt, xp, why, fees, pnl, pct, r, es, xs) in rows:
                icon, label = EXIT_LABELS.get(why, ("•", why.replace("_", " ")))
                out.append({"id": tid, "symbol": sym, "strategy": strat, "entry_date": et, "entry_px": ep, "qty": qty,
                            "initial_stop": istop, "exit_date": xt, "exit_px": xp, "exit_reason": why,
                            "exit_icon": icon, "exit_label": label, "fees": fees, "pnl": pnl, "pnl_pct": pct, "r": r,
                            "days": (date.fromisoformat(xt) - date.fromisoformat(et)).days,
                            "trail": self._trail(c, acct["run_id"], es, xs, sym, strat, et, ep, xt, xp, why)})
        return out

    def _trail(self, c, run_id, es, xs, sym, strat, et, ep, xt, xp, why) -> list[dict]:
        steps = []

        def signal_steps(sig_id, kind):
            if not sig_id:
                return None
            s = c.execute(text("SELECT bar_date, signal, strength, indicators_json FROM signals WHERE id = :i"),
                          {"i": sig_id}).fetchone()
            d = c.execute(text("SELECT risk_result, risk_reason, llm_json, final_action, final_qty FROM decisions "
                               "WHERE signal_id = :i"), {"i": sig_id}).fetchone()
            o = c.execute(text("SELECT o.fill_bar_date, o.fill_px, o.fee, o.filled_qty, o.status FROM orders o "
                               "JOIN decisions d ON d.id = o.decision_id WHERE d.signal_id = :i"), {"i": sig_id}).fetchone()
            ind_ = json.loads(s[3]) if s else {}
            steps.append({"step": "signal", "title": f"{kind} signal at the close of {s[0]}",
                          "detail": ", ".join(_fmt_ind(k, v) for k, v in ind_.items() if v is not None)
                          + (f" · strength {s[2]:.2f}" if s and s[2] is not None else "")})
            if d:
                steps.append({"step": "risk", "title": f"Risk check: {d[0]}", "detail": d[1] or ""})
                llm = json.loads(d[2]) if d[2] else None
                steps.append({"step": "llm", "title": "LLM analyst",
                              "detail": (f"{llm.get('decision')} (x{llm.get('size_multiplier')}): "
                                         + "; ".join(llm.get("reasons", []))) if llm else "not used (rules only)"})
            if o and o[4] == "filled":
                steps.append({"step": "fill", "title": f"Filled at the open of {o[0]}",
                              "detail": f"{o[3] or 0:.6g} @ ${o[1]:,.2f} incl. slippage, fee ${o[2]:,.2f}"})
            return True

        signal_steps(es, "Entry")
        if xs:
            signal_steps(xs, "Exit")
        else:  # resting stop: no signal row, the fill IS the event
            steps.append({"step": "fill", "title": f"Stop filled on {xt}",
                          "detail": f"{EXIT_LABELS.get(why, ('', why))[1]} @ ${xp:,.2f} incl. slippage"})
        return steps

    # -- risk events ---------------------------------------------------------------------------
    def events(self, acct: dict | None, limit: int = 15) -> list[dict]:
        params = {"n": limit, "r": acct["run_id"] if acct else -1}
        with self.db.read() as c:
            rows = c.execute(text(
                "SELECT id, ts, bar_date, type, severity, symbol, strategy, message FROM risk_events "
                "WHERE run_id = :r OR run_id IS NULL ORDER BY COALESCE(bar_date, substr(ts, 1, 10)) DESC, id DESC "
                "LIMIT :n"), params).fetchall()
        return [{"id": r[0], "ts": r[1], "day": r[2] or r[1][:10], "type": r[3], "severity": r[4], "symbol": r[5],
                 "strategy": r[6], "message": r[7]} for r in rows]

    # -- scoreboard ----------------------------------------------------------------------------
    def scoreboard(self) -> list[dict]:
        out = []
        first_start, last_day = None, None
        for a in self.accounts():
            with self.db.read() as c:
                eq_rows = c.execute(text("SELECT bar_date, equity FROM equity_snapshots WHERE run_id = :r ORDER BY bar_date"),
                                    {"r": a["run_id"]}).fetchall()
                tr = c.execute(text("SELECT pnl, r_multiple, fees, slippage FROM trades WHERE run_id = :r "
                                    "AND exit_ts IS NOT NULL"), {"r": a["run_id"]}).fetchall()
            if not eq_rows:
                continue
            eq = pd.Series([a["starting_equity"]] + [v for _, v in eq_rows],
                           index=[pd.Timestamp(eq_rows[0][0], tz="UTC") - pd.Timedelta(days=1)]
                           + [pd.Timestamp(d, tz="UTC") for d, _ in eq_rows])
            trades = [SimpleNamespace(pnl=p, r_multiple=r, fees=f, slippage=s) for p, r, f, s in tr]
            m = compute_metrics(eq, trades)
            out.append({"name": a["name"], "label": a["label"], "return": m.total_return, "max_dd": m.max_drawdown,
                        "sharpe": m.sharpe if len(eq_rows) >= 20 else None, "win_rate": m.win_rate, "trades": m.trades,
                        "days": len(eq_rows)})
            first_start = min(first_start or eq_rows[0][0], eq_rows[0][0])
            last_day = max(last_day or eq_rows[-1][0], eq_rows[-1][0])
        frames = self.frames()
        syms = [s for s in self.md.symbols() if s in frames]
        if out and syms:
            bh = buy_and_hold(frames, syms, first_start, last_day, CostModel.from_config(self.cfg.costs),
                              self.cfg.accounts.starting_equity)
            bh = pd.concat([pd.Series([self.cfg.accounts.starting_equity], index=[bh.index[0] - pd.Timedelta(days=1)]), bh])
            m = compute_metrics(bh, [])
            out.append({"name": "BH", "label": "Buy & Hold basket", "return": m.total_return, "max_dd": m.max_drawdown,
                        "sharpe": m.sharpe if len(bh) >= 21 else None, "win_rate": None, "trades": 0,
                        "days": len(bh) - 1})
        return out

    # -- alerts ------------------------------------------------------------------------------
    def alerts_summary(self) -> dict:
        with self.db.read() as c:
            rows = dict(c.execute(text("SELECT status, COUNT(*) FROM alerts GROUP BY status")).fetchall())
            last = c.execute(text("SELECT created_at, severity, subject, status, error FROM alerts "
                                  "ORDER BY id DESC LIMIT 5")).fetchall()
        return {"counts": rows, "recent": [dict(zip(("at", "severity", "subject", "status", "error"), r)) for r in last],
                "channel": self.cfg.alerts.channel}

    # -- research ------------------------------------------------------------------------------
    def reports(self) -> list[dict]:
        out = []
        for p in sorted(self.paths.reports.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True):
            head = p.read_text(errors="replace")[:4000]
            m = re.search(r"<title>(.*?)</title>", head)
            kind = "Research" if p.name.startswith("research_") else "Backtest"
            title = m.group(1) if m else p.stem
            if kind == "Research":  # e.g. research_S1-S2-S3_20260927T213812.html -> "S1, S2, S3 walk-forward"
                title = ", ".join(p.name.split("_")[1].split("-")) + " walk-forward (all coins)"
            out.append({"name": p.name, "title": title, "kind": kind,
                        "synthetic": "SYNTHETIC DATA" in p.read_text(errors="replace")[:20000],
                        "zero_cost": "_ZEROCOST" in p.name,
                        "modified": datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="minutes"),
                        "size_kb": p.stat().st_size // 1024})
        return out
