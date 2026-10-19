-- spotguard schema v1. Money and quantities are stored as decimal strings (TEXT), never floats.

CREATE TABLE events (
    event_id   TEXT PRIMARY KEY,          -- replay-safe: a duplicate event id is ignored
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE intents (
    intent_id      TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL,
    asset_key      TEXT NOT NULL,
    side           TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    purpose        TEXT NOT NULL CHECK (purpose IN ('entry', 'exit', 'emergency_exit')),
    qty            TEXT NOT NULL,
    limit_price    TEXT NOT NULL,
    reason         TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    model_version  TEXT,
    decision       TEXT NOT NULL
);

CREATE TABLE orders (
    client_order_id   TEXT PRIMARY KEY,
    intent_id         TEXT NOT NULL REFERENCES intents(intent_id),
    asset_key         TEXT NOT NULL,
    side              TEXT NOT NULL,
    purpose           TEXT NOT NULL,
    qty               TEXT NOT NULL,
    limit_price       TEXT NOT NULL,
    time_in_force     TEXT NOT NULL,
    state             TEXT NOT NULL,
    exchange_order_id TEXT,
    filled_qty        TEXT NOT NULL DEFAULT '0',
    filled_notional   TEXT NOT NULL DEFAULT '0',
    fees              TEXT NOT NULL DEFAULT '0',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    last_error        TEXT
);
CREATE INDEX orders_state ON orders(state);

CREATE TABLE order_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL,
    from_state      TEXT,
    to_state        TEXT NOT NULL,
    at              TEXT NOT NULL,
    detail          TEXT
);

CREATE TABLE fills (
    fill_id         TEXT PRIMARY KEY,     -- venue trade id; duplicates are ignored
    client_order_id TEXT NOT NULL REFERENCES orders(client_order_id),
    asset_key       TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             TEXT NOT NULL,
    price           TEXT NOT NULL,
    fee             TEXT NOT NULL,
    liquidity       TEXT NOT NULL,
    at              TEXT NOT NULL
);

CREATE TABLE positions (
    asset_key       TEXT PRIMARY KEY,
    qty             TEXT NOT NULL,
    cost_basis      TEXT NOT NULL,        -- quote spent incl. fees, reduced pro rata on sells
    stop            TEXT,
    target          TEXT,
    opened_at       TEXT NOT NULL,
    entry_intent_id TEXT,
    realized_pnl    TEXT NOT NULL DEFAULT '0',
    updated_at      TEXT NOT NULL
);

CREATE TABLE balances (
    currency TEXT PRIMARY KEY,
    amount   TEXT NOT NULL CHECK (CAST(amount AS REAL) >= 0)
);

CREATE TABLE cash_flows (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount   TEXT NOT NULL,
    note     TEXT NOT NULL
);

CREATE TABLE risk_state (
    id    INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL
);

CREATE TABLE risk_events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     TEXT NOT NULL,
    kind   TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE equity_marks (
    at       TEXT NOT NULL,
    equity   TEXT NOT NULL,
    cash     TEXT NOT NULL,
    exposure TEXT NOT NULL,
    fees     TEXT NOT NULL
);

CREATE TABLE security_evidence (
    evidence_id TEXT PRIMARY KEY,         -- sha256 of source + raw payload
    source      TEXT NOT NULL,
    url         TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    raw         TEXT NOT NULL
);

CREATE TABLE security_reports (
    report_id      TEXT PRIMARY KEY,
    asset_key      TEXT NOT NULL,
    verdict        TEXT NOT NULL CHECK (verdict IN ('PASS_POLICY', 'BLOCK', 'UNKNOWN')),
    findings       TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    policy_version TEXT NOT NULL
);
CREATE INDEX security_reports_asset ON security_reports(asset_key, created_at);

CREATE TABLE predictions (
    pred_id    TEXT PRIMARY KEY,
    model_id   TEXT NOT NULL,
    asset_key  TEXT NOT NULL,
    at         TEXT NOT NULL,
    horizon_s  INTEGER NOT NULL,
    features   TEXT NOT NULL,
    prob       TEXT NOT NULL
);

CREATE TABLE labels (
    pred_id     TEXT PRIMARY KEY REFERENCES predictions(pred_id),
    label       INTEGER NOT NULL,
    net_return  TEXT NOT NULL,
    labeled_at  TEXT NOT NULL
);

CREATE TABLE models (
    model_id      TEXT PRIMARY KEY,        -- sha256 of the artifact bytes
    created_at    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    params        TEXT NOT NULL,
    metrics       TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('candidate', 'shadow', 'approved', 'retired')),
    artifact_path TEXT NOT NULL,
    feature_reference TEXT NOT NULL
);

CREATE TABLE model_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT NOT NULL,
    at       TEXT NOT NULL,
    kind     TEXT NOT NULL,
    actor    TEXT NOT NULL,
    detail   TEXT NOT NULL
);

CREATE TABLE alerts (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    level   TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE controls (
    name       TEXT PRIMARY KEY,           -- 'block_entries', 'flatten'
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    actor      TEXT NOT NULL
);

CREATE TABLE latency (
    stage TEXT NOT NULL,
    ns    INTEGER NOT NULL,
    at    TEXT NOT NULL
);
