"""HTML research report: walk-forward (out-of-sample headline), sensitivity heatmaps,
Monte Carlo drawdowns, regime split, and a scoreboard vs Buy & Hold."""

from __future__ import annotations

import html
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from trader.metrics import Metrics
from trader.reports import CSS, JS, _cls, _line_chart, _money, _num, _pct
from trader.research import MonteCarlo, ResearchResult, Sensitivity
from trader.timeutil import now_iso

EXTRA_CSS = """
.heat td{text-align:center;font-family:var(--mono);min-width:64px;border:2px solid var(--surface-1);border-radius:4px}
.heat th{text-align:center}.heat .def{outline:2px solid var(--text-primary);outline-offset:-3px}
.note{font-size:13px;color:var(--text-secondary);margin:6px 0}
.flagnote{font-weight:400;font-size:13px;margin-top:4px}
nav a{color:var(--text-secondary);margin-right:14px;font-size:14px}
.score th{white-space:normal;vertical-align:bottom;min-width:70px}.score td,.score th{padding:5px 6px}
"""

FLAG_EXPLAIN = {  # most specific first: the out-of-sample flag also contains the word "Sharpe"
    "Out-of-sample": "Parameters that looked best in-sample did not hold up on unseen data: the in-sample choice was fitting noise. "
                     "Prefer the default parameters, and trust only the out-of-sample column.",
    "< 30": "Too few trades means the statistics are dominated by luck. Treat every number as provisional.",
    "Profit factor": "Real trend-following systems rarely exceed 2-3. Check costs are on, fills are next-open, and the look-ahead proof passes.",
    "Sharpe": "Crypto trend systems usually land between 0 and 1.5 out-of-sample. Re-run the look-ahead proof and compare zero-cost vs with-cost.",
}


def _explain(flag: str) -> str:
    for k, v in FLAG_EXPLAIN.items():
        if k in flag:
            return v
    return ""


def _mix(a: str, b: str, t: float) -> str:
    a_, b_ = [int(a[i:i + 2], 16) for i in (1, 3, 5)], [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a_, b_))


def _heatmap(s: Sensitivity) -> str:
    """Diverging blue (Sharpe > 0) <-> red (< 0) through a neutral gray at 0; values printed in every cell."""
    vals = [v for row in s.sharpe for v in row if v is not None]
    vmax = max([abs(v) for v in vals] + [0.25])
    mid, pos, neg = "#f0efec", "#256abf", "#e34948"
    out = [f'<table class="heat"><tr><th>{html.escape(s.y_name)} ↓ / {html.escape(s.x_name)} →</th>'
           + "".join(f"<th>{x}</th>" for x in s.x_values) + "</tr>"]
    for y, row in zip(s.y_values, s.sharpe):
        out.append(f"<tr><th>{y}</th>")
        for x, v in zip(s.x_values, row):
            is_def = (x, y) == s.default
            if v is None:
                out.append(f'<td title="invalid combination">—</td>')
                continue
            t = min(1.0, abs(v) / vmax)
            bg = _mix(mid, pos if v >= 0 else neg, t)
            ink = "#ffffff" if t > 0.55 else "#0b0b0b"
            out.append(f'<td class="{"def" if is_def else ""}" style="background:{bg};color:{ink}" '
                       f'title="{s.x_name}={x}, {s.y_name}={y}: Sharpe {v:.2f}">{v:+.2f}</td>')
        out.append("</tr>")
    out.append("</table>")
    return ("".join(out) + f'<div class="note">Full-history Sharpe for each parameter pair (in-sample by nature). '
            "Outlined cell = configured default. A robust strategy shows a broad plateau of similar values, "
            "not a single bright cell surrounded by poor ones.</div>")


def _histogram(mc: MonteCarlo) -> str:
    W, H, pl, pb = 960, 200, 50, 30
    bins = np.linspace(0, max(mc.dd_samples) * 1.05 or 0.01, 26)
    counts, edges = np.histogram(mc.dd_samples, bins=bins)
    cmax = counts.max() or 1
    bw = (W - pl - 20) / len(counts)
    xs = lambda v: pl + (v - edges[0]) / (edges[-1] - edges[0]) * (W - pl - 20)
    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="Monte Carlo max drawdown distribution">']
    for i, c in enumerate(counts):
        h = (H - pb - 20) * c / cmax
        x = pl + i * bw
        parts.append(f'<rect x="{x + 1:.1f}" y="{H - pb - h:.1f}" width="{max(bw - 2, 1):.1f}" height="{h:.1f}" rx="2" '
                     f'fill="var(--series-1)"><title>{edges[i] * 100:.1f}–{edges[i + 1] * 100:.1f}%: {c} runs</title></rect>')
    for v, label in ((mc.actual_max_dd, "actual"), (mc.p95_max_dd, "worst 5%")):
        x = xs(v)
        parts.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="14" y2="{H - pb}" stroke="var(--text-primary)" stroke-dasharray="4 3"/>'
                     f'<text x="{x + 4:.1f}" y="24" style="fill:var(--text-primary)">{label} {v * 100:.1f}%</text>')
    for k in range(0, 101, 10):
        v = edges[0] + (edges[-1] - edges[0]) * k / 100
        parts.append(f'<text x="{xs(v):.1f}" y="{H - 10}" text-anchor="middle">{v * 100:.0f}%</text>')
    parts.append("</svg>")
    return ('<div class="chartwrap"><div class="chart">' + "".join(parts) + "</div></div>"
            + f'<div class="note">{mc.runs:,} random orderings of the same {mc.trades} out-of-sample trades. '
              f"Median max drawdown {mc.median_max_dd * 100:.1f}%; in 5% of orderings it was worse than "
              f"<b>{mc.p95_max_dd * 100:.1f}%</b> (worst seen {mc.worst_max_dd * 100:.1f}%). "
              f"The actual order produced {mc.actual_max_dd * 100:.1f}%. Plan for the worst-5% figure, not the actual one. "
              "(These are closed-trade drawdowns; the equity curve's drawdown above also counts losses on trades still open, "
              "so it can be deeper.)</div>")


