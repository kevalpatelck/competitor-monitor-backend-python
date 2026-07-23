import sqlite3
from src.config.env import config
from src.db.export import export_data


def simulate_change(logger_fn=None):
    """Mutate latest successful snapshots so the next scan detects diffs."""
    log = logger_fn or print
    db_path = config["db_path"]
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT s.*, u.page_label
            FROM snapshots s
            JOIN urls u ON s.url_id = u.id
            WHERE s.scan_success = 1
            ORDER BY s.scanned_at DESC
        """)
        snapshots = [dict(row) for row in cursor.fetchall()]

        if not snapshots:
            log("[SIMULATION] ⚠️ No successful snapshots found. Run a scan first to establish baselines.")
            return False

        log(f"[SIMULATION] Found {len(snapshots)} snapshots to modify.")
        mutated = 0

        for snap in snapshots:
            page_label = snap.get("page_label")
            snap_id = snap.get("id")

            if page_label == "Science Books":
                cursor.execute("UPDATE snapshots SET price = ? WHERE id = ?", ("99.99", snap_id))
                log("[SIMULATION] 💰 Updated 'Science Books' snapshot price to $99.99.")
                mutated += 1
            elif page_label == "Travel Books":
                cursor.execute("UPDATE snapshots SET sku_count = ? WHERE id = ?", (5, snap_id))
                log("[SIMULATION] 📦 Updated 'Travel Books' snapshot SKU count to 5.")
                mutated += 1
            elif page_label == "Laptop Listing":
                cursor.execute(
                    "UPDATE snapshots SET messaging_text = ? WHERE id = ?",
                    ("Old outdated summer laptops clearance sale pricing terms apply.", snap_id),
                )
                log("[SIMULATION] 🤖 Updated 'Laptop Listing' messaging text to trigger AI comparison.")
                mutated += 1
            elif page_label == "Pr":
                cursor.execute("UPDATE snapshots SET sku_count = ? WHERE id = ?", (10, snap_id))
                log("[SIMULATION] 📦 Updated Flipkart 'Pr' snapshot SKU count to 10.")
                mutated += 1
            elif page_label == "Category Page":
                cursor.execute(
                    "UPDATE snapshots SET messaging_text = ? WHERE id = ?",
                    ("Old Outdated Category Marketing Slogans.", snap_id),
                )
                log("[SIMULATION] 🤖 Updated 'Category Page' messaging text to trigger AI comparison.")
                mutated += 1
            elif page_label == "HOME":
                current_sku = snap.get("sku_count") or 0
                cursor.execute(
                    "UPDATE snapshots SET sku_count = ? WHERE id = ?",
                    (max(0, int(current_sku) - 3), snap_id),
                )
                log(f"[SIMULATION] 📦 Updated 'HOME' snapshot SKU count to trigger a diff.")
                mutated += 1

        # Generic fallback: if no known labels matched, mutate the newest snapshots
        if mutated == 0:
            for idx, snap in enumerate(snapshots[:3]):
                snap_id = snap["id"]
                if idx == 0:
                    current_sku = snap.get("sku_count") or 0
                    cursor.execute(
                        "UPDATE snapshots SET sku_count = ? WHERE id = ?",
                        (max(0, int(current_sku) - 2), snap_id),
                    )
                    log(f"[SIMULATION] 📦 Fallback: reduced SKU count on snapshot {snap_id}.")
                elif idx == 1:
                    cursor.execute(
                        "UPDATE snapshots SET messaging_text = ? WHERE id = ?",
                        ("Simulated outdated marketing copy for change detection.", snap_id),
                    )
                    log(f"[SIMULATION] 🤖 Fallback: updated messaging on snapshot {snap_id}.")
                else:
                    cursor.execute(
                        "UPDATE snapshots SET price = ? WHERE id = ?",
                        ("9999.99", snap_id),
                    )
                    log(f"[SIMULATION] 💰 Fallback: updated price on snapshot {snap_id}.")
                mutated += 1

        conn.commit()
        export_data()
        log(f"[SIMULATION] ✅ Simulated values written ({mutated} snapshot(s)) and data.json updated.")
        return True

    except Exception as err:
        log(f"[SIMULATION] ❌ Error simulating changes: {err}")
        raise err
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    simulate_change()
