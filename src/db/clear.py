import os
import sys
import sqlite3
from src.config.env import config
from src.db.init_db import init_database

def clear_database(all_tables: bool = False):
    db_path = config["db_path"]
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        cursor = conn.cursor()
        
        if all_tables:
            print("[DB] Clearing all tables (Sites, URLs, Snapshots, and Changes)...")
            cursor.execute("DELETE FROM changes")
            cursor.execute("DELETE FROM products")
            cursor.execute("DELETE FROM snapshots")
            cursor.execute("DELETE FROM urls")
            cursor.execute("DELETE FROM sites")
            conn.commit()
            print("[DB] Database fully cleared.")
        else:
            print("[DB] Clearing scraped snapshot history and changes changelog...")
            cursor.execute("DELETE FROM changes")
            cursor.execute("DELETE FROM products")
            cursor.execute("DELETE FROM snapshots")
            conn.commit()
            print("[DB] Snapshot history and changes cleared. (Sites and URLs configurations kept).")
            
        # Re-export empty data structure
        from src.db.export import export_data
        export_data()
        
    except Exception as err:
        print(f"[DB ERROR] Failed to clear database: {err}")
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    clear_all = "--all" in sys.argv
    clear_database(clear_all)