def _regimes(rows: list[dict]) -> str:
    if not rows:
        return '<div class="note">No regime data.</div>'
    out = ['<table><tr><th>BTC regime</th><th>Days</th><th>Strategy return</th><th>Buy &amp; Hold return</th><th>Strategy Sharpe</th></tr>']
    for r in rows:
        out.append(f"<tr><td>{r['regime']}</td><td>{r['days']:,}</td><td class=\"{_cls(r['strategy_return'])}\">{_pct(r['strategy_return'])}</td>"
                   f"<td class=\"{_cls(r['benchmark_return'])}\">{_pct(r['benchmark_return'])}</td><td>{_num(r['strategy_sharpe'])}</td></tr>")
    out.append("</table><div class=\"note\">Bull = BTC above a rising 200-day average; bear = below a falling one; "
               "sideways = everything else. Returns compound only the days in each regime (out-of-sample).</div>")
    return "".join(out)


def _flags(flags: list[str]) -> str:
    if not flags:
        return ""
    return ('<div class="banner bad">Red flags:<ul class="flags">'
            + "".join(f"<li>{html.escape(f)}<div class=\"flagnote\">{html.escape(_explain(f))}</div></li>" for f in flags)
            + "</ul></div>")


ROWS = [("OOS total return", "total_return", "pct"), ("CAGR", "cagr", "pct"), ("Max drawdown", "max_drawdown", "dd"),
        ("Sharpe", "sharpe", "num"), ("Sortino", "sortino", "num"), ("Calmar", "calmar", "num"),
        ("Trades", "trades", "int"), ("Win rate", "win_rate", "abs"), ("Profit factor", "profit_factor", "num"),
        ("Average R", "avg_r", "num"), ("Fees", "fees", "money")]


def _cell(m: Metrics, key: str, kind: str) -> str:
    v = getattr(m, key)
    if v is None:
        return "—"
    return {"pct": lambda: f'<span class="{_cls(v)}">{_pct(v)}</span>', "dd": lambda: f"−{v * 100:.2f}%",
            "num": lambda: _num(v), "int": lambda: f"{v:,}", "abs": lambda: f"{v * 100:.1f}%",
            "money": lambda: _money(v)}[kind]()


