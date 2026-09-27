"""Self-contained HTML backtest reports (no CDN, no external requests; inline SVG charts)."""

from __future__ import annotations

import html
import json
import math
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from trader.backtest import BacktestResult
from trader.metrics import Metrics
from trader.timeutil import now_iso

W, H_EQ, H_DD = 960, 320, 150
PAD_L, PAD_R, PAD_T, PAD_B = 64, 150, 14, 26

CSS = """
.viz-root{color-scheme:light;--surface-0:#f4f4f2;--surface-1:#fcfcfb;--border:#e2e1dc;--text-primary:#0b0b0b;
--text-secondary:#52514e;--text-muted:#77766f;--grid:#ebeae6;--series-1:#2a78d6;--series-2:#eb6834;
--warn-bg:#fff4db;--warn-ink:#6b4a00;--bad-bg:#fde8e7;--bad-ink:#8a1c1b;--good-ink:#0b6b3a;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;
--surface-0:#121211;--surface-1:#1a1a19;--border:#2e2e2b;--text-primary:#fff;--text-secondary:#c3c2b7;--text-muted:#8f8e86;
--grid:#262624;--series-1:#3987e5;--series-2:#d95926;--warn-bg:#3a2e0c;--warn-ink:#f3cf73;--bad-bg:#3b1716;--bad-ink:#f3a3a2;
--good-ink:#6fd39a}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--surface-0:#121211;--surface-1:#1a1a19;--border:#2e2e2b;
--text-primary:#fff;--text-secondary:#c3c2b7;--text-muted:#8f8e86;--grid:#262624;--series-1:#3987e5;--series-2:#d95926;
--warn-bg:#3a2e0c;--warn-ink:#f3cf73;--bad-bg:#3b1716;--bad-ink:#f3a3a2;--good-ink:#6fd39a}
*{box-sizing:border-box}body{margin:0;background:var(--surface-0)}
.viz-root{font-family:var(--sans);color:var(--text-primary);background:var(--surface-0);padding:16px;max-width:1040px;margin:0 auto;line-height:1.45}
h1{font-size:20px;margin:4px 0 2px}h2{font-size:15px;margin:28px 0 8px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.04em}
.sub{color:var(--text-secondary);font-size:13px}.card{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;padding:12px 14px}
.banner{border-radius:8px;padding:10px 14px;margin:12px 0;font-weight:600}.banner.warn{background:var(--warn-bg);color:var(--warn-ink)}
.banner.bad{background:var(--bad-bg);color:var(--bad-ink)}.flags li{margin:4px 0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}
.tile .k{font-size:12px;color:var(--text-secondary)}.tile .v{font:600 24px var(--mono);font-variant-numeric:tabular-nums}
.tile .b{font:12px var(--mono);color:var(--text-muted)}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:5px 8px;border-bottom:1px solid var(--border);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{color:var(--text-secondary);font-weight:600}
td{font-family:var(--mono);font-variant-numeric:tabular-nums}.scroll{overflow-x:auto}
.pos{color:var(--good-ink)}.neg{color:var(--bad-ink)}
.legend{display:flex;gap:16px;font-size:13px;color:var(--text-secondary);margin:0 0 6px}.legend i{display:inline-block;width:18px;height:2px;vertical-align:middle;margin-right:6px}
svg text{font:13px var(--sans);fill:var(--text-muted)}svg .lbl{font-weight:600}
.chart{position:relative;min-width:700px}.chartwrap{overflow-x:auto}.tip{position:absolute;pointer-events:none;background:var(--surface-1);border:1px solid var(--border);
border-radius:6px;padding:6px 8px;font-size:12px;display:none;min-width:160px;box-shadow:0 2px 8px rgba(0,0,0,.12)}
.tip .d{color:var(--text-secondary);margin-bottom:2px}.tip .row{display:flex;justify-content:space-between;gap:10px}
.tip .row b{font-family:var(--mono)}.tip .row span i{display:inline-block;width:12px;height:2px;vertical-align:middle;margin-right:5px}
details summary{cursor:pointer;color:var(--text-secondary);font-size:13px}pre{font:12px var(--mono);white-space:pre-wrap}
.foot{margin-top:28px;font-size:12px;color:var(--text-muted)}
"""

