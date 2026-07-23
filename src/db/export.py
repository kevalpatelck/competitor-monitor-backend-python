import os
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from src.config.env import config
from src.utils.logger import logger

def export_data(output_path: str = None) -> str:
    # We will write to both dashboard and dashboardd public/data.json to keep them synchronized
    root_dir = Path(__file__).resolve().parents[3]
    default_paths = [
        root_dir / "competitor-monitor-dashboardd" / "public" / "data.json",
        root_dir / "competitor-monitor-dashboard" / "public" / "data.json",
        root_dir / "New design" / "public" / "data.json",
    ]
    
    conn = None
    try:
        conn = sqlite3.connect(config["db_path"])
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 1. Fetch sites and URLs
        cursor.execute("SELECT * FROM sites")
        sites = [dict(row) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT u.*, s.name AS site_name 
            FROM urls u 
            JOIN sites s ON u.site_id = s.id
        """)
        urls = [dict(row) for row in cursor.fetchall()]

        # 2. Fetch latest snapshot per URL
        cursor.execute("""
            SELECT s1.*, u.url, u.page_label, st.name AS site_name
            FROM snapshots s1
            JOIN urls u ON s1.url_id = u.id
            JOIN sites st ON u.site_id = st.id
            WHERE s1.id = (
                SELECT s2.id 
                FROM snapshots s2 
                WHERE s2.url_id = s1.url_id 
                ORDER BY s2.scanned_at DESC 
                LIMIT 1
            )
        """)
        latest_snapshots = [dict(row) for row in cursor.fetchall()]

        # 3. Fetch all changes (limiting to 100)
        cursor.execute("""
            SELECT c.*, u.url, u.page_label, s.name AS site_name
            FROM changes c
            JOIN urls u ON c.url_id = u.id
            JOIN sites s ON u.site_id = s.id
            ORDER BY c.detected_at DESC
            LIMIT 100
        """)
        changes = [dict(row) for row in cursor.fetchall()]

        # 4. Fetch snapshot history for charts (using snapshot price or calculated product average price)
        cursor.execute("""
            SELECT 
                s.scanned_at, 
                s.url_id, 
                COALESCE(s.price, (SELECT ROUND(AVG(p.price), 2) FROM products p WHERE p.snapshot_id = s.id AND p.price IS NOT NULL)) AS price, 
                s.price_currency, 
                s.sku_count, 
                u.page_label, 
                st.name AS site_name
            FROM snapshots s
            JOIN urls u ON s.url_id = u.id
            JOIN sites st ON u.site_id = st.id
            WHERE s.scan_success = 1
            ORDER BY s.scanned_at ASC
        """)
        history = [dict(row) for row in cursor.fetchall()]

        # 5. Fetch individual product catalog from latest successful snapshots
        cursor.execute("""
            SELECT p.*, u.page_label, st.name AS site_name, u.url
            FROM products p
            JOIN snapshots s ON p.snapshot_id = s.id
            JOIN urls u ON s.url_id = u.id
            JOIN sites st ON u.site_id = st.id
            WHERE s.id IN (
                SELECT s2.id
                FROM snapshots s2
                WHERE s2.url_id = s.url_id AND s2.scan_success = 1
                ORDER BY s2.scanned_at DESC
                LIMIT 1
            )
            ORDER BY st.name, p.name
        """)
        latest_products = [dict(row) for row in cursor.fetchall()]

        # Calculate stats
        stats = {
            "totalSites": len(sites),
            "totalUrls": len(urls),
            "totalChanges": len(changes),
            "successfulScans": sum(1 for s in latest_snapshots if s.get("scan_success") == 1),
            "failedScans": sum(1 for s in latest_snapshots if s.get("scan_success") == 0),
            "lastScanTime": latest_snapshots[0]["scanned_at"] if latest_snapshots else None,
        }

        now_utc = datetime.now(timezone.utc)
        exported_at = now_utc.strftime('%Y-%m-%dT%H:%M:%S.') + f"{now_utc.microsecond // 1000:03d}Z"

        payload = {
            "exportedAt": exported_at,
            "stats": stats,
            "sites": sites,
            "urls": urls,
            "latestSnapshots": latest_snapshots,
            "latestProducts": latest_products,
            "changes": changes,
            "history": history,
        }

        paths_to_write = [Path(output_path)] if output_path else default_paths
        for p in paths_to_write:
            p_dir = p.parent
            if not p_dir.exists():
                p_dir.mkdir(parents=True, exist_ok=True)
            
            with open(p, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            logger.info(f"[EXPORT] Database successfully exported to: {p}")

        return str(paths_to_write[0])

    except Exception as err:
        logger.error(f"[EXPORT] Failed to export database: {err}")
        raise err
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    export_data()
