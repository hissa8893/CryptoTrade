"""Look-ahead proof: the decision at bar t must be identical whether the engine sees
data only up to t or the full history.

For each sampled day t we re-run the whole engine on every series truncated at t
(indicators recomputed on the truncated data) and compare, exactly:
  * the signals, risk decisions and orders created at the close of t, and
  * the complete engine state after t (cash, positions, stops, risk state).
Any difference means some code path peeked at data after t.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field

import pandas as pd

from trader.backtest import make_engine
from trader.broker import CostModel
from trader.config import AppConfig


@dataclass
class LookaheadReport:
    dates_checked: int = 0
    dates_with_signals: int = 0
    signals_compared: int = 0
    orders_compared: int = 0
    mismatches: list[dict] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.dates_checked > 0 and not self.mismatches


def _canon(x) -> str:
    return json.dumps(x, sort_keys=True, default=float)


def lookahead_proof(
    cfg: AppConfig,
    frames: dict[str, pd.DataFrame],
    strategy_names: list[str],
    *,
    symbols: list[str],
    samples: int = 250,
    seed: int = 7,
    costs: CostModel | None = None,
) -> LookaheadReport:
    full, frames, start, end = make_engine(cfg, frames, strategy_names, symbols=symbols, costs=costs)
    states: dict[str, str] = {}
    full.run(after_step=lambda d, e: states.__setitem__(d, _canon(e.state_dict())))
    days = list(states)
    signal_days = sorted({s["bar_date"] for s in full.journal.signals})
    rng = random.Random(seed)
    # half the sample from days where something was decided (the interesting ones), half uniform
    picked = set(rng.sample(signal_days, min(len(signal_days), samples // 2)))
    rest = [d for d in days if d not in picked]
    picked |= set(rng.sample(rest, min(len(rest), samples - len(picked))))
    rep = LookaheadReport()
    for t in sorted(picked):
        trunc, _, _, _ = make_engine(cfg, {s: df.loc[:t] for s, df in frames.items()}, strategy_names,
                                     symbols=symbols, start=start, costs=costs)
        trunc.run()
        a, b = full.journal.for_date(t), trunc.journal.for_date(t)
        rep.dates_checked += 1
        rep.dates_with_signals += bool(a["signals"])
        rep.signals_compared += len(a["signals"])
        rep.orders_compared += len(a["orders"])
        if _canon(a) != _canon(b):
            rep.mismatches.append({"date": t, "kind": "decisions", "full": a, "truncated": b})
        elif states[t] != _canon(trunc.state_dict()):
            rep.mismatches.append({"date": t, "kind": "state", "full": json.loads(states[t]),
                                   "truncated": trunc.state_dict()})
    return rep
