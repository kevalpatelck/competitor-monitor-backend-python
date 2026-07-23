import sqlite3

conn = sqlite3.connect("data/monitoring.db")
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT url, page_label, max_pages, pagination_strategy, pagination_param FROM urls").fetchall()
for r in rows:
    print(f"page_label={r['page_label']}, max_pages={r['max_pages']}, strategy={r['pagination_strategy']}, param={r['pagination_param']}")
conn.close()
