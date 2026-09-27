"""Indicators vs HAND-COMPUTED values on a small fixed dataset, plus a causality proof.

Fixture (O, H, L, C), bars numbered 1..9:
  1: 10  12    9    11        6: 14  14.5 12.5 13
  2: 11  13   10    12        7: 13  13.5 11   12
  3: 12  12.5 10.5  11        8: 12  16   12   15
  4: 11  14   11    13        9: 15  15.5  9    9.5   (only used for the Supertrend bearish flip)
  5: 13  15   12    14
"""

import math

import numpy as np
import pandas as pd
import pytest

from trader import indicators as ind

BARS = [
    (10, 12, 9, 11), (11, 13, 10, 12), (12, 12.5, 10.5, 11), (11, 14, 11, 13),
    (13, 15, 12, 14), (14, 14.5, 12.5, 13), (13, 13.5, 11, 12), (12, 16, 12, 15),
]


def frame(bars=BARS):
    idx = pd.date_range("2024-01-01", periods=len(bars), freq="D", tz="UTC")
    df = pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=idx, dtype="float64")
    return df


def approx_list(series, expected):
    got = series.tolist()
    assert len(got) == len(expected)
    for g, e in zip(got, expected):
        if e is None:
            assert math.isnan(g), f"expected NaN warm-up, got {g}"
        else:
            assert g == pytest.approx(e, rel=1e-12, abs=1e-12), (got, expected)


def test_true_range_by_hand():
    # TR1 = H-L = 3 (no prior close); TR2 = max(3, |13-11|, |10-11|) = 3; TR3 = max(2, .5, 1.5) = 2
    # TR4 = max(3, 3, 0) = 3; TR5 = max(3, 2, 1) = 3; TR6 = max(2, .5, 1.5) = 2
    # TR7 = max(2.5, .5, 2) = 2.5; TR8 = max(4, 4, 0) = 4
    df = frame()
    approx_list(ind.true_range(df.high, df.low, df.close), [3, 3, 2, 3, 3, 2, 2.5, 4])


def test_atr_wilder_by_hand():
    # ATR3 at bar3 = (3+3+2)/3 = 8/3 ; then ATR_t = (2*ATR_{t-1} + TR_t)/3
    # bar4 = (16/3+3)/3 = 25/9 ; bar5 = (50/9+3)/3 = 77/27 ; bar6 = (154/27+2)/3 = 208/81
    # bar7 = (416/81+2.5)/3 = 618.5/243 ; bar8 = (1237/243+4)/3 = 2209/729
    df = frame()
    approx_list(
        ind.atr(df.high, df.low, df.close, 3),
        [None, None, 8 / 3, 25 / 9, 77 / 27, 208 / 81, 618.5 / 243, 2209 / 729],
    )


def test_sma_by_hand():
    # closes 11 12 11 13 14 13 12 15
    df = frame()
    approx_list(ind.sma(df.close, 3), [None, None, 34 / 3, 36 / 3, 38 / 3, 40 / 3, 39 / 3, 40 / 3])


def test_donchian_excludes_current_bar():
    # upper at bar4 = max(H1..H3) = max(12,13,12.5) = 13 ; bar5 = max(13,12.5,14) = 14 ; bar6.. = 15
    # lower at bar4 = min(L1..L3) = 9 ; bar5 = min(10,10.5,11) = 10 ; bar6 = 10.5 ; bar7 = 11 ; bar8 = min(12,12.5,11) = 11
    df = frame()
    approx_list(ind.donchian_high(df.high, 3), [None, None, None, 13, 14, 15, 15, 15])
    approx_list(ind.donchian_low(df.low, 3), [None, None, None, 9, 10, 10.5, 11, 11])
    # including the current bar is a different (explicit) option
    approx_list(ind.donchian_high(df.high, 3, include_current=True), [None, None, 13, 14, 15, 15, 15, 16])


def test_period_return_by_hand():
    df = frame()
    approx_list(ind.period_return(df.close, 2), [None, None, 0.0, 13 / 12 - 1, 14 / 11 - 1, 0.0, 12 / 14 - 1, 15 / 13 - 1])


def test_realized_vol_by_hand():
    # bar4: log returns ln(12/11), ln(11/12), ln(13/11); sample stdev * sqrt(365)
    r = [math.log(12 / 11), math.log(11 / 12), math.log(13 / 11)]
    m = sum(r) / 3
    sd = math.sqrt(sum((x - m) ** 2 for x in r) / 2)
    assert sd * math.sqrt(365) == pytest.approx(2.4816864111, rel=1e-9)  # the hand number itself
    df = frame()
    rv = ind.realized_vol(df.close, 3, 365)
    assert all(math.isnan(v) for v in rv.iloc[:3])
    assert rv.iloc[3] == pytest.approx(2.4816864111, rel=1e-9)
    assert rv.iloc[7] == pytest.approx(3.3119780816, rel=1e-9)


