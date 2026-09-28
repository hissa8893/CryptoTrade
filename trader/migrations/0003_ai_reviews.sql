-- 0003: optional AI analyst. One row per review of one proposed entry (the exact prompt is
-- identified by prompt_hash). Written in its own small transaction BEFORE the day's run, so a
-- re-run of the day replays the same verdict (same decisions, no second bill), and failed
-- calls are recorded too (status 'fallback' = the rule-based decision stood).
CREATE TABLE ai_reviews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_key         TEXT    NOT NULL,
    bar_date        TEXT    NOT NULL,
    strategy        TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    prompt_hash     TEXT    NOT NULL,
    prompt_version  TEXT    NOT NULL,
    model           TEXT    NOT NULL,          -- requested (pinned in config.yaml)
    served_model    TEXT,                      -- what actually answered (differs only after a refusal fallback)
    status          TEXT    NOT NULL CHECK (status IN ('ok', 'fallback')),
    fallback_reason TEXT,
    decision        TEXT    NOT NULL CHECK (decision IN ('approve', 'reduce', 'veto')),
    size_multiplier REAL    NOT NULL CHECK (size_multiplier >= 0 AND size_multiplier <= 1),
    confidence      REAL,
    reasons_json    TEXT    NOT NULL,
    request_json    TEXT    NOT NULL,          -- exactly what the model was shown
    response_text   TEXT,
    error           TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    latency_ms      INTEGER,
    cost_usd        REAL,
    created_at      TEXT    NOT NULL,
    UNIQUE (run_key, prompt_hash)
);
CREATE INDEX ix_ai_reviews_day ON ai_reviews (run_key, bar_date);
