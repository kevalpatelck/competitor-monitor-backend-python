-- ==============================================
-- Competitor Monitor — Database Schema
-- ==============================================

-- Competitor sites (top-level grouping)
CREATE TABLE IF NOT EXISTS sites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    base_url    TEXT    NOT NULL,
    created_at  TEXT    DEFAULT (datetime('now'))
);

-- Individual URLs to monitor (belong to a site)
CREATE TABLE IF NOT EXISTS urls (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id             INTEGER NOT NULL,
    url                 TEXT    NOT NULL UNIQUE,
    page_label          TEXT    NOT NULL,
    price_selector      TEXT,
    price_fallback      TEXT,
    sku_selector        TEXT,
    sku_fallback        TEXT,
    sku_count_method    TEXT    DEFAULT 'elements',
    messaging_selector  TEXT,
    messaging_fallback  TEXT,
    pagination_selector TEXT,
    pagination_fallback TEXT,
    max_pages           INTEGER DEFAULT 1,
    pagination_strategy TEXT    DEFAULT 'none',
    pagination_param    TEXT,
    -- Circuit breaker columns (Python backend upgrade, not in original Node schema)
    consecutive_failures INTEGER DEFAULT 0,
    cooldown_until      TEXT,
    created_at          TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (site_id) REFERENCES sites(id)
);

-- Daily snapshots of extracted data per URL
CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url_id          INTEGER NOT NULL,
    scanned_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    price           TEXT,
    price_currency  TEXT,
    sku_count       INTEGER,
    messaging_text  TEXT,
    raw_html_hash   TEXT,
    scan_success    INTEGER DEFAULT 1,
    error_message   TEXT,
    FOREIGN KEY (url_id) REFERENCES urls(id)
);

-- Detected changes between consecutive snapshots
CREATE TABLE IF NOT EXISTS changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url_id      INTEGER NOT NULL,
    detected_at TEXT    NOT NULL DEFAULT (datetime('now')),
    change_type TEXT    NOT NULL CHECK(change_type IN ('price', 'sku', 'messaging', 'product_price')),
    old_value   TEXT,
    new_value   TEXT,
    summary     TEXT,
    FOREIGN KEY (url_id) REFERENCES urls(id)
);

-- ======== Indexes for fast lookups ========

-- Fast lookup: get latest snapshot for a URL
CREATE INDEX IF NOT EXISTS idx_snapshots_url_date
    ON snapshots(url_id, scanned_at DESC);

-- Fast lookup: get today's changes
CREATE INDEX IF NOT EXISTS idx_changes_detected
    ON changes(detected_at DESC);

-- Fast lookup: changes by URL
CREATE INDEX IF NOT EXISTS idx_changes_url
    ON changes(url_id, detected_at DESC);

-- Individual products parsed from snapshots
CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id     INTEGER NOT NULL,
    url_id          INTEGER NOT NULL,
    name            TEXT    NOT NULL,
    price           TEXT,
    price_currency  TEXT,
    scanned_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE,
    FOREIGN KEY (url_id) REFERENCES urls(id) ON DELETE CASCADE
);

-- Fast lookup: products by snapshot
CREATE INDEX IF NOT EXISTS idx_products_snapshot
    ON products(snapshot_id);