def test_supertrend_by_hand_with_both_flips():
    # ATR(3), multiplier 1. First valid bar = bar3, starts bearish.
    # bar3: hl2=11.5, ATR=8/3 -> upper=14.1667, lower=8.8333, dir=-1, ST=upper
    # bar4: basic upper 12.5+25/9=15.278 is not < 14.1667 and C3=11 not > 14.1667 -> upper stays 14.1667;
    #       basic lower 12.5-25/9=9.7222 > 8.8333 -> lower=9.7222; C4=13 not > 14.1667 -> dir -1
    # bar5..7: upper stays 14.1667; lower ratchets 10.6481, 10.9321, 10.9321; no close above 14.1667
    # bar8: C8=15 > 14.1667 -> BULLISH flip; lower = 14-2209/729 = 10.96982; ST = lower
    # bar9 (H15.5 L9 C9.5): TR=6.5, ATR=(2*2209/729+6.5)/3=4.186786; basic lower 8.0632 not > 10.9698 and
    #       C8=15 not < 10.9698 -> lower stays 10.96982; C9=9.5 < 10.96982 -> BEARISH flip;
    #       upper = basic upper 16.43679 (because C8 > previous upper); ST = upper
    df = frame(BARS + [(15, 15.5, 9, 9.5)])
    st = ind.supertrend(df.high, df.low, df.close, atr_period=3, multiplier=1.0)
    approx_list(st["direction"], [None, None, -1, -1, -1, -1, -1, 1, -1])
    approx_list(st["flip"], [0, 0, 0, 0, 0, 0, 0, 1, -1])
    approx_list(
        st["upper"],
        [None, None, 11.5 + 8 / 3, 11.5 + 8 / 3, 11.5 + 8 / 3, 11.5 + 8 / 3, 11.5 + 8 / 3, 11.5 + 8 / 3, 12.25 + 4.186785550983082],
    )
    approx_list(
        st["lower"],
        [None, None, 11.5 - 8 / 3, 12.5 - 25 / 9, 13.5 - 77 / 27, 13.5 - 208 / 81, 13.5 - 208 / 81, 14 - 2209 / 729, 14 - 2209 / 729],
    )
    assert st["supertrend"].iloc[7] == pytest.approx(14 - 2209 / 729)
    assert st["supertrend"].iloc[8] == pytest.approx(12.25 + 4.186785550983082)


def _random_ohlc(n=400, seed=7):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.03, n)))
    open_ = np.concatenate([[100], close[:-1]])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.01, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.01, n)))
    idx = pd.date_range("2020-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)


INDICATOR_FUNCS = {
    "sma": lambda d: ind.sma(d.close, 20),
    "atr": lambda d: ind.atr(d.high, d.low, d.close, 14),
    "donchian_high": lambda d: ind.donchian_high(d.high, 20),
    "donchian_low": lambda d: ind.donchian_low(d.low, 10),
    "supertrend": lambda d: ind.supertrend(d.high, d.low, d.close, 10, 3.0)["supertrend"],
    "supertrend_dir": lambda d: ind.supertrend(d.high, d.low, d.close, 10, 3.0)["direction"],
    "realized_vol": lambda d: ind.realized_vol(d.close, 30),
    "period_return": lambda d: ind.period_return(d.close, 30),
}


@pytest.mark.parametrize("name", list(INDICATOR_FUNCS))
def test_indicator_is_causal(name):
    """Value at bar t must be identical whether the data ends at t or continues."""
    df = _random_ohlc()
    fn = INDICATOR_FUNCS[name]
    full = fn(df)
    for t in range(0, len(df), 7):
        trunc = fn(df.iloc[: t + 1])
        a, b = full.iloc[t], trunc.iloc[t]
        assert (math.isnan(a) and math.isnan(b)) or a == pytest.approx(b, rel=1e-12, abs=1e-12), (name, t, a, b)


def test_future_shock_does_not_change_past():
    """Mutating bars after t must not change any indicator value at or before t."""
    df = _random_ohlc()
    shocked = df.copy()
    shocked.iloc[300:, :] *= 3.0
    for name, fn in INDICATOR_FUNCS.items():
        a = fn(df).iloc[:300]
        b = fn(shocked).iloc[:300]
        pd.testing.assert_series_equal(a, b, check_names=False, obj=name)


def test_bad_window_rejected():
    df = frame()
    with pytest.raises(ValueError):
        ind.sma(df.close, 0)
    with pytest.raises(ValueError):
        ind.realized_vol(df.close, 1)
