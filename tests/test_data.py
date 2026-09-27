"""Market data: pagination, retries, safety guards, validation, cache, incremental updates."""

from datetime import datetime, timedelta

import ccxt
import numpy as np
import pandas as pd
import pytest

from trader import timeutil
from trader.config import AppConfig, DataConfig
from trader.data import (
    DataError,
    MarketData,
    OhlcvCache,
    PublicMarketData,
    SafetyError,
    candles_to_frame,
    clean_and_validate,
    fetch_ohlcv_history,
    freshness_problem,
    resolve_symbols,
    retry_call,
)
from trader.synthetic import generate
from trader.timeutil import MS_PER_DAY, UTC, ms

from tests.fakes import FakeExchange

NOW = datetime(2026, 9, 27, 19, 30, tzinfo=UTC)  # mid-day: today's candle is NOT closed


def client(fake, ex_id="bitstamp"):
    return PublicMarketData(ex_id, exchange=fake)


# ----------------------------------------------------------------------------- pagination
def test_pagination_skips_empty_windows_before_listing_and_gets_everything():
    fake = FakeExchange({"SOL/USD": "2020-08-11"}, NOW, page_cap=100)
    rows = fetch_ohlcv_history(
        client(fake), "SOL/USD", since=datetime(2015, 1, 1, tzinfo=UTC), now=NOW, sleep=lambda s: None
    )
    df = candles_to_frame(rows)
    assert df.index[0] == pd.Timestamp("2020-08-11", tz=UTC)
    assert df.index[-1] == pd.Timestamp("2026-09-26", tz=UTC)  # yesterday: last CLOSED candle
    assert not df.index.duplicated().any()
    expected_days = (pd.Timestamp("2026-09-26") - pd.Timestamp("2020-08-11")).days + 1
    assert len(df) == expected_days
    # proves we walked through the empty pre-listing windows instead of stopping on the first one
    assert fake.calls[0][1] == ms(datetime(2015, 1, 1, tzinfo=UTC))
    assert len(fake.calls) > expected_days // 100


def test_unclosed_candle_is_never_returned():
    fake = FakeExchange({"BTC/USD": "2026-01-01"}, NOW)
    rows = fetch_ohlcv_history(client(fake), "BTC/USD", since=datetime(2026, 1, 1, tzinfo=UTC), now=NOW)
    today_open = ms(datetime(2026, 9, 27, tzinfo=UTC))
    assert all(r[0] < today_open for r in rows)
    # the fake DID offer today's candle; the fetcher filtered it
    assert any(c[0] == today_open for c in FakeExchange({"BTC/USD": "2026-01-01"}, NOW).fetch_ohlcv("BTC/USD", "1d", today_open - MS_PER_DAY, 5))


def test_overlapping_pages_do_not_duplicate_after_cleaning():
    fake = FakeExchange({"BTC/USD": "2024-01-01"}, NOW, page_cap=50, overlap_days=3)
    rows = fetch_ohlcv_history(client(fake), "BTC/USD", since=datetime(2024, 1, 1, tzinfo=UTC), now=NOW)
    df, rep = clean_and_validate(candles_to_frame(rows), "BTC/USD", now=NOW)
    assert rep.ok
    assert not df.index.duplicated().any()
    assert rep.gaps == []


def test_retry_with_exponential_backoff():
    fake = FakeExchange({"BTC/USD": "2026-09-01"}, NOW, fail_first=2)
    sleeps = []
    rows = fetch_ohlcv_history(
        client(fake), "BTC/USD", since=datetime(2026, 9, 1, tzinfo=UTC), now=NOW, retries=3, base_delay=2.0, sleep=sleeps.append
    )
    assert sleeps == [2.0, 4.0]
    assert len(rows) == 26


def test_retry_exhausted_raises():
    fake = FakeExchange({"BTC/USD": "2026-09-01"}, NOW, fail_first=10)
    with pytest.raises(ccxt.NetworkError):
        fetch_ohlcv_history(client(fake), "BTC/USD", since=datetime(2026, 9, 1, tzinfo=UTC), now=NOW,
                            retries=2, base_delay=0.0, sleep=lambda s: None)


def test_non_transient_errors_are_not_retried():
    calls = []

    def boom():
        calls.append(1)
        raise ccxt.BadSymbol("nope")

    with pytest.raises(ccxt.BadSymbol):
        retry_call(boom, retries=5, base_delay=0, what="x", sleep=lambda s: None)
    assert len(calls) == 1


# ----------------------------------------------------------------------------- safety
def test_client_refuses_credentials():
    fake = FakeExchange({"BTC/USD": "2020-01-01"}, NOW)
    fake.apiKey = "abc"
    with pytest.raises(SafetyError):
        PublicMarketData("bitstamp", exchange=fake)


