"""Market data: public OHLCV fetch (paginated, retried), validation, Parquet cache.

Safety: the exchange client is a read-only wrapper around ccxt that never holds
credentials and only exposes public market-data calls. There is no order code.

Frame format used everywhere: DatetimeIndex (tz=UTC, midnight, name="date") with
float columns open/high/low/close/volume and a bool column `filled` that marks
synthetic flat bars inserted to bridge small exchange gaps.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd

from trader.config import CAPPED_HISTORY_EXCHANGES, AppConfig
from trader.paths import Paths, cli_hint
from trader.timeutil import (
    MS_PER_DAY,
    UTC,
    day_start,
    iso,
    last_closed_day,
    ms,
    now_utc,
)

log = logging.getLogger(__name__)

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]
TIMEFRAME_MS = {"1d": MS_PER_DAY}
PAGE_LIMITS = {"bitstamp": 1000, "coinbaseexchange": 300, "binance": 1000, "binanceus": 1000}
DEFAULT_PAGE_LIMIT = 300
# History may be trimmed at a long exchange gap only if at least this many days remain after
# it (200-day SMA warm-up + a year of use). A more recent long gap is an error instead: it
# must never silently shrink history to a few days.
MIN_HISTORY_AFTER_TRIM_DAYS = 400


class DataError(RuntimeError):
    pass


class SafetyError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------
# Retry
# --------------------------------------------------------------------------------------
def _transient_errors() -> tuple[type[BaseException], ...]:
    import ccxt

    return (
        ccxt.NetworkError,  # includes RequestTimeout, ExchangeNotAvailable, DDoSProtection, RateLimitExceeded
        ConnectionError,
        TimeoutError,
    )


def retry_call(
    fn: Callable[[], Any],
    *,
    retries: int,
    base_delay: float,
    what: str,
    sleep: Callable[[float], None] = time.sleep,
    retry_on: tuple[type[BaseException], ...] | None = None,
) -> Any:
    """Call fn, retrying transient errors with exponential backoff (base, 2x, 4x, ...)."""
    retry_on = retry_on or _transient_errors()
    attempt = 0
    while True:
        try:
            return fn()
        except retry_on as exc:
            if attempt >= retries:
                raise
            delay = base_delay * (2**attempt)
            log.warning("%s failed (%s: %s); retry %d/%d in %.1fs", what, type(exc).__name__, exc, attempt + 1, retries, delay)
            sleep(delay)
            attempt += 1


# --------------------------------------------------------------------------------------
# Read-only exchange client
# --------------------------------------------------------------------------------------
_CREDENTIAL_ATTRS = ("apiKey", "secret", "password", "uid", "privateKey", "walletAddress", "token")


class PublicMarketData:
    """Read-only facade over a ccxt exchange: markets, OHLCV, and server time. Nothing else."""

    def __init__(self, exchange_id: str, *, timeout_s: int = 30, exchange: Any | None = None):
        if exchange_id.lower() in CAPPED_HISTORY_EXCHANGES:
            raise DataError(f"{exchange_id} caps public OHLC history; not usable for backtests")
        if exchange is None:
            import ccxt

            if not hasattr(ccxt, exchange_id):
                raise DataError(f"unknown ccxt exchange id: {exchange_id}")
            exchange = getattr(ccxt, exchange_id)({"enableRateLimit": True, "timeout": timeout_s * 1000})
        for attr in _CREDENTIAL_ATTRS:
            if getattr(exchange, attr, None):
                raise SafetyError(f"exchange client has credential '{attr}' set; this app must never hold keys")
        self.id = exchange_id
        self._ex = exchange

    @property
    def page_limit(self) -> int:
        return PAGE_LIMITS.get(self.id, DEFAULT_PAGE_LIMIT)

    def load_markets(self) -> dict[str, dict]:
        return self._ex.load_markets()

    def fetch_ohlcv(self, symbol: str, timeframe: str, since: int, limit: int) -> list[list]:
        return self._ex.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)

    def has_fetch_ohlcv(self) -> bool:
        has = getattr(self._ex, "has", {}) or {}
        return bool(has.get("fetchOHLCV", True))


def resolve_symbols(markets: dict[str, dict], assets: Iterable[str], quotes: Iterable[str]) -> dict[str, str]:
    """Map each base asset to the first active spot market in quote-preference order."""
    out: dict[str, str] = {}
    quotes = list(quotes)
    for asset in assets:
        for q in quotes:
            sym = f"{asset}/{q}"
            m = markets.get(sym)
            if m is None:
                continue
            if m.get("spot", True) is False or m.get("active") is False:
                continue
            out[asset] = sym
            break
    return out


# --------------------------------------------------------------------------------------
# Paginated fetch
# --------------------------------------------------------------------------------------
def fetch_ohlcv_history(
    client: PublicMarketData,
    symbol: str,
    *,
    timeframe: str = "1d",
    since: datetime,
    now: datetime | None = None,
    retries: int = 5,
    base_delay: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    max_pages: int = 5000,
) -> list[list]:
    """Fetch every CLOSED candle from `since` up to the last closed bar.

    Exchanges page by fixed time window (Bitstamp: 1000 bars from `start`). A window
    before an asset was listed comes back EMPTY, so an empty page means "skip one
    window ahead", not "done". The loop ends once the cursor passes the last closed bar.
    """
    tf_ms = TIMEFRAME_MS[timeframe]
    now = now or now_utc()
    last_closed_open = ms(day_start(last_closed_day(now)))
    cursor = ms(since)
    limit = client.page_limit
    rows: list[list] = []
    pages = 0
    while cursor <= last_closed_open:
        pages += 1
        if pages > max_pages:
            raise DataError(f"{symbol}: pagination exceeded {max_pages} pages; aborting")
        batch = retry_call(
            lambda c=cursor: client.fetch_ohlcv(symbol, timeframe, c, limit),
            retries=retries,
            base_delay=base_delay,
            what=f"fetch_ohlcv {client.id} {symbol} since={c_iso(cursor)}",
            sleep=sleep,
        )
        batch = [list(c) for c in (batch or []) if c and cursor <= int(c[0]) <= last_closed_open]
        if batch:
            rows.extend(batch)
            nxt = max(int(c[0]) for c in batch) + tf_ms
        else:
            nxt = cursor + limit * tf_ms
        if nxt <= cursor:
            raise DataError(f"{symbol}: pagination made no progress at {c_iso(cursor)}")
        cursor = nxt
    log.info("fetched %d candles for %s from %s in %d pages", len(rows), symbol, client.id, pages)
    return rows


def c_iso(ms_value: int) -> str:
    return iso(datetime.fromtimestamp(ms_value / 1000, tz=UTC))


def candles_to_frame(rows: list[list]) -> pd.DataFrame:
    """Raw ccxt rows -> frame (duplicates preserved; cleaning happens in clean_and_validate)."""
    if not rows:
        return _empty_frame()
    arr = np.asarray([[float(x) if x is not None else np.nan for x in r[:6]] for r in rows], dtype="float64")
    idx = pd.to_datetime(arr[:, 0].astype("int64"), unit="ms", utc=True)
    df = pd.DataFrame(arr[:, 1:6], index=idx, columns=OHLCV_COLUMNS)
    df.index.name = "date"
    df["filled"] = False
    return df


def _empty_frame() -> pd.DataFrame:
    df = pd.DataFrame(
        {c: pd.Series(dtype="float64") for c in OHLCV_COLUMNS} | {"filled": pd.Series(dtype="bool")},
        index=pd.DatetimeIndex([], tz=UTC, name="date"),
    )
    return df


# --------------------------------------------------------------------------------------
# Validation / cleaning
# --------------------------------------------------------------------------------------
@dataclass
class ValidationReport:
    symbol: str
    rows: int = 0
    start: str | None = None
    end: str | None = None
    unclosed_dropped: int = 0
    duplicates_dropped: int = 0
    conflicting_duplicates: int = 0
    misaligned: int = 0
    nan_rows: int = 0
    nonpositive_prices: int = 0
    negative_volume: int = 0
    ohlc_violations: list[str] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)
    filled_days: int = 0
    trimmed_before: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        return d

    def summary(self) -> str:
        status = "OK" if self.ok else "FAILED"
        if not self.rows:
            return f"{self.symbol}: {status}" + (" — " + "; ".join(self.errors) if self.errors else "")
        parts = [f"{self.symbol}: {status}", f"{self.rows} rows", f"{self.start} .. {self.end}"]
        if self.filled_days:
            parts.append(f"{self.filled_days} gap day(s) filled flat")
        if self.trimmed_before:
            parts.append(f"history trimmed to start {self.trimmed_before}")
        if self.duplicates_dropped:
            parts.append(f"{self.duplicates_dropped} duplicate(s) dropped")
        if self.errors:
            parts.append("errors: " + "; ".join(self.errors))
        return " | ".join(parts)


def clean_and_validate(
    raw: pd.DataFrame,
    symbol: str,
    *,
    now: datetime | None = None,
    max_fill_gap_days: int = 3,
    check_gaps: bool = True,
) -> tuple[pd.DataFrame, ValidationReport]:
    """Validate raw candles and return a clean, gap-free daily frame plus a report.

    Hard errors (data unusable, never traded): misaligned timestamps, NaNs,
    non-positive prices, negative volume, high/low inconsistent with open/close.
    Handled explicitly (and reported): unclosed last candle (dropped), exact
    duplicates (dropped), conflicting duplicates (newest kept), small gaps
    (<= max_fill_gap_days, filled flat with filled=True), large gaps (history
    before the gap is trimmed off if >= MIN_HISTORY_AFTER_TRIM_DAYS remain after
    it; otherwise the series is rejected).
    """
    now = now or now_utc()
    rep = ValidationReport(symbol=symbol)
    df = raw.copy()
    if "filled" not in df.columns:
        df["filled"] = False
    df = df.sort_index(kind="stable")

    if df.empty:
        rep.errors.append("no data")
        return df, rep

    # 1. never keep a candle whose day has not fully ended in UTC
    closed_mask = (df.index + pd.Timedelta(days=1)) <= pd.Timestamp(now)
    rep.unclosed_dropped = int((~closed_mask).sum())
    df = df[closed_mask]

    # 2. alignment to UTC midnight
    misaligned = df.index != df.index.normalize()
    rep.misaligned = int(misaligned.sum())
    if rep.misaligned:
        rep.errors.append(f"{rep.misaligned} candle(s) not aligned to UTC midnight")

    # 3. duplicates
    dup_mask = df.index.duplicated(keep="last")
    if dup_mask.any():
        dups = df[df.index.duplicated(keep=False)]
        conflicting = 0
        for _, grp in dups.groupby(level=0):
            if len(grp[OHLCV_COLUMNS].drop_duplicates()) > 1:
                conflicting += 1
        rep.duplicates_dropped = int(dup_mask.sum())
        rep.conflicting_duplicates = conflicting
        df = df[~dup_mask]
        rep.warnings.append(f"dropped {rep.duplicates_dropped} duplicate timestamp(s)")
        if conflicting:
            rep.warnings.append(f"{conflicting} duplicate(s) had different values; kept the newest fetch")

    # 4. value checks
    vals = df[OHLCV_COLUMNS]
    nan_rows = vals.isna().any(axis=1)
    rep.nan_rows = int(nan_rows.sum())
    if rep.nan_rows:
        rep.errors.append(f"{rep.nan_rows} row(s) contain NaN")
    nonpos = (vals[["open", "high", "low", "close"]] <= 0).any(axis=1)
    rep.nonpositive_prices = int(nonpos.sum())
    if rep.nonpositive_prices:
        rep.errors.append(f"{rep.nonpositive_prices} row(s) with non-positive prices")
    negvol = vals["volume"] < 0
    rep.negative_volume = int(negvol.sum())
    if rep.negative_volume:
        rep.errors.append(f"{rep.negative_volume} row(s) with negative volume")
    bad_ohlc = (
        (vals["high"] < vals[["open", "close"]].max(axis=1))
        | (vals["low"] > vals[["open", "close"]].min(axis=1))
        | (vals["high"] < vals["low"])
    )
    if bad_ohlc.any():
        rep.ohlc_violations = [d.date().isoformat() for d in df.index[bad_ohlc]]
        rep.errors.append(
            f"{len(rep.ohlc_violations)} row(s) violate high>=max(open,close) / low<=min(open,close): "
            + ", ".join(rep.ohlc_violations[:5])
            + (" ..." if len(rep.ohlc_violations) > 5 else "")
        )

    # 5. gaps
    if check_gaps and len(df) >= 2 and not rep.misaligned:
        df = _handle_gaps(df, rep, max_fill_gap_days)

    rep.rows = len(df)
    if len(df):
        rep.start = df.index[0].date().isoformat()
        rep.end = df.index[-1].date().isoformat()
    else:
        rep.errors.append("no closed candles")
    return df, rep


def _handle_gaps(df: pd.DataFrame, rep: ValidationReport, max_fill: int) -> pd.DataFrame:
    diffs = df.index.to_series().diff().dt.days.iloc[1:]
    gap_ends = diffs[diffs > 1]
    if gap_ends.empty:
        return df
    # large gaps: trim everything before the most recent large gap
    large = gap_ends[gap_ends - 1 > max_fill]
    for end_ts, d in gap_ends.items():
        missing = int(d) - 1
        before = df.index[df.index.get_loc(end_ts) - 1]
        rep.gaps.append(
            {
                "after": before.date().isoformat(),
                "before": end_ts.date().isoformat(),
                "missing_days": missing,
                "action": "trimmed" if missing > max_fill else "filled",
            }
        )
    if not large.empty:
        cut = large.index[-1]
        remaining = (df.index[-1] - cut).days + 1
        if remaining < MIN_HISTORY_AFTER_TRIM_DAYS:
            g = next(g for g in rep.gaps if g["before"] == cut.date().isoformat())
            g["action"] = "rejected"
            rep.errors.append(
                f"{g['missing_days']}-day data gap between {g['after']} and {g['before']} is too recent to trim "
                f"(only {remaining} days would remain; {MIN_HISTORY_AFTER_TRIM_DAYS} needed). Not tradable until "
                f"the exchange backfills it; then run: {cli_hint('data fetch --full-refresh')}"
            )
            return df
        df = df[df.index >= cut]
        rep.trimmed_before = cut.date().isoformat()
        rep.warnings.append(
            f"history trimmed to start {rep.trimmed_before}: gap longer than {max_fill} day(s) before it"
        )
        # gaps before the cut no longer matter
        for g in rep.gaps:
            if g["before"] < rep.trimmed_before:
                g["action"] = "trimmed"
    # small gaps: fill flat at the previous close, volume 0, flagged
    full_idx = pd.date_range(df.index[0], df.index[-1], freq="D", tz=UTC, name="date")
    if len(full_idx) != len(df):
        missing_idx = full_idx.difference(df.index)
        df = df.reindex(full_idx)
        prev_close = df["close"].ffill()
        for col in ("open", "high", "low", "close"):
            df.loc[missing_idx, col] = prev_close.loc[missing_idx]
        df.loc[missing_idx, "volume"] = 0.0
        df["filled"] = df["filled"].astype("boolean").fillna(True).astype(bool)
        rep.filled_days = len(missing_idx)
        rep.warnings.append(
            f"filled {rep.filled_days} missing day(s) flat at previous close (volume 0, flagged; no entries on them)"
        )
    return df


def freshness_problem(df: pd.DataFrame, now: datetime | None = None) -> str | None:
    """None when the last bar is the most recent closed UTC day, else a reason."""
    expected = last_closed_day(now)
    if df.empty:
        return "no data"
    last = df.index[-1].date()
    if last < expected:
        return f"stale: last candle {last.isoformat()}, expected {expected.isoformat()}"
    if last > expected:
        return f"last candle {last.isoformat()} is not closed yet (expected {expected.isoformat()})"
    return None


# --------------------------------------------------------------------------------------
# Parquet cache
# --------------------------------------------------------------------------------------
class OhlcvCache:
    """Stores RAW closed candles (open/high/low/close/volume, deduplicated). Gap filling and
    trimming are recomputed on every load, so a policy decision never destroys history."""

    def __init__(self, cache_dir: Path, source: str):
        self.dir = Path(cache_dir) / source
        self.source = source

    def _stem(self, symbol: str, timeframe: str) -> str:
        return f"{symbol.replace('/', '-')}_{timeframe}"

    def path(self, symbol: str, timeframe: str = "1d") -> Path:
        return self.dir / f"{self._stem(symbol, timeframe)}.parquet"

    def meta_path(self, symbol: str, timeframe: str = "1d") -> Path:
        return self.dir / f"{self._stem(symbol, timeframe)}.meta.json"

    def load(self, symbol: str, timeframe: str = "1d") -> pd.DataFrame | None:
        p = self.path(symbol, timeframe)
        if not p.exists():
            return None
        df = pd.read_parquet(p)
        if df.index.tz is None:
            df.index = df.index.tz_localize(UTC)
        df.index.name = "date"
        if "filled" in df.columns:  # legacy (pre-raw) cache: synthetic gap-fill rows are not real candles
            df = df[~df["filled"].astype(bool)]
        return df[OHLCV_COLUMNS]

    def meta(self, symbol: str, timeframe: str = "1d") -> dict:
        p = self.meta_path(symbol, timeframe)
        return json.loads(p.read_text()) if p.exists() else {}

    def save(self, symbol: str, df: pd.DataFrame, meta: dict, timeframe: str = "1d") -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.path(symbol, timeframe)
        tmp = p.with_suffix(".parquet.tmp")
        df[OHLCV_COLUMNS].to_parquet(tmp, engine="pyarrow")
        os.replace(tmp, p)
        mp = self.meta_path(symbol, timeframe)
        mtmp = mp.with_suffix(".json.tmp")
        mtmp.write_text(json.dumps(meta, indent=2, default=str))
        os.replace(mtmp, mp)
        return p

    def symbols(self) -> list[str]:
        if not self.dir.exists():
            return []
        out = []
        for p in sorted(self.dir.glob("*_1d.parquet")):
            out.append(p.name[: -len("_1d.parquet")].replace("-", "/"))
        return out


# --------------------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------------------
@dataclass
class UpdateResult:
    symbol: str
    source: str
    fetched: int
    report: ValidationReport
    fresh_problem: str | None
    revised: int = 0


class MarketData:
    """Fetches, validates, and caches daily candles for the configured universe."""

    def __init__(
        self,
        cfg: AppConfig,
        paths: Paths,
        *,
        client_factory: Callable[[str], PublicMarketData] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cfg = cfg
        self.paths = paths
        self._client_factory = client_factory or (
            lambda ex_id: PublicMarketData(ex_id, timeout_s=cfg.data.request_timeout_seconds)
        )
        self._clients: dict[str, PublicMarketData] = {}
        self._sleep = sleep

    # -- sources -------------------------------------------------------------------
    @property
    def source(self) -> str:
        return "synthetic" if self.cfg.data.source == "synthetic" else self.cfg.data.exchange

    @property
    def cache(self) -> OhlcvCache:
        return OhlcvCache(self.paths.cache, self.source)

    def client(self, exchange_id: str) -> PublicMarketData:
        if exchange_id not in self._clients:
            self._clients[exchange_id] = self._client_factory(exchange_id)
        return self._clients[exchange_id]

    def _symbols_file(self) -> Path:
        return self.cache.dir / "symbols.json"

    def resolve(self, refresh: bool = False) -> dict[str, tuple[str, str]]:
        """asset -> (exchange_id, symbol). Primary exchange first; a fallback exchange is
        used only for an asset the primary does not list (each series stays single-source)."""
        if self.source == "synthetic":
            from trader.synthetic import SYNTHETIC_ASSETS

            return {a: ("synthetic", f"{a}/USD") for a in self.cfg.data.assets if a in SYNTHETIC_ASSETS}
        f = self._symbols_file()
        fingerprint = {
            "exchange": self.cfg.data.exchange,
            "fallback_exchanges": self.cfg.data.fallback_exchanges,
            "quote_preference": self.cfg.data.quote_preference,
        }
        if f.exists() and not refresh:
            saved = json.loads(f.read_text())
            # reuse only if resolved under the SAME settings (else a config change is silently ignored)
            if saved.get("config") == fingerprint and set(saved.get("assets", {})) >= set(self.cfg.data.assets):
                return {a: tuple(saved["assets"][a]) for a in self.cfg.data.assets}  # type: ignore[misc]
        out: dict[str, tuple[str, str]] = {}
        remaining = list(self.cfg.data.assets)
        for ex_id in [self.cfg.data.exchange, *self.cfg.data.fallback_exchanges]:
            if not remaining:
                break
            client = self.client(ex_id)
            try:
                markets = retry_call(
                    client.load_markets,
                    retries=self.cfg.data.request_retries,
                    base_delay=self.cfg.data.retry_base_delay,
                    what=f"load_markets {ex_id}",
                    sleep=self._sleep,
                )
            except Exception as exc:
                if ex_id == self.cfg.data.exchange:
                    raise DataError(f"primary exchange {ex_id} unreachable: {type(exc).__name__}: {exc}") from exc
                log.warning("fallback exchange %s unreachable: %s", ex_id, exc)
                continue
            found = resolve_symbols(markets, remaining, self.cfg.data.quote_preference)
            for a, sym in found.items():
                out[a] = (ex_id, sym)
            remaining = [a for a in remaining if a not in found]
        if remaining:
            log.warning("no market found for assets %s", remaining)
        self.cache.dir.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"config": fingerprint, "assets": {a: list(v) for a, v in out.items()}}, indent=2))
        return out

    # -- update --------------------------------------------------------------------
    def update(
        self,
        assets: list[str] | None = None,
        *,
        full_refresh: bool = False,
        now: datetime | None = None,
    ) -> list[UpdateResult]:
        now = now or now_utc()
        if self.source == "synthetic":
            return self._update_synthetic(assets, now)
        mapping = self.resolve()
        results = []
        for asset in assets or self.cfg.data.assets:
            if asset not in mapping:
                rep = ValidationReport(symbol=asset, errors=[f"no market for {asset} on configured exchanges"])
                results.append(UpdateResult(asset, "-", 0, rep, "no market"))
                continue
            ex_id, symbol = mapping[asset]
            try:
                results.append(self._update_symbol(ex_id, symbol, full_refresh=full_refresh, now=now))
            except Exception as exc:  # network down, exchange error: report it, keep going
                log.error("update failed for %s on %s: %s: %s", symbol, ex_id, type(exc).__name__, exc)
                rep = ValidationReport(symbol=symbol, errors=[f"{ex_id} fetch failed: {type(exc).__name__}: {exc}"])
                results.append(UpdateResult(symbol, ex_id, 0, rep, "fetch failed; cached data unchanged"))
        return results

    def _update_symbol(self, ex_id: str, symbol: str, *, full_refresh: bool, now: datetime) -> UpdateResult:
        cache = self.cache
        cached = None if full_refresh else cache.load(symbol)
        history_start = datetime.fromisoformat(self.cfg.data.history_start).replace(tzinfo=UTC)
        if cached is not None and len(cached):
            # re-fetch a few recent days to pick up late corrections
            since = max(history_start, cached.index[-1].to_pydatetime() - timedelta(days=3))
        else:
            since = history_start
        rows = fetch_ohlcv_history(
            self.client(ex_id),
            symbol,
            since=since,
            now=now,
            retries=self.cfg.data.request_retries,
            base_delay=self.cfg.data.retry_base_delay,
            sleep=self._sleep,
        )
        new = candles_to_frame(rows)[OHLCV_COLUMNS]
        new = new[~new.index.duplicated(keep="last")].sort_index()
        # a malformed new batch must never overwrite good cached data
        _, new_rep = clean_and_validate(new, symbol, now=now, check_gaps=False) if len(new) else (None, None)
        if new_rep is not None and new_rep.errors:
            rep = ValidationReport(symbol=symbol, errors=[f"new data rejected: {e}" for e in new_rep.errors])
            log.error("rejected new candles for %s: %s", symbol, new_rep.errors)
            return UpdateResult(symbol, ex_id, len(rows), rep, "new data rejected; cached data unchanged")
        revised = 0
        if cached is not None and len(cached):
            overlap = cached.index.intersection(new.index)
            if len(overlap):
                diff = ~np.isclose(cached.loc[overlap, OHLCV_COLUMNS], new.loc[overlap, OHLCV_COLUMNS], rtol=0, atol=0)
                revised = int(diff.any(axis=1).sum())
            merged = pd.concat([cached[OHLCV_COLUMNS], new])  # newest last -> kept
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        else:
            merged = new
        closed = (merged.index + pd.Timedelta(days=1)) <= pd.Timestamp(now)
        merged = merged[closed]
        cache.save(symbol, merged, {"symbol": symbol, "exchange": ex_id, "fetched_at": iso(now)})
        clean, rep = clean_and_validate(merged, symbol, now=now, max_fill_gap_days=self.cfg.data.max_fill_gap_days)
        if revised:
            rep.warnings.append(f"{revised} recent candle(s) were revised by the exchange; kept the newest values")
        fresh = freshness_problem(clean, now) if rep.ok else "validation failed"
        if not rep.ok:
            log.error("validation failed for %s: %s", symbol, rep.errors)
        return UpdateResult(symbol, ex_id, len(rows), rep, fresh, revised)

    def _update_synthetic(self, assets: list[str] | None, now: datetime) -> list[UpdateResult]:
        from trader.synthetic import generate

        results = []
        end = last_closed_day(now)
        for asset in assets or self.cfg.data.assets:
            symbol = f"{asset}/USD"
            try:
                raw = generate(asset, end=end)
            except KeyError:
                rep = ValidationReport(symbol=symbol, errors=[f"no synthetic model for {asset}"])
                results.append(UpdateResult(symbol, "synthetic", 0, rep, "no data"))
                continue
            clean, rep = clean_and_validate(raw, symbol, now=now, max_fill_gap_days=self.cfg.data.max_fill_gap_days)
            if rep.ok:
                self.cache.save(
                    symbol,
                    raw,
                    {"symbol": symbol, "exchange": "synthetic", "fetched_at": iso(now),
                     "WARNING": "SYNTHETIC DATA - not real market prices"},
                )
            results.append(UpdateResult(symbol, "synthetic", len(raw), rep, freshness_problem(clean, now)))
        return results

    # -- load ----------------------------------------------------------------------
    def symbols(self) -> list[str]:
        if self.source == "synthetic":
            return [f"{a}/USD" for a in self.cfg.data.assets]
        f = self._symbols_file()
        if not f.exists():
            return self.cache.symbols()
        saved = json.loads(f.read_text()).get("assets", {})
        return [saved[a][1] for a in self.cfg.data.assets if a in saved]

    def load_checked(self, symbol: str, now: datetime | None = None) -> tuple[pd.DataFrame, ValidationReport]:
        """Cached raw candles -> (clean frame, validation report). Raises if nothing is cached."""
        raw = self.cache.load(symbol)
        if raw is None:
            raise DataError(f"no cached data for {symbol} ({self.source}); run: {cli_hint('data fetch')}")
        return clean_and_validate(raw, symbol, now=now, max_fill_gap_days=self.cfg.data.max_fill_gap_days)

    def load(self, symbol: str, now: datetime | None = None) -> pd.DataFrame:
        """Clean, validated candles; raises DataError if the cached series fails validation."""
        df, rep = self.load_checked(symbol, now)
        if not rep.ok:
            raise DataError(f"{symbol} failed validation: " + "; ".join(rep.errors))
        return df

    def load_all(self, now: datetime | None = None) -> dict[str, pd.DataFrame]:
        """Every cached symbol that passes validation (failures are logged, never traded)."""
        out = {}
        for sym in self.symbols():
            try:
                out[sym] = self.load(sym, now)
            except DataError as exc:
                log.error("excluding %s: %s", sym, exc)
        return out


def asset_of(symbol: str) -> str:
    return symbol.split("/")[0]


def history_years(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    return (df.index[-1] - df.index[0]).days / 365.25


def first_date(df: pd.DataFrame) -> date | None:
    return df.index[0].date() if len(df) else None