JS = """
document.querySelectorAll('.chart[data-series]').forEach(function(box){
  var d=JSON.parse(box.dataset.series), svg=box.querySelector('svg'), tip=box.querySelector('.tip'),
      hair=svg.querySelector('.hair'), n=d.dates.length;
  function show(ev){
    var r=svg.getBoundingClientRect(), sx=(ev.clientX-r.left)*(d.w/r.width);
    var t=Math.min(1,Math.max(0,(sx-d.x0)/(d.x1-d.x0))), i=Math.round(t*(n-1)), x=d.x0+(d.x1-d.x0)*i/(n-1);
    hair.setAttribute('x1',x);hair.setAttribute('x2',x);hair.style.display='';
    tip.replaceChildren();var dd=document.createElement('div');dd.className='d';dd.textContent=d.dates[i];tip.appendChild(dd);
    d.series.forEach(function(s){var row=document.createElement('div');row.className='row';var sp=document.createElement('span');
      var k=document.createElement('i');k.style.background=s.color;sp.appendChild(k);sp.appendChild(document.createTextNode(s.name));
      var b=document.createElement('b');b.textContent=s.fmt==='pct'?(s.values[i]*100).toFixed(2)+'%':'$'+Math.round(s.values[i]).toLocaleString();
      row.appendChild(b);row.appendChild(sp);tip.appendChild(row);});
    tip.style.display='block';var px=(x/d.w)*r.width;tip.style.left=Math.min(px+12,r.width-tip.offsetWidth-4)+'px';tip.style.top='8px';}
  svg.addEventListener('pointermove',show);svg.addEventListener('pointerleave',function(){tip.style.display='none';hair.style.display='none';});
});
"""


def _pct(x, digits=2):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "—"
    return f"{x * 100:+.{digits}f}%".replace("-", "−")


def _num(x, digits=2):
    if x is None:
        return "—"
    if isinstance(x, float) and math.isinf(x):
        return "∞"
    return f"{x:,.{digits}f}"


def _money(x):
    if x is None:
        return "—"
    return f"−${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def _cls(x):
    return "" if x is None else ("pos" if x > 0 else "neg" if x < 0 else "")


def _nice_step(raw: float) -> float:
    """Round a raw tick step up to 1, 2, 2.5 or 5 x 10^k."""
    e = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * e:
            return m * e
    return 10 * e


def _nice_log_ticks(lo: float, hi: float) -> list[float]:
    out = []
    for e in range(int(math.floor(math.log10(lo))) - 1, int(math.ceil(math.log10(hi))) + 1):
        for m in (1, 2, 5):
            v = m * 10**e
            if lo <= v <= hi:
                out.append(v)
    return out


