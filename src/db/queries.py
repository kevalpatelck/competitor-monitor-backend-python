import os
import sqlite3
from typing import List, Dict, Any, Optional
from datetime import datetime
from src.config.env import config

class DBQueries:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or config["db_path"]

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_all_urls(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT u.*, s.name AS site_name, s.base_url AS site_base_url
                FROM urls u
                JOIN sites s ON u.site_id = s.id
                ORDER BY s.name, u.page_label
            """)
            return [dict(row) for row in cursor.fetchall()]

    def get_url_by_id(self, url_id: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM urls WHERE id = ?", (url_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def insert_snapshot(self, snapshot: Dict[str, Any]) -> int:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO snapshots (url_id, scanned_at, price, price_currency, sku_count, messaging_text, raw_html_hash, scan_success, error_message)
                VALUES (?, datetime('now'), ?, ?, ?, ?, ?, ?, ?)
            """, (
                snapshot["url_id"],
                snapshot.get("price"),
                snapshot.get("price_currency"),
                snapshot.get("sku_count"),
                snapshot.get("messaging_text"),
                snapshot.get("raw_html_hash"),
                1 if snapshot.get("success", True) else 0,
                snapshot.get("error_message")
            ))
            conn.commit()
            return cursor.lastrowid

    def get_previous_snapshot(self, url_id: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM snapshots
                WHERE url_id = ? AND scan_success = 1
                ORDER BY scanned_at DESC
                LIMIT 1 OFFSET 1
            """, (url_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_latest_snapshot(self, url_id: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM snapshots
                WHERE url_id = ? AND scan_success = 1
                ORDER BY scanned_at DESC
                LIMIT 1
            """, (url_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def insert_change(self, change: Dict[str, Any]) -> int:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO changes (url_id, detected_at, change_type, old_value, new_value, summary)
                VALUES (?, datetime('now'), ?, ?, ?, ?)
            """, (
                change["url_id"],
                change["change_type"], # Standardized to snake_case change_type
                change.get("old_value"),
                change.get("new_value"),
                change.get("summary")
            ))
            conn.commit()
            return cursor.lastrowid

    def get_todays_changes(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT c.*, u.url, u.page_label, s.name AS site_name
                FROM changes c
                JOIN urls u ON c.url_id = u.id
                JOIN sites s ON u.site_id = s.id
                WHERE date(c.detected_at) = date('now')
                ORDER BY s.name, u.page_label, c.change_type
            """)
            return [dict(row) for row in cursor.fetchall()]

    def get_changes_by_url_id(self, url_id: int) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM changes
                WHERE url_id = ?
                ORDER BY detected_at DESC
                LIMIT 50
            """, (url_id,))
            return [dict(row) for row in cursor.fetchall()]

    def get_all_sites(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sites ORDER BY name")
            return [dict(row) for row in cursor.fetchall()]

    def insert_product(self, product: Dict[str, Any]) -> int:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO products (snapshot_id, url_id, name, price, price_currency, scanned_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
            """, (
                product["snapshot_id"],
                product["url_id"],
                product["name"],
                product.get("price"),
                product.get("price_currency")
            ))
            conn.commit()
            return cursor.lastrowid

    def get_products_by_url_id(self, url_id: int) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT p.*
                FROM products p
                JOIN (
                    SELECT id FROM snapshots 
                    WHERE url_id = ? AND scan_success = 1
                    ORDER BY scanned_at DESC
                    LIMIT 1
                ) s ON p.snapshot_id = s.id
                ORDER BY p.name
            """, (url_id,))
            return [dict(row) for row in cursor.fetchall()]

    def get_products_by_snapshot_id(self, snapshot_id: int) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM products WHERE snapshot_id = ? ORDER BY name", (snapshot_id,))
            return [dict(row) for row in cursor.fetchall()]

    def clear_product_data(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM products")
            cursor.execute("DELETE FROM snapshots")
            conn.commit()

    def clear_changelog(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM changes")
            conn.commit()
