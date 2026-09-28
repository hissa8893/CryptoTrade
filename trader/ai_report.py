"""Is the AI analyst helping? Compares the AI-filtered account with its rules-only shadow twin
(same start day, same money, same signals) and says honestly how much the numbers can tell.

Used by the dashboard's "AI analyst" card and `trader llm report`."""

from __future__ import annotations

import json
import math
from collections import Counter

import pandas as pd
from sqlalchemy import text

from trader.metrics import drawdown_stats

MIN_DAYS = 30  # before this, any difference is noise and we say so


def _series(c, run_id: int, start_eq: float) -> pd.Series:
    rows = c.execute(text("SELECT bar_date, equity FROM equity_snapshots WHERE run_id = :r ORDER BY bar_date"),
                     {"r": run_id}).fetchall()
    return pd.Series([start_eq] + [e for _, e in rows], index=["start"] + [d for d, _ in rows], dtype=float)


def verdict_text(n_days: int, t: float | None, years_needed: float | None) -> tuple[str, str]:
    """(level, plain-English verdict) for the paired daily-return test."""
    if n_days < MIN_DAYS or t is None:
        return "info", (f"Too early to judge: {n_days} day(s) of data. Differences this early are noise; "
                        f"the comparison becomes meaningful after months, not days.")
    if abs(t) < 2:
        wait = (f" At the current pace it would take roughly {years_needed:.0f} more year(s) of data to tell."
                if years_needed and years_needed < 100 else " The two accounts are too close to ever tell apart.")
        return "info", (f"No evidence either way yet (t = {t:+.2f} over {n_days} days; |t| needs to reach about 2)."
                        + wait + " Daily trend strategies trade rarely, so this measurement is slow by nature.")
    if t >= 2:
        return "ok", (f"The AI-filtered account is ahead by more than chance usually explains (t = {t:+.2f} over "
                      f"{n_days} days). Keep watching: a lucky stretch can do this, and the AI's cost is not in "
                      "its equity.")
    return "bad", (f"The AI-filtered account is behind by more than chance usually explains (t = {t:+.2f} over "
                   f"{n_days} days): the AI is probably hurting. Consider switching it off (llm.enabled: false).")