def _line_chart(dates: list[str], series: list[dict], *, height: int, log: bool, fmt: str, title: str) -> str:
    n = len(dates)
    x0, x1 = PAD_L, W - PAD_R
    y0, y1 = PAD_T, height - PAD_B
    allv = np.concatenate([np.asarray(s["values"], float) for s in series])
    if log:
        lo, hi = max(allv.min(), 1e-9), allv.max()
        f = lambda v: math.log10(max(v, 1e-9))
        a, b = f(lo) - 0.02, f(hi) + 0.02
        ticks = _nice_log_ticks(lo, hi) or [lo, hi]
    else:
        lo, hi = min(allv.min(), 0.0), max(allv.max(), 0.0)
        f = lambda v: v
        step = _nice_step((hi - lo) / 4 if hi > lo else 1.0)
        lo, hi = math.floor(lo / step) * step, math.ceil(hi / step) * step
        a, b = lo - (hi - lo) * 0.03, hi + (hi - lo) * 0.03 or 1
        ticks = [lo + k * step for k in range(int(round((hi - lo) / step)) + 1)]
    ys = lambda v: y1 - (f(v) - a) / (b - a) * (y1 - y0)
    xs = lambda i: x0 + (x1 - x0) * i / max(1, n - 1)
    parts = [f'<svg viewBox="0 0 {W} {height}" width="100%" role="img" aria-label="{html.escape(title)}">']
    for t in ticks:
        y = ys(t)
        lab = f"{t * 100:.0f}%" if fmt == "pct" else (f"${t / 1000:,.0f}k" if t >= 1000 else f"${t:,.0f}")
        parts.append(f'<line x1="{x0}" x2="{x1}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--grid)"/>'
                     f'<text x="{x0 - 6}" y="{y + 4:.1f}" text-anchor="end">{lab}</text>')
    years = sorted({d[:4] for d in dates})
    for yr in years:
        i = next(k for k, d in enumerate(dates) if d[:4] == yr)
        if i == 0 and len(years) > 1 and dates[0][5:] != "01-01":
            continue
        parts.append(f'<text x="{xs(i):.1f}" y="{height - 8}" text-anchor="middle">{yr}</text>')
    for s in series:
        vals = s["values"]
        d = "M" + " L".join(f"{xs(i):.1f},{ys(v):.1f}" for i, v in enumerate(vals))
        parts.append(f'<path d="{d}" fill="none" stroke="{s["color"]}" stroke-width="2" stroke-linejoin="round"/>')
    # direct labels at the right end (nudged apart if they collide)
    ends = sorted(((ys(s["values"][-1]), s) for s in series), key=lambda t: t[0])
    last_y = -1e9
    for y, s in ends:
        y = max(y, last_y + 14)
        last_y = y
        v = s["values"][-1]
        val = f"{v * 100:.1f}%" if fmt == "pct" else f"${v:,.0f}"
        parts.append(f'<text class="lbl" x="{x1 + 6}" y="{y + 4:.1f}" style="fill:var(--text-primary)">{val}</text>'
                     f'<text x="{x1 + 6}" y="{y + 16:.1f}">{html.escape(s["short"])}</text>')
    parts.append(f'<line class="hair" x1="0" x2="0" y1="{y0}" y2="{y1}" stroke="var(--text-muted)" style="display:none"/>')
    parts.append(f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" fill="transparent"/></svg>')
    data = {"dates": dates, "w": W, "x0": x0, "x1": x1, "series": [
        {"name": s["name"], "color": s["color"], "fmt": fmt, "values": [round(float(v), 6) for v in s["values"]]}
        for s in series]}
    legend = "".join(f'<span><i style="background:{s["color"]}"></i>{html.escape(s["name"])}</span>' for s in series)
    return (f'<div class="legend">{legend}</div><div class="chartwrap"><div class="chart" data-series=\'{html.escape(json.dumps(data))}\'>'
            + "".join(parts) + '<div class="tip"></div></div></div>')


METRIC_ROWS = [
    ("Total return", "total_return", "pct"), ("CAGR", "cagr", "pct"), ("Max drawdown", "max_drawdown", "pctneg"),
    ("Longest drawdown (days)", "max_dd_duration_days", "int"), ("Sharpe (365d)", "sharpe", "num"),
    ("Sortino (365d)", "sortino", "num"), ("Calmar", "calmar", "num"), ("Trades", "trades", "int"),
    ("Win rate", "win_rate", "pctabs"), ("Profit factor", "profit_factor", "num"), ("Average R", "avg_r", "num"),
    ("Expectancy per trade", "expectancy", "money"), ("Exposure (time in market)", "exposure", "pctabs"),
    ("Fees paid", "fees", "money"), ("Slippage cost", "slippage", "money"), ("End equity", "end_equity", "money"),
]


def _fmt_metric(m: Metrics, key: str, kind: str) -> str:
    v = getattr(m, key)
    if kind == "pct":
        return f'<span class="{_cls(v)}">{_pct(v)}</span>'
    if kind == "pctneg":
        return "—" if v is None else f"−{v * 100:.2f}%"
    if kind == "pctabs":
        return "—" if v is None else f"{v * 100:.1f}%"
    if kind == "int":
        return f"{v:,}"
    if kind == "money":
        return _money(v)
    return _num(v)


def render_backtest_report(res: BacktestResult, extra_sections: list[tuple[str, str]] | None = None) -> str:
    m = res.metrics
    bh = res.benchmarks[0] if res.benchmarks else None
    synthetic = res.data_source == "synthetic"
    zero_cost = res.costs.get("fee_rate") == 0
    dates = [d.date().isoformat() for d in res.equity.index]
    series = [{"name": f"Strategy ({'+'.join(res.strategies)})", "short": "+".join(res.strategies), "color": "var(--series-1)",
               "values": list(res.equity.values)}]
    if bh is not None:
        series.append({"name": bh.name, "short": "Buy & Hold", "color": "var(--series-2)",
                       "values": list(bh.equity.reindex(res.equity.index).ffill().values)})
    dd_series = [dict(s, values=list(-(1 - pd.Series(s["values"]) / pd.Series(s["values"]).cummax()))) for s in series]

    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
           f"<title>Backtest {html.escape(res.label)}</title>",
           "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'\">",
           f"<style>{CSS}</style></head><body><main class=\"viz-root\">"]
    out.append(f"<h1>Backtest — {html.escape(res.label)}</h1>")
    out.append(f'<div class="sub">{m.start} → {m.end} · {m.days:,} days · start equity {_money(m.start_equity)} · '
               f'data: {html.escape(res.data_source)} · generated {now_iso()} UTC</div>')
    out.append('<div class="banner warn">SIMULATION — paper trading only. Backtest results do not predict future returns.</div>')
    if synthetic:
        out.append('<div class="banner bad">SYNTHETIC DATA — these prices are randomly generated for testing, not real '
                   'market history. None of these numbers say anything about the strategy on real markets.</div>')
    if zero_cost:
        out.append('<div class="banner bad">ZERO-COST RUN — fees and slippage disabled for a sanity check only.</div>')
    if res.flags:
        out.append('<div class="banner bad">Red flags (investigate before trusting any number below):<ul class="flags">'
                   + "".join(f"<li>{html.escape(f)}</li>" for f in res.flags) + "</ul></div>")

    def tile(k, v, b):
        return f'<div class="card tile"><div class="k">{k}</div><div class="v">{v}</div><div class="b">{b}</div></div>'

    bm = bh.metrics if bh else None
    out.append('<h2>Headline</h2><div class="tiles">'
               + tile("Total return", f'<span class="{_cls(m.total_return)}">{_pct(m.total_return)}</span>', f"B&H {_pct(bm.total_return) if bm else '—'}")
               + tile("CAGR", _pct(m.cagr), f"B&H {_pct(bm.cagr) if bm else '—'}")
               + tile("Max drawdown", f"−{m.max_drawdown * 100:.2f}%", f"B&H −{bm.max_drawdown * 100:.2f}%" if bm else "")
               + tile("Sharpe", _num(m.sharpe), f"B&H {_num(bm.sharpe) if bm else '—'}")
               + tile("Trades", f"{m.trades}", f"win rate {_fmt_metric(m, 'win_rate', 'pctabs')}")
               + "</div>")
    out.append("<h2>Equity vs Buy &amp; Hold (log scale)</h2><div class=\"card\">"
               + _line_chart(dates, series, height=H_EQ, log=True, fmt="money", title="Equity curve") + "</div>")
    out.append("<h2>Drawdown from peak</h2><div class=\"card\">"
               + _line_chart(dates, [dict(s) for s in dd_series], height=H_DD, log=False, fmt="pct", title="Drawdown") + "</div>")
    cols = [("Strategy", m)] + [(b.name, b.metrics) for b in res.benchmarks]
    out.append('<h2>Metrics</h2><div class="card scroll"><table><tr><th>Metric</th>'
               + "".join(f"<th>{html.escape(n)}</th>" for n, _ in cols) + "</tr>")
    for label, key, kind in METRIC_ROWS:
        out.append(f"<tr><td>{label}</td>" + "".join(f"<td>{_fmt_metric(mm, key, kind)}</td>" for _, mm in cols) + "</tr>")
    out.append("</table></div>")
    if m.max_dd_peak:
        out.append(f'<div class="sub">Max drawdown ran from the peak on {m.max_dd_peak} to the trough on {m.max_dd_trough}.</div>')

    for title, body in extra_sections or []:
        out.append(f"<h2>{html.escape(title)}</h2><div class=\"card\">{body}</div>")

    ev = Counter(e.type for e in res.journal.events)
    out.append('<h2>Risk engine activity</h2><div class="card scroll"><table><tr><th>Event</th><th>Count</th></tr>'
               + "".join(f"<tr><td>{html.escape(k)}</td><td>{v:,}</td></tr>" for k, v in ev.most_common())
               + ("" if ev else "<tr><td>No risk events</td><td>0</td></tr>") + "</table></div>")

    trades = res.journal.trades
    out.append(f"<h2>Trades ({len(trades)})</h2>")
    if trades:
        out.append('<div class="card scroll"><table><tr><th>#</th><th>Symbol</th><th>Entry</th><th>Entry px</th><th>Qty</th>'
                   "<th>Initial stop</th><th>Exit</th><th>Exit px</th><th>Days</th><th>Fees</th><th>P&amp;L</th><th>P&amp;L %</th>"
                   "<th>R</th><th>Exit reason</th></tr>")
        for t in trades:
            out.append(f"<tr><td>{t.trade_no}</td><td>{html.escape(t.symbol)}</td><td>{t.entry_date}</td><td>{_num(t.entry_px)}</td>"
                       f"<td>{t.qty:.6f}</td><td>{_num(t.initial_stop)}</td><td>{t.exit_date}</td><td>{_num(t.exit_px)}</td>"
                       f"<td>{t.bars_held}</td><td>{_money(t.fees)}</td><td class=\"{_cls(t.pnl)}\">{'+' if t.pnl > 0 else '−' if t.pnl < 0 else ''}{_money(abs(t.pnl))}</td>"
                       f"<td class=\"{_cls(t.pnl_pct)}\">{_pct(t.pnl_pct)}</td><td>{_num(t.r_multiple)}</td><td>{html.escape(t.exit_reason)}</td></tr>")
        out.append("</table></div>")
    else:
        out.append('<div class="card sub">No trades were taken in this period.</div>')
    if res.open_positions:
        out.append(f'<div class="sub">{len(res.open_positions)} position(s) still open at the end (marked to market, not in the trade list).</div>')
    out.append("<h2>Settings</h2><div class=\"card\"><details><summary>Strategy, risk and cost parameters</summary><pre>"
               + html.escape(json.dumps(res.params | {"costs": res.costs}, indent=2, default=str)) + "</pre></details></div>")
    out.append('<div class="foot">Fills: decisions at the close of day t, filled at the open of day t+1 plus slippage; '
               "stops are resting orders (gap through the stop fills at the open). Sharpe/Sortino use 365-day annualization "
               "and a zero risk-free rate. This is a simulation; backtest results do not predict future returns.</div>")
    out.append(f"<script>{JS}</script></main></body></html>")
    return "".join(out)


def write_report(res: BacktestResult, reports_dir: Path, name: str | None = None,
                 extra_sections: list[tuple[str, str]] | None = None) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_iso()[:19].replace(":", "").replace("-", "")  # e.g. 20260927T210745 (UTC, to the second)
    tag = "_ZEROCOST" if res.costs.get("fee_rate") == 0 else ""
    fname = name or (f"backtest_{'-'.join(res.strategies)}_{'-'.join(s.split('/')[0] for s in res.symbols)}"
                     f"{tag}_{stamp}.html")
    if (reports_dir / fname).exists():  # never overwrite an earlier report
        fname = fname.replace(".html", f"_{len(list(reports_dir.glob('*.html')))}.html")
    p = reports_dir / fname
    p.write_text(render_backtest_report(res, extra_sections), encoding="utf-8")
    res.report_path = str(p)
    return p
