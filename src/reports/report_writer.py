import os
import json
import datetime
from pathlib import Path
from typing import Dict, Any, List
from src.config.env import config

def ensure_reports_dir():
    reports_dir = Path(config["reports_dir"])
    if not reports_dir.exists():
        reports_dir.mkdir(parents=True, exist_ok=True)

def escape_html(str_val: Any) -> str:
    if str_val is None:
        return ""
    return str(str_val).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def get_icon(change_type: str) -> str:
    return {"price": "💰", "sku": "📦", "messaging": "📝", "product_price": "🏷️"}.get(change_type, "🔔")

def build_report_html(report_data: Dict[str, Any]) -> str:
    changes = report_data.get("changes", {})
    warnings = report_data.get("warnings", [])
    stats = report_data.get("stats", {})
    scan_date = report_data.get("scanDate", str(datetime.date.today()))

    # Build changes HTML
    changes_html = ""
    site_names = list(changes.keys())
    if not site_names:
        changes_html = '<div class="no-changes">✅ No changes detected across all monitored sites.</div>'
    else:
        for site_name in site_names:
            site_changes = changes[site_name]
            changes_html += f"""<div class="site-group">
      <div class="site-name">🏢 {escape_html(site_name)}</div>"""

            if not site_changes:
                changes_html += '<div class="no-changes">✅ No changes detected</div>'
            else:
                for change in site_changes:
                    c_type = change.get("change_type", "price")
                    badge_class = c_type
                    changes_html += f"""
      <div class="change-card {escape_html(badge_class)}">
        <div class="change-badge {escape_html(badge_class)}">{get_icon(c_type)} {escape_html(c_type)}</div>
        <div class="change-page">{escape_html(change.get("page_label"))} — {escape_html(change.get("url"))}</div>
        <div class="change-summary">{escape_html(change.get("summary"))}</div>
      </div>"""
            changes_html += "</div>"

    # Build warnings HTML
    warnings_html = ""
    if warnings:
        warnings_cards = "".join(f'<div class="warning-card"><div class="warning-text">{escape_html(w)}</div></div>\n      ' for w in warnings)
        warnings_html = f"""
    <div class="section">
      <div class="section-title">⚠️ Warnings ({len(warnings)})</div>
      {warnings_cards}
    </div>"""

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    generated_time = now_utc.strftime("%H:%M:%S")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Competitor Monitor — Comparison Report — {escape_html(scan_date)}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; padding: 32px; }}
    .container {{ max-width: 900px; margin: 0 auto; }}
    .header {{ background: linear-gradient(135deg, #1e293b, #334155); border-radius: 16px; padding: 32px 40px; margin-bottom: 24px; border: 1px solid #475569; }}
    .header h1 {{ font-size: 26px; font-weight: 700; color: #f8fafc; margin-bottom: 4px; }}
    .header .date {{ color: #94a3b8; font-size: 14px; }}
    .stats-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }}
    .stat-card {{ background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 20px; text-align: center; }}
    .stat-num {{ font-size: 32px; font-weight: 800; color: #f8fafc; }}
    .stat-num.success {{ color: #22c55e; }}
    .stat-num.fail {{ color: #ef4444; }}
    .stat-num.changes {{ color: #f59e0b; }}
    .stat-label {{ font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: #64748b; margin-top: 4px; }}
    .section {{ background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 24px 28px; margin-bottom: 20px; }}
    .section-title {{ font-size: 18px; font-weight: 600; color: #f8fafc; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }}
    .site-group {{ margin-bottom: 20px; }}
    .site-name {{ font-size: 15px; font-weight: 600; color: #94a3b8; padding-bottom: 8px; border-bottom: 1px solid #334155; margin-bottom: 12px; }}
    .change-card {{ background: #0f172a; border-radius: 8px; padding: 14px 18px; margin-bottom: 10px; border-left: 4px solid #3b82f6; }}
    .change-card.price {{ border-left-color: #ef4444; }}
    .change-card.sku {{ border-left-color: #f59e0b; }}
    .change-card.messaging {{ border-left-color: #a855f7; }}
    .change-card.product_price {{ border-left-color: #f43f5e; }}
    .change-badge {{ display: inline-block; font-size: 10px; text-transform: uppercase; font-weight: 700; letter-spacing: 0.8px; padding: 2px 8px; border-radius: 4px; margin-bottom: 6px; }}
    .change-badge.price {{ background: rgba(239,68,68,0.15); color: #f87171; }}
    .change-badge.sku {{ background: rgba(245,158,11,0.15); color: #fbbf24; }}
    .change-badge.messaging {{ background: rgba(168,85,247,0.15); color: #c084fc; }}
    .change-badge.product_price {{ background: rgba(244,63,94,0.15); color: #fb7185; }}
    .change-page {{ font-size: 12px; color: #64748b; margin-bottom: 4px; }}
    .change-summary {{ font-size: 14px; color: #cbd5e1; line-height: 1.5; }}
    .no-changes {{ color: #22c55e; font-size: 14px; padding: 8px 0; }}
    .warning-card {{ background: rgba(245,158,11,0.08); border: 1px solid rgba(245,158,11,0.3); border-radius: 8px; padding: 14px 18px; margin-bottom: 8px; }}
    .warning-text {{ font-size: 13px; color: #fbbf24; }}
    .footer {{ text-align: center; color: #475569; font-size: 12px; margin-top: 32px; padding-top: 16px; border-top: 1px solid #1e293b; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>🔍 Competitor Comparison Report</h1>
      <div class="date">{escape_html(scan_date)} — Generated at {generated_time}</div>
    </div>

    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-num">{stats.get("totalUrls", 0)}</div>
        <div class="stat-label">URLs Scanned</div>
      </div>
      <div class="stat-card">
        <div class="stat-num changes">{stats.get("totalChanges", 0)}</div>
        <div class="stat-label">Changes Found</div>
      </div>
      <div class="stat-card">
        <div class="stat-num success">{stats.get("successfulScans", 0)}</div>
        <div class="stat-label">Successful</div>
      </div>
      <div class="stat-card">
        <div class="stat-num fail">{stats.get("failedScans", 0)}</div>
        <div class="stat-label">Failed</div>
      </div>
    </div>

    <div class="section">
      <div class="section-title">📊 Changes by Site</div>
      {changes_html}
    </div>

    {warnings_html}

    <div class="footer">
      Competitor Monitor v1.0 — Auto-generated comparison report
    </div>
  </div>
</body>
</html>"""

def write_report(report_data: Dict[str, Any], logger_fn=None) -> Dict[str, str]:
    log = logger_fn or print
    ensure_reports_dir()

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    html_filename = f"report-{timestamp}.html"
    json_filename = f"report-{timestamp}.json"

    reports_dir = Path(config["reports_dir"])
    html_path = reports_dir / html_filename
    json_path = reports_dir / json_filename

    # HTML Report
    html_content = build_report_html(report_data)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    log(f"[REPORT] ✅ HTML report saved: {html_path}")

    # JSON Report
    json_report = {
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + f"{datetime.datetime.now(datetime.timezone.utc).microsecond // 1000:03d}Z",
        "scanDate": report_data.get("scanDate"),
        "stats": report_data.get("stats"),
        "changes": report_data.get("changes"),
        "warnings": report_data.get("warnings"),
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_report, f, indent=2)
    log(f"[REPORT] ✅ JSON report saved: {json_path}")

    return {
        "htmlPath": str(html_path),
        "jsonPath": str(json_path)
    }