def render_research_report(r: ResearchResult) -> str:
    synthetic = r.data_source == "synthetic"
    cols: list[tuple[str, Metrics]] = [(n, s.wf.oos_metrics) for n, s in r.strategies.items()]
    if r.combined:
        cols.append(("Combined " + "+".join(r.combined.strategies), r.combined.metrics))
    for n, m in r.defaults.items():
        cols.append((f"{'Combined ' if '+' in n else ''}{n} fixed defaults", m))
    cols.append(("Buy & Hold basket", r.benchmark_metrics))
    first = next(iter(r.strategies.values())).wf
    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
           "<title>Research report</title>",
           "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'\">",
           f"<style>{CSS}{EXTRA_CSS}</style></head><body><main class=\"viz-root\">",
           f"<h1>Research — {', '.join(r.strategies)} on {', '.join(r.symbols)}</h1>",
           f'<div class="sub">Walk-forward: 2-year in-sample / 6-month out-of-sample windows, {len(first.windows)} windows, '
           f"out-of-sample {first.windows[0].oos_start} → {first.windows[-1].oos_end} · data: {html.escape(r.data_source)} · "
           f"generated {now_iso()} UTC</div>",
           '<div class="banner warn">SIMULATION — paper trading only. Backtest results do not predict future returns. '
           "Every headline number below is OUT-OF-SAMPLE: chosen parameters were never tested on the months they are scored on.</div>"]
    if synthetic:
        out.append('<div class="banner bad">SYNTHETIC DATA — randomly generated prices, not market history. A random walk has no '
                   "edge to find: these numbers test the machinery, not the strategies. Do not tune anything to them.</div>")
    zc = r.zero_cost_check
    if zc:
        out.append(f'<div class="note">Cost sanity check ({zc["strategy"]}, default parameters, same span): zero-cost ending equity '
                   f'{_money(zc["zero_costs"])} vs with-cost {_money(zc["with_costs"])} — '
                   f'{"✅ zero-cost is higher, as it must be" if zc["ok"] else "❌ zero-cost is NOT higher: investigate"}.</div>')
    out.append("<nav>" + "".join(f'<a href="#{n}">{n}</a>' for n in r.strategies)
               + ('<a href="#combined">Combined</a>' if r.combined else "") + "</nav>")
    out.append('<h2>Scoreboard (out-of-sample)</h2><div class="card scroll"><table class="score"><tr><th>Metric</th>'
               + "".join(f"<th>{html.escape(n)}</th>" for n, _ in cols) + "</tr>")
    for label, key, kind in ROWS:
        out.append(f"<tr><td>{label}</td>" + "".join(f"<td>{_cell(m, key, kind)}</td>" for _, m in cols) + "</tr>")
    out.append("</table></div>")
    if r.defaults:
        out.append('<div class="note">"Fixed defaults" = the configured parameters, never tuned to any data, run over the same '
                   "out-of-sample span. If they beat the walk-forward columns, re-optimising is fitting noise: keep the "
                   "defaults (and never change them to chase these numbers, or the comparison stops being fair).</div>")

    dates_all = [d.date().isoformat() for d in r.benchmark_equity.index]
    for name, s in r.strategies.items():
        wf = s.wf
        out.append(f'<h2 id="{name}">{name} — walk-forward</h2>')
        out.append(_flags(wf.flags))
        eq = wf.oos_equity.reindex(r.benchmark_equity.index).ffill()
        series = [{"name": f"{name} out-of-sample", "short": name, "color": "var(--series-1)", "values": list(eq.values)},
                  {"name": "Buy & Hold basket", "short": "Buy & Hold", "color": "var(--series-2)",
                   "values": list(r.benchmark_equity.values)}]
        out.append('<div class="card">' + _line_chart(dates_all, series, height=300, log=True, fmt="money",
                                                      title=f"{name} out-of-sample equity") + "</div>")
        out.append(f'<div class="note">Mean in-sample Sharpe of the chosen parameters: {_num(wf.is_sharpe_mean)} · '
                   f"out-of-sample Sharpe: {_num(wf.oos_metrics.sharpe)}.</div>")
        out.append('<div class="card scroll"><table><tr><th>Out-of-sample window</th><th>Chosen parameters (from the prior 2 years)</th>'
                   "<th>In-sample Sharpe</th><th>OOS return</th><th>OOS Sharpe</th><th>OOS trades</th></tr>")
        for w in wf.windows:
            p = ", ".join(f"{k}={v}" for k, v in w.params.items())
            out.append(f"<tr><td>{w.oos_start} → {w.oos_end}</td><td>{html.escape(p)}</td><td>{_num(w.is_sharpe)}</td>"
                       f"<td class=\"{_cls(w.oos_return)}\">{_pct(w.oos_return)}</td><td>{_num(w.oos_sharpe)}</td><td>{w.oos_trades}</td></tr>")
        out.append("</table></div>")
        if s.sens:
            out.append(f"<h2>{name} — parameter sensitivity</h2><div class=\"card scroll\">{_heatmap(s.sens)}</div>")
        out.append(f"<h2>{name} — Monte Carlo drawdown</h2><div class=\"card\">"
                   + (_histogram(s.mc) if s.mc else '<div class="note">Too few trades for Monte Carlo.</div>') + "</div>")
        out.append(f"<h2>{name} — by market regime</h2><div class=\"card scroll\">{_regimes(s.regimes)}</div>")

    if r.combined:
        c = r.combined
        out.append('<h2 id="combined">Combined portfolio (one shared account)</h2>')
        out.append(_flags(c.flags))
        eq = c.oos_equity.reindex(r.benchmark_equity.index).ffill()
        series = [{"name": "Combined out-of-sample", "short": "Combined", "color": "var(--series-1)", "values": list(eq.values)},
                  {"name": "Buy & Hold basket", "short": "Buy & Hold", "color": "var(--series-2)",
                   "values": list(r.benchmark_equity.values)}]
        out.append('<div class="card">' + _line_chart(dates_all, series, height=300, log=True, fmt="money",
                                                      title="Combined out-of-sample equity") + "</div>")
        out.append('<div class="card">' + (_histogram(c.mc) if c.mc else "") + "</div>")
        out.append(f'<div class="card scroll">{_regimes(c.regimes)}</div>')
    out.append('<div class="foot">Each strategy runs as its own $10,000 sub-account; the combined portfolio shares one account '
               "and one risk engine across all strategies. Costs: 0.10% fee per side plus slippage (0.05% BTC/ETH, 0.15% others). "
               "This is a simulation; backtest results do not predict future returns.</div>")
    out.append(f"<script>{JS}</script></main></body></html>")
    return "".join(out)


def write_research_report(r: ResearchResult, reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_iso()[:19].replace(":", "").replace("-", "")
    p = reports_dir / f"research_{'-'.join(r.strategies)}_{stamp}.html"
    p.write_text(render_research_report(r), encoding="utf-8")
    return p