def test_real_ccxt_client_holds_no_credentials():
    c = PublicMarketData("bitstamp")
    for attr in ("apiKey", "secret", "password", "uid", "privateKey"):
        assert not getattr(c._ex, attr, None)


def test_capped_history_exchange_rejected():
    with pytest.raises(DataError):
        PublicMarketData("kraken")
    with pytest.raises(ValueError):
        DataConfig(exchange="kraken")


def test_resolve_symbols_prefers_quote_order_and_skips_inactive():
    markets = {
        "BTC/USD": {"spot": True, "active": True},
        "BTC/USDT": {"spot": True, "active": True},
        "ETH/USD": {"spot": True, "active": False},
        "ETH/USDT": {"spot": True, "active": True},
    }
    assert resolve_symbols(markets, ["BTC", "ETH", "SOL"], ["USD", "USDT"]) == {"BTC": "BTC/USD", "ETH": "ETH/USDT"}


# ----------------------------------------------------------------------------- validation
def _frame(days, start="2024-01-01"):
    idx = pd.date_range(start, periods=days, freq="D", tz=UTC, name="date")
    close = np.linspace(100, 120, days)
    df = pd.DataFrame(
        {"open": close - 1, "high": close + 2, "low": close - 3, "close": close, "volume": 10.0, "filled": False},
        index=idx,
    )
    return df


def test_clean_data_passes():
    df, rep = clean_and_validate(_frame(30), "X/USD", now=NOW)
    assert rep.ok and rep.rows == 30 and rep.filled_days == 0 and not rep.warnings


def test_exact_and_conflicting_duplicates():
    df = _frame(10)
    dup = df.iloc[[3]].copy()
    conflict = df.iloc[[5]].copy()
    conflict["close"] = conflict["close"] + 0.5
    raw = pd.concat([df, dup, conflict])
    clean, rep = clean_and_validate(raw, "X/USD", now=NOW)
    assert rep.ok
    assert rep.duplicates_dropped == 2 and rep.conflicting_duplicates == 1
    assert len(clean) == 10
    assert clean["close"].iloc[5] == pytest.approx(df["close"].iloc[5] + 0.5)  # newest kept


def test_small_gap_filled_flat_and_flagged():
    df = _frame(20).drop(pd.Timestamp("2024-01-08", tz=UTC)).drop(pd.Timestamp("2024-01-09", tz=UTC))
    clean, rep = clean_and_validate(df, "X/USD", now=NOW, max_fill_gap_days=3)
    assert rep.ok and rep.filled_days == 2 and len(clean) == 20
    filled = clean[clean["filled"]]
    assert list(filled.index.day) == [8, 9]
    prev_close = clean.loc[pd.Timestamp("2024-01-07", tz=UTC), "close"]
    assert (filled[["open", "high", "low", "close"]] == prev_close).all().all()
    assert (filled["volume"] == 0).all()
    assert rep.gaps[0]["action"] == "filled"


def test_large_gap_trims_history_explicitly():
    df = _frame(40)
    df = df[(df.index < pd.Timestamp("2024-01-10", tz=UTC)) | (df.index >= pd.Timestamp("2024-01-20", tz=UTC))]
    clean, rep = clean_and_validate(df, "X/USD", now=NOW, max_fill_gap_days=3)
    assert rep.ok
    assert rep.trimmed_before == "2024-01-20"
    assert clean.index[0] == pd.Timestamp("2024-01-20", tz=UTC)
    assert rep.gaps[0]["missing_days"] == 10 and rep.gaps[0]["action"] == "trimmed"


def test_ohlc_violation_is_an_error():
    df = _frame(10)
    df.loc[df.index[4], "high"] = df["close"].iloc[4] - 5  # high below close
    _, rep = clean_and_validate(df, "X/USD", now=NOW)
    assert not rep.ok and rep.ohlc_violations == ["2024-01-05"]


@pytest.mark.parametrize(
    "mutate,field",
    [
        (lambda d: d.__setitem__("volume", d["volume"].where(d.index != d.index[2], -1.0)), "negative_volume"),
        (lambda d: d.__setitem__("close", d["close"].where(d.index != d.index[2], np.nan)), "nan_rows"),
        (lambda d: d.__setitem__("low", d["low"].where(d.index != d.index[2], 0.0)), "nonpositive_prices"),
    ],
)
def test_bad_values_are_errors(mutate, field):
    df = _frame(10)
    mutate(df)
    _, rep = clean_and_validate(df, "X/USD", now=NOW)
    assert not rep.ok and getattr(rep, field) == 1


def test_misaligned_timestamps_are_an_error():
    df = _frame(5)
    df.index = df.index + pd.Timedelta(hours=1)
    _, rep = clean_and_validate(df, "X/USD", now=NOW)
    assert not rep.ok and rep.misaligned == 5


