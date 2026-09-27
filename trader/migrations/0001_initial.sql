-- 0001: initial schema. All timestamps are UTC ISO-8601 strings.
-- bar_date is the UTC calendar day (YYYY-MM-DD) of a CLOSED daily candle.

CREATE TABLE runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_key         TEXT    NOT NULL UNIQUE,         -- stable name, e.g. "paper:S1" or "bt:<uuid>"
    mode            TEXT    NOT NULL CHECK (mode IN ('backtest', 'paper', 'shadow')),
    strategy        TEXT    NOT NULL,                -- S1 | S2 | S3 | PORTFOLIO | BH
    params_json     TEXT    NOT NULL DEFAULT '{}',
    start           TEXT,
    "end"           TEXT,
    created_at      TEXT    NOT NULL,
    git_commit      TEXT,
    app_version     TEXT    NOT NULL,
    starting_equity REAL    NOT NULL,
    data_source     TEXT    NOT NULL DEFAULT 'exchange'   -- exchange id, or 'synthetic'
);
CREATE INDEX ix_runs_mode ON runs (mode);

CREATE TABLE signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    bar_date        TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    strategy        TEXT    NOT NULL,
    signal          TEXT    NOT NULL CHECK (signal IN ('enter', 'exit', 'rebalance')),
    strength        REAL,
    indicators_json TEXT    NOT NULL DEFAULT '{}',
    created_at      TEXT    NOT NULL,
    UNIQUE (run_id, strategy, symbol, bar_date)
);
CREATE INDEX ix_signals_run ON signals (run_id);
CREATE INDEX ix_signals_symbol ON signals (symbol);
CREATE INDEX ix_signals_bar_date ON signals (bar_date);

CREATE TABLE decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id    INTEGER NOT NULL UNIQUE REFERENCES signals (id) ON DELETE CASCADE,
    risk_result  TEXT    NOT NULL CHECK (risk_result IN ('pass', 'blocked', 'shrunk')),
    risk_reason  TEXT,
    llm_json     TEXT,
    final_action TEXT    NOT NULL,          -- buy | sell | none
    final_qty    REAL,
    created_at   TEXT    NOT NULL
);

-- Orders queued at bar t's close, filled at bar t+1's open (plus slippage).
CREATE TABLE orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    decision_id      INTEGER REFERENCES decisions (id) ON DELETE SET NULL,
    symbol           TEXT    NOT NULL,
    strategy         TEXT    NOT NULL,
    side             TEXT    NOT NULL CHECK (side IN ('buy', 'sell')),
    qty              REAL    NOT NULL CHECK (qty > 0),
    reason           TEXT    NOT NULL,
    stop_price       REAL,
    created_bar_date TEXT    NOT NULL,
    fill_bar_date    TEXT,
    status           TEXT    NOT NULL CHECK (status IN ('pending', 'filled', 'cancelled')),
    fill_px          REAL,
    fee              REAL,
    slippage         REAL,
    created_at       TEXT    NOT NULL,
    UNIQUE (run_id, strategy, symbol, created_bar_date, side, reason)
);
CREATE INDEX ix_orders_run ON orders (run_id);
CREATE INDEX ix_orders_status ON orders (status);

CREATE TABLE trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    symbol        TEXT    NOT NULL,
    strategy      TEXT    NOT NULL,
    entry_ts      TEXT    NOT NULL,
    entry_px      REAL    NOT NULL,
    qty           REAL    NOT NULL,
    initial_stop  REAL    NOT NULL,
    exit_ts       TEXT,
    exit_px       REAL,
    exit_reason   TEXT,
    fees          REAL    NOT NULL DEFAULT 0,
    slippage      REAL    NOT NULL DEFAULT 0,
    pnl           REAL,
    pnl_pct       REAL,
    r_multiple    REAL,
    entry_signal_id INTEGER REFERENCES signals (id) ON DELETE SET NULL,
    exit_signal_id  INTEGER REFERENCES signals (id) ON DELETE SET NULL,
    UNIQUE (run_id, strategy, symbol, entry_ts)
);
CREATE INDEX ix_trades_run ON trades (run_id);
CREATE INDEX ix_trades_symbol ON trades (symbol);
CREATE INDEX ix_trades_exit_ts ON trades (exit_ts);

-- Open positions with their current stop (for crash recovery).
CREATE TABLE positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    trade_id      INTEGER REFERENCES trades (id) ON DELETE SET NULL,
    symbol        TEXT    NOT NULL,
    strategy      TEXT    NOT NULL,
    qty           REAL    NOT NULL CHECK (qty > 0),
    avg_entry_px  REAL    NOT NULL,
    entry_ts      TEXT    NOT NULL,
    initial_stop  REAL    NOT NULL,
    current_stop  REAL    NOT NULL,
    highest_high  REAL,
    last_price    REAL,
    state_json    TEXT    NOT NULL DEFAULT '{}',
    updated_at    TEXT    NOT NULL,
    UNIQUE (run_id, strategy, symbol)
);
CREATE INDEX ix_positions_run ON positions (run_id);

CREATE TABLE equity_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    bar_date        TEXT    NOT NULL,
    equity          REAL    NOT NULL,
    cash            REAL    NOT NULL,
    positions_value REAL    NOT NULL DEFAULT 0,
    open_risk       REAL    NOT NULL DEFAULT 0,
    peak_equity     REAL    NOT NULL,
    drawdown_pct    REAL    NOT NULL,
    UNIQUE (run_id, bar_date)
);
CREATE INDEX ix_equity_run ON equity_snapshots (run_id);

CREATE TABLE risk_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER REFERENCES runs (id) ON DELETE CASCADE,
    ts           TEXT    NOT NULL,
    bar_date     TEXT,
    type         TEXT    NOT NULL,
    severity     TEXT    NOT NULL CHECK (severity IN ('info', 'warn', 'urgent')),
    symbol       TEXT,
    strategy     TEXT,
    message      TEXT    NOT NULL,
    details_json TEXT    NOT NULL DEFAULT '{}',
    dedupe_key   TEXT    UNIQUE,              -- makes re-running a day idempotent
    alerted      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_risk_events_run ON risk_events (run_id);
CREATE INDEX ix_risk_events_ts ON risk_events (ts);

CREATE TABLE job_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    bar_date     TEXT    NOT NULL UNIQUE,
    started_at   TEXT    NOT NULL,
    finished_at  TEXT,
    status       TEXT    NOT NULL CHECK (status IN ('running', 'ok', 'failed', 'skipped')),
    error        TEXT,
    heartbeat_at TEXT,
    attempts     INTEGER NOT NULL DEFAULT 1
);

-- Per-run engine state (peak equity, breaker/cooldown state, last processed bar).
CREATE TABLE run_state (
    run_id        INTEGER PRIMARY KEY REFERENCES runs (id) ON DELETE CASCADE,
    last_bar_date TEXT,
    state_json    TEXT NOT NULL DEFAULT '{}',
    updated_at    TEXT NOT NULL
);

-- Small key/value store for app-level state (daemon heartbeat, etc.).
CREATE TABLE kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
