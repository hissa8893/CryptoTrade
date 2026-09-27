-- 0002: live paper trading. Stable engine reference numbers let a run written day by day
-- link rows across days (e.g. a trade closed today whose entry signal was weeks ago).
ALTER TABLE signals ADD COLUMN ref INTEGER;
ALTER TABLE decisions ADD COLUMN ref INTEGER;
ALTER TABLE orders ADD COLUMN ext_id INTEGER;
ALTER TABLE orders ADD COLUMN filled_qty REAL;
CREATE UNIQUE INDEX ux_signals_run_ref ON signals (run_id, ref);
CREATE UNIQUE INDEX ux_orders_run_ext ON orders (run_id, ext_id);

-- Outgoing alerts (email/Telegram). dedupe_key makes re-running a day never re-send.
CREATE TABLE alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    severity    TEXT NOT NULL CHECK (severity IN ('info', 'warn', 'urgent')),
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL UNIQUE,
    status      TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'skipped', 'failed')),
    sent_at     TEXT,
    error       TEXT
);
CREATE INDEX ix_alerts_status ON alerts (status);