def ai_summary(db, source: str) -> dict | None:
    ai_key, sh_key = f"paper:{source}:AI", f"paper:{source}:AI_SHADOW"
    with db.read() as c:
        runs = {k: (i, st, eq) for i, k, st, eq in c.execute(text(
            "SELECT id, run_key, start, starting_equity FROM runs WHERE run_key IN (:a, :s)"),
            {"a": ai_key, "s": sh_key}).fetchall()}
        if ai_key not in runs or sh_key not in runs:
            return None
        (ai_id, start, eq0), (sh_id, _, sh_eq0) = runs[ai_key], runs[sh_key]
        ai, sh = _series(c, ai_id, eq0), _series(c, sh_id, sh_eq0)
        n_closed = {rid: c.execute(text("SELECT COUNT(*) FROM trades WHERE run_id = :r AND exit_ts IS NOT NULL"),
                                   {"r": rid}).scalar() for rid in (ai_id, sh_id)}
        decisions = c.execute(text(
            "SELECT s.bar_date, s.strategy, s.symbol, d.llm_json FROM decisions d JOIN signals s ON s.id = d.signal_id "
            "WHERE s.run_id = :r AND d.llm_json IS NOT NULL ORDER BY s.bar_date"), {"r": ai_id}).fetchall()
        shadow_trades = {(d, st, sym): (pnl, xt) for d, st, sym, pnl, xt in c.execute(text(
            "SELECT s.bar_date, t.strategy, t.symbol, t.pnl, t.exit_ts FROM trades t JOIN signals s "
            "ON s.id = t.entry_signal_id WHERE t.run_id = :r"), {"r": sh_id}).fetchall()}
        calls = c.execute(text(
            "SELECT COUNT(*), SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END), COALESCE(SUM(cost_usd), 0), "
            "SUM(input_tokens), SUM(output_tokens), MAX(created_at) FROM ai_reviews WHERE run_key = :k"),
            {"k": ai_key}).fetchone()
        served = [m for (m,) in c.execute(text(
            "SELECT DISTINCT COALESCE(served_model, model) FROM ai_reviews WHERE run_key = :k AND status = 'ok'"),
            {"k": ai_key}).fetchall()]

    verdicts, fallbacks = Counter(), Counter()
    vetoed = {"n": 0, "closed": 0, "shadow_pnl": 0.0, "not_comparable": 0}
    reduced = {"n": 0, "closed": 0, "saved": 0.0}
    for day, strat, sym, lj in decisions:
        llm = json.loads(lj)
        if llm.get("status") == "ok":
            verdicts[llm["decision"]] += 1
        else:
            fallbacks[llm.get("fallback_reason") or "unknown"] += 1
            continue
        m = float(llm.get("applied_multiplier", llm.get("multiplier", 1.0)))
        twin = shadow_trades.get((day, strat, sym))
        if m <= 0:
            vetoed["n"] += 1
            if twin is None:
                vetoed["not_comparable"] += 1  # still open in the shadow, or the shadow did not take it
            elif twin[1] is not None:
                vetoed["closed"] += 1
                vetoed["shadow_pnl"] += twin[0]
        elif m < 1:
            reduced["n"] += 1
            if twin is not None and twin[1] is not None:
                reduced["closed"] += 1
                reduced["saved"] += -(1 - m) * twin[0]  # approx: the part not bought would have made/lost this

    common = [d for d in ai.index if d in sh.index and d != "start"]
    ra, rs = ai.pct_change(), sh.pct_change()
    diff = (ra - rs).loc[common].dropna()
    n = len(diff)
    t = years = None
    if n >= 2 and diff.std(ddof=1) > 0:
        t = float(diff.mean() / (diff.std(ddof=1) / math.sqrt(n)))
        if diff.mean() != 0:
            years = float(max(0.0, (2 * diff.std(ddof=1) / abs(diff.mean())) ** 2 - n) / 365)
    level, text_ = verdict_text(n, t, years)
    ai_last, sh_last = float(ai.iloc[-1]), float(sh.iloc[-1])
    cost = float(calls[2] or 0.0)

    def stats(s: pd.Series, eq_start: float, closed: int) -> dict:
        idx = pd.to_datetime([d for d in s.index if d != "start"], utc=True)
        dd = drawdown_stats(pd.Series(s.iloc[1:].values, index=idx))[0] if len(s) > 1 else 0.0
        return {"equity": float(s.iloc[-1]), "return": float(s.iloc[-1]) / eq_start - 1, "max_dd": dd, "trades": closed}

    return {
        "start": start, "days": n, "ai": stats(ai, eq0, n_closed[ai_id]), "shadow": stats(sh, sh_eq0, n_closed[sh_id]),
        "difference": ai_last - sh_last, "cost_usd": cost, "net_difference": ai_last - sh_last - cost,
        "verdicts": dict(verdicts), "fallbacks": dict(fallbacks), "vetoed": vetoed, "reduced": reduced,
        "calls": int(calls[0] or 0), "calls_ok": int(calls[1] or 0), "input_tokens": int(calls[3] or 0),
        "output_tokens": int(calls[4] or 0), "last_call": calls[5], "served_models": served,
        "t_stat": t, "years_needed": years, "level": level, "verdict": text_,
    }


def summary_lines(s: dict) -> list[str]:
    """Plain-text version for the CLI."""
    v, f = s["verdicts"], s["fallbacks"]
    lines = [
        f"AI analyst since {s['start']} ({s['days']} days) - AI-filtered vs its rules-only shadow twin:",
        f"  AI-filtered   equity ${s['ai']['equity']:,.2f}  return {s['ai']['return']:+.2%}  max DD -{s['ai']['max_dd']:.2%}"
        f"  closed trades {s['ai']['trades']}",
        f"  rules shadow  equity ${s['shadow']['equity']:,.2f}  return {s['shadow']['return']:+.2%}  max DD "
        f"-{s['shadow']['max_dd']:.2%}  closed trades {s['shadow']['trades']}",
        f"  difference {s['difference']:+,.2f} USD; AI cost so far ${s['cost_usd']:,.2f}; net {s['net_difference']:+,.2f} USD",
        f"  verdicts: {v.get('approve', 0)} approve, {v.get('reduce', 0)} reduce, {v.get('veto', 0)} veto"
        + (f"; rules decided alone {sum(f.values())}x ({', '.join(f'{k} {n}' for k, n in f.items())})" if f else ""),
        f"  API calls {s['calls']} ({s['calls_ok']} answered), tokens in/out {s['input_tokens']:,}/{s['output_tokens']:,}",
    ]
    ve = s["vetoed"]
    if ve["n"]:
        lines.append(f"  vetoed entries: {ve['n']}; the shadow took and has closed {ve['closed']} of them, for "
                     f"{ve['shadow_pnl']:+,.2f} USD (negative = the vetoes avoided losses). The other "
                     f"{ve['not_comparable']} are still open there, or repeat a signal the shadow was already holding.")
    lines.append("  " + s["verdict"])
    return lines
