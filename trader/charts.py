"""Server-rendered inline-SVG charts shared by HTML reports and the dashboard.

No chart library and no network: the SVG is built in Python, and a small script
(reports inline / dashboard static JS) adds the hover crosshair from `data-series`.
Series options: color (CSS color/var), dash (dashed line), fill (area down to zero).
"""

from __future__ import annotations

import html
import json
import math
from datetime import date

import numpy as np

W = 960
PAD_L, PAD_R, PAD_T, PAD_B = 64, 150, 14, 26


def nice_step(raw: float) -> float:
    """Round a raw tick step up to 1, 2, 2.5 or 5 x 10^k."""
    e = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * e:
            return m * e
    return 10 * e


def nice_log_ticks(lo: float, hi: float) -> list[float]:
    out = []
    for e in range(int(math.floor(math.log10(lo))) - 1, int(math.ceil(math.log10(hi))) + 1):
        for m in (1, 2, 5):
            v = m * 10**e
            if lo <= v <= hi:
                out.append(v)
    return out


def _x_labels(dates: list[str]) -> list[tuple[int, str]]:
    """Year labels for long spans; a few 'Sep 21' labels for short ones."""
    if not dates:
        return []
    span = (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days
    if span > 400:
        years, out = sorted({d[:4] for d in dates}), []
        for yr in years:
            i = next(k for k, d in enumerate(dates) if d[:4] == yr)
            if i == 0 and len(years) > 1 and dates[0][5:] != "01-01":
                continue
            out.append((i, yr))
        return out
    k = min(5, len(dates))
    idx = sorted({round(j * (len(dates) - 1) / max(1, k - 1)) for j in range(k)})
    return [(i, date.fromisoformat(dates[i]).strftime("%b %d").replace(" 0", " ")) for i in idx]


def _fmt_axis(t: float, fmt: str) -> str:
    if fmt == "pct":
        return f"{t * 100:.0f}%"
    if abs(t) >= 1000:
        return f"${t / 1000:,.0f}k" if abs(t) >= 10_000 or t % 1000 == 0 else f"${t / 1000:,.1f}k"
    return f"${t:,.0f}"


def line_chart(dates: list[str], series: list[dict], *, height: int, log: bool, fmt: str, title: str,
               legend: bool = True) -> str:
    n = len(dates)
    x0, x1 = PAD_L, W - PAD_R
    y0, y1 = PAD_T, height - PAD_B
    allv = np.concatenate([np.asarray(s["values"], float) for s in series])
    allv = allv[np.isfinite(allv)]
    if log:
        lo, hi = max(allv.min(), 1e-9), allv.max()
        if hi / lo < 1.02:  # nearly flat: pad so the line sits mid-chart
            lo, hi = lo * 0.99, hi * 1.01
        f = lambda v: math.log10(max(v, 1e-9))
        a, b = f(lo) - 0.02, f(hi) + 0.02
        ticks = nice_log_ticks(lo, hi)
        if len(ticks) < 2:  # narrow range: fall back to evenly spaced linear-ish ticks
            step = nice_step((hi - lo) / 3)
            ticks = [math.ceil(lo / step) * step + k * step for k in range(4) if math.ceil(lo / step) * step + k * step <= hi]
    else:
        lo, hi = float(allv.min()), float(allv.max())
        if fmt == "pct":  # drawdowns/returns read against zero; money zooms to the data
            lo, hi = min(lo, 0.0), max(hi, 0.0)
        f = lambda v: v
        step = nice_step((hi - lo) / 4 if hi > lo else 0.01)
        lo, hi = math.floor(lo / step) * step, math.ceil(hi / step) * step
        if hi == lo:
            lo -= step
        a, b = lo - (hi - lo) * 0.03, hi + (hi - lo) * 0.03
        ticks = [lo + k * step for k in range(int(round((hi - lo) / step)) + 1)]
    ys = lambda v: y1 - (f(v) - a) / (b - a) * (y1 - y0)
    xs = lambda i: x0 + (x1 - x0) * i / max(1, n - 1) if n > 1 else (x0 + x1) / 2
    parts = [f'<svg viewBox="0 0 {W} {height}" width="100%" role="img" aria-label="{html.escape(title)}">']
    for t in ticks:
        y = ys(t)
        parts.append(f'<line x1="{x0}" x2="{x1}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--grid)"/>'
                     f'<text x="{x0 - 6}" y="{y + 4:.1f}" text-anchor="end">{_fmt_axis(t, fmt)}</text>')
    for i, lab in _x_labels(dates):
        parts.append(f'<text x="{xs(i):.1f}" y="{height - 8}" text-anchor="middle">{lab}</text>')
    for s in series:
        vals = s["values"]
        pts = [(xs(i), ys(v)) for i, v in enumerate(vals)]
        dash = ' stroke-dasharray="6 4"' if s.get("dash") else ""
        if len(pts) == 1:
            parts.append(f'<circle cx="{pts[0][0]:.1f}" cy="{pts[0][1]:.1f}" r="4" fill="{s["color"]}"/>')
            continue
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        if s.get("fill"):
            base = ys(0.0) if not log else y1
            parts.append(f'<path d="{d} L{pts[-1][0]:.1f},{base:.1f} L{pts[0][0]:.1f},{base:.1f} Z" '
                         f'fill="{s["color"]}" fill-opacity="0.18" stroke="none"/>')
        parts.append(f'<path d="{d}" fill="none" stroke="{s["color"]}" stroke-width="2" stroke-linejoin="round"{dash}/>')
    # direct labels at the right end (nudged apart if they collide)
    ends = sorted(((ys(s["values"][-1]), s) for s in series), key=lambda t: t[0])
    last_y = -1e9
    for y, s in ends:
        y = max(y, last_y + 30)
        last_y = y
        v = s["values"][-1]
        val = f"{v * 100:.1f}%" if fmt == "pct" else f"${v:,.0f}"
        parts.append(f'<text class="lbl" x="{x1 + 6}" y="{y + 4:.1f}" style="fill:var(--text-primary)">{val}</text>'
                     f'<text x="{x1 + 6}" y="{y + 18:.1f}">{html.escape(s["short"])}</text>')
    parts.append(f'<line class="hair" x1="0" x2="0" y1="{y0}" y2="{y1}" stroke="var(--text-muted)" style="display:none"/>')
    parts.append(f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" fill="transparent"/></svg>')
    data = {"dates": dates, "w": W, "x0": x0, "x1": x1, "series": [
        {"name": s["name"], "color": s["color"], "fmt": fmt, "values": [round(float(v), 6) for v in s["values"]]}
        for s in series]}
    leg = ""
    if legend:
        leg = '<div class="legend">' + "".join(
            f'<span><i class="{"dash" if s.get("dash") else ""}" style="--key:{s["color"]}"></i>{html.escape(s["name"])}</span>'
            for s in series) + "</div>"
    return (f'{leg}<div class="chartwrap"><div class="chart" data-series=\'{html.escape(json.dumps(data))}\'>'
            + "".join(parts) + '<div class="tip" role="status"></div></div></div>')