def test_unclosed_candle_dropped_in_validation():
    df = _frame(3, start="2026-09-25")  # 25, 26, 27 ; 27 is still open at NOW
    clean, rep = clean_and_validate(df, "X/USD", now=NOW)
    assert rep.unclosed_dropped == 1 and clean.index[-1] == pd.Timestamp("2026-09-26", tz=UTC)


def test_freshness():
    df = _frame(3, start="2026-09-24")  # ends 26th = last closed day at NOW
    assert freshness_problem(df, NOW) is None
    assert "stale" in freshness_problem(df, NOW + timedelta(days=2))


# ----------------------------------------------------------------------------- cache + service
def test_cache_roundtrip_preserves_tz_and_flags(tmp_path):
    cache = OhlcvCache(tmp_path, "bitstamp")
    df, _ = clean_and_validate(_frame(20).drop(pd.Timestamp("2024-01-05", tz=UTC)), "BTC/USD", now=NOW)
    cache.save("BTC/USD", df, {"exchange": "bitstamp"})
    back = cache.load("BTC/USD")
    pd.testing.assert_frame_equal(df, back, check_freq=False)
    assert str(back.index.tz) == "UTC" and back["filled"].sum() == 1
    assert cache.symbols() == ["BTC/USD"]


def test_market_data_full_then_incremental_update(home):
    cfg = AppConfig(data=DataConfig(assets=["BTC", "SOL", "XRP"], fallback_exchanges=["coinbaseexchange"],
                                    history_start="2019-01-01", retry_base_delay=0))
    primary = FakeExchange({"BTC/USD": "2015-01-01", "SOL/USD": "2020-08-11"}, NOW)
    fallback = FakeExchange({"XRP/USD": "2023-07-13"}, NOW, page_cap=300)
    fakes = {"bitstamp": primary, "coinbaseexchange": fallback}
    md = MarketData(cfg, home, client_factory=lambda ex: PublicMarketData(ex, exchange=fakes[ex]), sleep=lambda s: None)

    res = md.update(now=NOW)
    assert [r.report.ok for r in res] == [True, True, True]
    assert {r.symbol: r.source for r in res} == {"BTC/USD": "bitstamp", "SOL/USD": "bitstamp", "XRP/USD": "coinbaseexchange"}
    assert all(r.fresh_problem is None for r in res)
    btc = md.load("BTC/USD")
    assert btc.index[0] == pd.Timestamp("2019-01-01", tz=UTC) and btc.index[-1] == pd.Timestamp("2026-09-26", tz=UTC)

    # two days later: incremental update only re-fetches the tail
    later = NOW + timedelta(days=2)
    primary.now = later
    fallback.now = later
    primary.calls.clear()
    res2 = md.update(now=later)
    assert all(r.report.ok and r.fresh_problem is None for r in res2)
    assert len(primary.calls) == 2  # one small page per symbol (BTC, SOL)
    btc2 = md.load("BTC/USD")
    assert btc2.index[-1] == pd.Timestamp("2026-09-28", tz=UTC)
    assert len(btc2) == len(btc) + 2
    pd.testing.assert_frame_equal(btc2.iloc[: len(btc)], btc, check_freq=False)


def test_primary_unreachable_is_a_clear_error(home):
    class Down(FakeExchange):
        def load_markets(self):
            raise ccxt.ExchangeNotAvailable("403 blocked")

    cfg = AppConfig(data=DataConfig(assets=["BTC"], request_retries=1, retry_base_delay=0))
    md = MarketData(cfg, home, client_factory=lambda ex: PublicMarketData(ex, exchange=Down({"BTC/USD": "2020-01-01"}, NOW)),
                    sleep=lambda s: None)
    with pytest.raises(DataError, match="unreachable"):
        md.update(now=NOW)


# ----------------------------------------------------------------------------- synthetic
def test_synthetic_is_deterministic_prefix_stable_and_valid():
    a = generate("BTC", end=datetime(2024, 1, 1).date())
    b = generate("BTC", end=datetime(2025, 1, 1).date())
    pd.testing.assert_frame_equal(a, b.loc[a.index], check_freq=False)
    _, rep = clean_and_validate(b, "BTC/USD", now=NOW)
    assert rep.ok, rep.errors


# ----------------------------------------------------------------------------- real network (optional)
def _exchange_reachable() -> bool:
    try:
        PublicMarketData("bitstamp", timeout_s=10).load_markets()
        return True
    except Exception:
        return False


@pytest.mark.network
def test_real_bitstamp_btc_has_5_years():
    if not _exchange_reachable():
        pytest.skip("www.bitstamp.net unreachable from this environment")
    now = timeutil.now_utc()
    c = PublicMarketData("bitstamp")
    rows = fetch_ohlcv_history(c, "BTC/USD", since=datetime(2015, 1, 1, tzinfo=UTC), now=now)
    df, rep = clean_and_validate(candles_to_frame(rows), "BTC/USD", now=now)
    assert rep.ok, rep.errors
    assert (df.index[-1] - df.index[0]).days >= 5 * 365
