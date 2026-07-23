import os
import json
import sqlite3
import re
from pathlib import Path
from src.config.env import config

def init_database() -> sqlite3.Connection:
    # Ensure the data directory exists
    db_path = Path(config["db_path"])
    db_dir = db_path.parent
    if not db_dir.exists():
        db_dir.mkdir(parents=True, exist_ok=True)
        print(f"[DB] Created data directory: {db_dir}")

    # Open (or create) database
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Performance pragmas
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -64000")
    conn.execute("PRAGMA foreign_keys = ON")

    # Read and run schema.sql
    schema_path = Path(__file__).resolve().parent / "schema.sql"
    with open(schema_path, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    # Strip SQL comments
    clean_sql = re.sub(r"--.*$", "", schema_sql, flags=re.MULTILINE)
    clean_sql = re.sub(r"/\*[\s\S]*?\*/", "", clean_sql)

    # Split by semicolon and run each statement
    statements = [stmt.strip() for stmt in clean_sql.split(";") if stmt.strip()]
    for stmt in statements:
        conn.execute(stmt)
    conn.commit()

    # CREATE IF NOT EXISTS does not add new columns to existing tables — migrate them.
    _run_migrations(conn)

    print(f"[DB] Database initialized at: {db_path}")
    return conn


def _column_names(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str):
    if column not in _column_names(conn, table):
        print(f"[DB] Migrating {table}: adding {column}...")
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _run_migrations(conn: sqlite3.Connection):
    """Apply additive column migrations for DBs created from older schemas."""
    _add_column_if_missing(conn, "urls", "pagination_selector", "pagination_selector TEXT")
    _add_column_if_missing(conn, "urls", "pagination_fallback", "pagination_fallback TEXT")
    _add_column_if_missing(conn, "urls", "max_pages", "max_pages INTEGER DEFAULT 1")
    _add_column_if_missing(conn, "urls", "pagination_strategy", "pagination_strategy TEXT DEFAULT 'none'")
    _add_column_if_missing(conn, "urls", "pagination_param", "pagination_param TEXT")
    _add_column_if_missing(conn, "urls", "consecutive_failures", "consecutive_failures INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "urls", "cooldown_until", "cooldown_until TEXT")

    _add_column_if_missing(conn, "snapshots", "price_currency", "price_currency TEXT")
    _add_column_if_missing(conn, "products", "price_currency", "price_currency TEXT")

    # Ensure changes.change_type CHECK allows product_price and product_name (SQLite cannot ALTER CHECK)
    try:
        conn.execute(
            "INSERT INTO changes (url_id, change_type, old_value, new_value, summary) "
            "VALUES (0, 'product_name', '', '', 'migration test')"
        )
        conn.execute("DELETE FROM changes WHERE url_id = 0 AND summary = 'migration test'")
        conn.commit()
    except sqlite3.IntegrityError:
        print("[DB] Migrating changes table to allow product_price and product_name change_types...")
        conn.execute("ALTER TABLE changes RENAME TO changes_old")
        conn.execute("""
            CREATE TABLE changes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                url_id      INTEGER NOT NULL,
                detected_at TEXT    NOT NULL DEFAULT (datetime('now')),
                change_type TEXT    NOT NULL CHECK(change_type IN ('price', 'sku', 'messaging', 'product_price', 'product_name')),
                old_value   TEXT,
                new_value   TEXT,
                summary     TEXT,
                FOREIGN KEY (url_id) REFERENCES urls(id)
            )
        """)
        conn.execute("""
            INSERT INTO changes (id, url_id, detected_at, change_type, old_value, new_value, summary)
            SELECT id, url_id, detected_at, change_type, old_value, new_value, summary FROM changes_old
        """)
        conn.execute("DROP TABLE changes_old")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_changes_detected ON changes(detected_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_changes_url ON changes(url_id, detected_at DESC)")
        conn.commit()
        print("[DB] changes table migration complete.")

    conn.commit()

def seed_from_config(conn: sqlite3.Connection):
    # Use sites.json from this project's own config directory
    sites_config_path = Path(__file__).resolve().parents[1] / "config" / "sites.json"
    if not sites_config_path.exists():
        print(f"[DB] No sites.json found at {sites_config_path} — skipping seed.")
        return

    with open(sites_config_path, "r", encoding="utf-8") as f:
        sites_config = json.load(f)

    cursor = conn.cursor()
    try:
        for site in sites_config.get("sites", []):
            cursor.execute(
                "INSERT OR IGNORE INTO sites (name, base_url) VALUES (?, ?)",
                (site["name"], site["base_url"])
            )
            
            # Fetch site ID
            cursor.execute("SELECT id FROM sites WHERE name = ?", (site["name"],))
            site_id = cursor.fetchone()["id"]

            for url_config in site.get("urls", []):
                price_sel = url_config.get("selectors", {}).get("price", {})
                sku_sel = url_config.get("selectors", {}).get("sku", {})
                msg_sel = url_config.get("selectors", {}).get("messaging", {})
                pag_sel = url_config.get("selectors", {}).get("pagination", {})

                cursor.execute("""
                    INSERT INTO urls 
                      (site_id, url, page_label, price_selector, price_fallback, 
                       sku_selector, sku_fallback, sku_count_method, 
                       messaging_selector, messaging_fallback,
                       pagination_selector, pagination_fallback, max_pages,
                       pagination_strategy, pagination_param)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                       page_label = excluded.page_label,
                       price_selector = excluded.price_selector,
                       price_fallback = excluded.price_fallback,
                       sku_selector = excluded.sku_selector,
                       sku_fallback = excluded.sku_fallback,
                       sku_count_method = excluded.sku_count_method,
                       messaging_selector = excluded.messaging_selector,
                       messaging_fallback = excluded.messaging_fallback,
                       pagination_selector = excluded.pagination_selector,
                       pagination_fallback = excluded.pagination_fallback,
                       max_pages = excluded.max_pages,
                       pagination_strategy = excluded.pagination_strategy,
                       pagination_param = excluded.pagination_param
                """, (
                    site_id,
                    url_config["url"],
                    url_config["page_label"],
                    price_sel.get("primary") if price_sel else None,
                    price_sel.get("fallback") if price_sel else None,
                    sku_sel.get("primary") if sku_sel else None,
                    sku_sel.get("fallback") if sku_sel else None,
                    sku_sel.get("countMethod", "elements") if sku_sel else "elements",
                    msg_sel.get("primary") if msg_sel else None,
                    msg_sel.get("fallback") if msg_sel else None,
                    pag_sel.get("primary") if pag_sel else None,
                    pag_sel.get("fallback") if pag_sel else None,
                    url_config.get("max_pages", 1),
                    url_config.get("pagination_strategy", "none"),
                    url_config.get("pagination_param")
                ))
        conn.commit()
        print("[DB] Sites and URLs seeded from config.")
    except Exception as e:
        conn.rollback()
        print(f"[DB ERROR] Failed to seed database: {e}")
        raise e

if __name__ == "__main__":
    conn = init_database()
    seed_from_config(conn)
    conn.close()
    print("[DB] Initialization complete.")
