import asyncio
import datetime
import hashlib
import random
import sqlite3
import traceback
import sys
from typing import Dict, Any, List

from src.config.env import config, validate_env
from src.db.init_db import init_database, seed_from_config
from src.db.queries import DBQueries
from src.scraper.reliability import scrape_with_retry
from src.scraper.extract import extract_data
from src.scraper.validate import validate_extraction
from src.diff.numeric_diff import diff_price, diff_sku_count
from src.diff.messaging_diff import batch_analyze_changes
from src.db.export import export_data
from src.reports.report_writer import write_report
from src.utils.logger import logger
from src.scraper.browser import shutdown_browser

def compute_product_list_hash(products: List[Dict[str, Any]], messaging_text: str) -> str:
    sorted_prods = sorted(products, key=lambda p: p["name"])
    serialized_parts = [f"{p['name']}|{p['price']}|{p.get('priceCurrency') or ''}" for p in sorted_prods]
    serialized = "\n".join(serialized_parts) + f"\nMSG:{messaging_text or ''}"
    
    sha256 = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return sha256[:16]

async def run_scan(log_fn=None) -> Dict[str, Any]:
    def log(msg: str):
        logger.info(msg)
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass

    log("========================================")
    log("COMPETITOR MONITOR — Starting daily scan")
    log("========================================")

    start_time = datetime.datetime.now()
    scan_date = start_time.strftime("%A, %B %d, %Y")

    stats = {
        "totalUrls": 0,
        "successfulScans": 0,
        "failedScans": 0,
        "totalChanges": 0,
    }

    all_warnings = []
    changes_by_site = {}

    conn = None
    try:
        # Step 1: Validate environment
        logger.info("[1/6] Validating environment...")
        validate_env()

        # Step 2: Initialize database
        logger.info("[2/6] Initializing database...")
        conn = init_database()
        seed_from_config(conn)
        conn.close() # Queries manager will open connections as needed

        queries = DBQueries()

        # Step 3: Load URLs to scan
        logger.info("[3/6] Loading URLs...")
        urls = queries.get_all_urls()
        stats["totalUrls"] = len(urls)
        logger.info(f"  Found {len(urls)} URLs to scan.")

        if not urls:
            logger.info("  No URLs configured. Check sites.json. Exiting.")
            return {"stats": stats, "warnings": ["No URLs configured"], "changes": {}}

        # Step 4: Group URLs by site
        logger.info("[4/6] Grouping URLs by site for sequential scan...")
        site_groups = {}
        for url_record in urls:
            site_name = url_record["site_name"]
            if site_name not in site_groups:
                site_groups[site_name] = []
            site_groups[site_name].append(url_record)

        # Run sites sequentially (1 at a time) to stay within free tier RAM limits (512MB)
        sem = asyncio.Semaphore(1)

        async def scan_site(site_name: str, site_urls: List[Dict[str, Any]]):
            async with sem:
                for j, url_record in enumerate(site_urls):
                    logger.info(f"\n--- [Site: {site_name}] URL: {url_record['url']} ({url_record['page_label']}) ---")
                    
                    if site_name not in changes_by_site:
                        changes_by_site[site_name] = []

                    try:
                        # Scrape with retry
                        data, scrape_warnings = await scrape_with_retry(
                            url_record=url_record,
                            extract_fn=lambda page: extract_data(page, url_record),
                            logger_fn=log
                        )

                        # Collect warnings
                        if scrape_warnings:
                            all_warnings.extend(scrape_warnings)

                        # Validate extraction
                        validation_warnings = validate_extraction(data, url_record["url"])
                        if validation_warnings:
                            all_warnings.extend(validation_warnings)
                            for w in validation_warnings:
                                logger.info(f"  {w}")

                        if data.get("warnings"):
                            all_warnings.extend(data["warnings"])
                            for w in data["warnings"]:
                                logger.info(f"  {w}")

                        # Skip identical content snapshots
                        latest_snapshot = queries.get_latest_snapshot(url_record["id"])
                        current_hash = compute_product_list_hash(data.get("products") or [], data.get("messagingText") or "")
                        data["rawHtmlHash"] = current_hash

                        if latest_snapshot and latest_snapshot.get("raw_html_hash") == current_hash:
                            logger.info(f"  ℹ️ Content is unchanged (hash: {current_hash}) since last successful snapshot. Skipping DB writes.")
                            stats["successfulScans"] += 1
                            continue

                        # Save snapshot to database
                        snapshot_id = queries.insert_snapshot({
                            "url_id": url_record["id"],
                            "price": data.get("price"),
                            "price_currency": data.get("priceCurrency"),
                            "sku_count": data.get("skuCount"),
                            "messaging_text": data.get("messagingText"),
                            "raw_html_hash": data.get("rawHtmlHash"),
                            "success": True
                        })

                        # Save parsed catalog products to database
                        products = data.get("products") or []
                        if products:
                            logger.info(f"  📦 Saving {len(products)} parsed products to catalog database...")
                            for prod in products:
                                queries.insert_product({
                                    "snapshot_id": snapshot_id,
                                    "url_id": url_record["id"],
                                    "name": prod["name"],
                                    "price": prod["price"],
                                    "price_currency": prod.get("priceCurrency")
                                })
                        else:
                            sku_n = data.get("skuCount") or 0
                            if sku_n:
                                logger.warning(
                                    f"  ⚠️ SKU count is {sku_n} but 0 product rows were parsed "
                                    "(name/price extract failed inside cards). Check selectors."
                                )

                        logger.info(f"  ✅ Snapshot saved | Price: {data.get('price') or 'N/A'} | SKU: {data.get('skuCount') or 'N/A'} | Messaging: {data.get('messagingText')[:50] + '...' if data.get('messagingText') else 'N/A'}")

                        # Compare against previous snapshot
                        prev_snapshot = queries.get_previous_snapshot(url_record["id"])
                        if prev_snapshot:
                            logger.info(f"  Comparing against previous snapshot ({prev_snapshot['scanned_at']})...")
                            
                            pending_changes = []
                            slogan_change = None
                            if prev_snapshot.get("messaging_text") and data.get("messagingText") and prev_snapshot["messaging_text"] != data["messagingText"]:
                                slogan_change = {
                                    "oldText": prev_snapshot["messaging_text"],
                                    "newText": data["messagingText"]
                                }

                            # Price diff
                            price_diff = diff_price(
                                old_price=prev_snapshot.get("price"),
                                new_price=data.get("price"),
                                old_currency=prev_snapshot.get("price_currency"),
                                new_currency=data.get("priceCurrency")
                            )
                            if price_diff:
                                logger.info(f"  💰 PRICE CHANGE: {price_diff['summary']}")
                                pending_changes.append(price_diff)

                            # SKU diff
                            sku_diff = diff_sku_count(prev_snapshot.get("sku_count"), data.get("skuCount"))
                            if sku_diff:
                                logger.info(f"  📦 SKU CHANGE: {sku_diff['summary']}")
                                pending_changes.append(sku_diff)

                            # Product-level additions, removals, and price diffs
                            if products and prev_snapshot.get("id"):
                                prev_products = queries.get_products_by_snapshot_id(prev_snapshot["id"])
                                if prev_products:
                                    prev_by_name = {p["name"]: p for p in prev_products}
                                    new_by_name = {p["name"]: p for p in products}

                                    # 1. Detect price changes for existing products
                                    for new_p in products:
                                        old_p = prev_by_name.get(new_p["name"])
                                        if old_p:
                                            try:
                                                old_pr = float(old_p["price"])
                                                new_pr = float(new_p["price"])
                                                if old_pr != new_pr:
                                                    diff_amount = new_pr - old_pr
                                                    pct = f"{((diff_amount / old_pr) * 100):.1f}" if old_pr != 0 else "N/A"
                                                    direction = "increased" if diff_amount > 0 else "decreased"
                                                    old_curr = old_p.get("price_currency") or data.get("priceCurrency") or ""
                                                    new_curr = new_p.get("priceCurrency") or data.get("priceCurrency") or ""
                                                    sign = "+" if diff_amount > 0 else ""
                                                    
                                                    summary = f'"{new_p["name"]}" price {direction} from {old_curr + " " if old_curr else ""}{old_pr:.2f} to {new_curr + " " if new_curr else ""}{new_pr:.2f} ({sign}{pct}%)'
                                                    pending_changes.append({
                                                        "change_type": "product_price",
                                                        "old_value": f"{new_p['name']}: {old_pr:.2f}",
                                                        "new_value": f"{new_p['name']}: {new_pr:.2f}",
                                                        "summary": summary
                                                    })
                                            except Exception:
                                                pass

                                    # 2. Detect added products (name changed / added)
                                    for new_p in products:
                                        if new_p["name"] not in prev_by_name:
                                            curr = new_p.get("priceCurrency") or data.get("priceCurrency") or ""
                                            price_str = f" ({curr} {new_p['price']})" if new_p.get("price") is not None else ""
                                            pending_changes.append({
                                                "change_type": "product_name",
                                                "old_value": None,
                                                "new_value": new_p["name"],
                                                "summary": f'New product added: "{new_p["name"]}"{price_str}'
                                            })

                                    # 3. Detect removed products (name changed / deleted)
                                    for old_p in prev_products:
                                        if old_p["name"] not in new_by_name:
                                            pending_changes.append({
                                                "change_type": "product_name",
                                                "old_value": old_p["name"],
                                                "new_value": None,
                                                "summary": f'Product removed: "{old_p["name"]}"'
                                            })

                            # Batch analyze slogan and numerical changes using Claude
                            if slogan_change:
                                logger.info("  🤖 Batch analyzing slogan change and other page updates with Claude/AI...")
                                msg_diff = await batch_analyze_changes(
                                    old_text=slogan_change["oldText"],
                                    new_text=slogan_change["newText"],
                                    numeric_diffs=[{"type": c["change_type"], "summary": c["summary"]} for c in pending_changes]
                                )

                                if msg_diff["meaningful"]:
                                    logger.info(f"  📝 MESSAGING CHANGE: {msg_diff['summary']}")
                                    pending_changes.append({
                                        "change_type": "messaging",
                                        "old_value": slogan_change["oldText"][:500],
                                        "new_value": slogan_change["newText"][:500],
                                        "summary": msg_diff["summary"]
                                    })
                                else:
                                    logger.info(f"  📝 Messaging text changed but not meaningful: {msg_diff['summary']}")

                            # Write changes
                            if pending_changes:
                                for change in pending_changes:
                                    queries.insert_change({
                                        "url_id": url_record["id"],
                                        "change_type": change["change_type"],
                                        "old_value": change["old_value"],
                                        "new_value": change["new_value"],
                                        "summary": change["summary"]
                                    })

                                    changes_by_site[site_name].append({
                                        "change_type": change["change_type"],
                                        "old_value": change["old_value"],
                                        "new_value": change["new_value"],
                                        "summary": change["summary"],
                                        "url": url_record["url"],
                                        "page_label": url_record["page_label"]
                                    })
                                    stats["totalChanges"] += 1
                                logger.info(f"  ✅ Batch changes written successfully | Changes count: {len(pending_changes)}")
                            else:
                                logger.info("  ✅ No changes detected.")
                        else:
                            logger.info("  ℹ️ First scan for this URL — no previous data to compare.")

                        stats["successfulScans"] += 1

                    except Exception as err:
                        stats["failedScans"] += 1
                        error_msg = f"FAILED: {url_record['url']} — {err}"
                        logger.error(f"  ❌ {error_msg}")
                        all_warnings.append(error_msg)

                        # Write failed snapshot
                        queries.insert_snapshot({
                            "url_id": url_record["id"],
                            "price": None,
                            "price_currency": None,
                            "sku_count": None,
                            "messaging_text": None,
                            "raw_html_hash": None,
                            "success": False,
                            "error_message": str(err)
                        })

                    # Pause between URLs of same site
                    if j < len(site_urls) - 1:
                        delay_ms = random.randint(config["scrape_delay_min"], config["scrape_delay_max"])
                        logger.info(f"  ⏳ Waiting {(delay_ms / 1000.0):.1f}s before next URL under {site_name}...")
                        await asyncio.sleep(delay_ms / 1000.0)

        # Run all site groups concurrently
        tasks = [scan_site(site_name, site_urls) for site_name, site_urls in site_groups.items()]
        await asyncio.gather(*tasks)

        # Step 5 & 6: Generate reports
        logger.info("\n[5/6] Building comparison report...")
        report_data = {
            "changes": changes_by_site,
            "warnings": all_warnings,
            "stats": stats,
            "scanDate": scan_date
        }

        logger.info("[6/6] Writing report files...")
        try:
            write_report(report_data, logger.info)
            export_data()
        except Exception as report_err:
            logger.error(f"❌ Report generation failed: {report_err}")
            all_warnings.append(f"Report generation failed: {report_err}")

        # Check and send email notifications if enabled
        try:
            import json
            from pathlib import Path
            settings_path = Path(__file__).resolve().parent / "config" / "settings.json"
            if settings_path.exists():
                with open(settings_path, "r", encoding="utf-8") as f:
                    settings = json.load(f)
                
                if settings.get("emailNotifications"):
                    all_detected_changes = []
                    for site_name, site_changes in changes_by_site.items():
                        for chg in site_changes:
                            all_detected_changes.append({
                                "site_name": site_name,
                                "page_label": chg.get("page_label"),
                                "change_type": chg.get("change_type"),
                                "summary": chg.get("summary")
                            })
                    
                    if all_detected_changes:
                        from src.utils.email_notifier import send_changelog_email
                        send_changelog_email(all_detected_changes)
                    else:
                        logger.info("[EMAIL] No changes detected this scan. Skipping email.")
        except Exception as email_err:
            logger.error(f"[EMAIL ERROR] Failed to send email changelog: {email_err}")

    except Exception as fatal_err:
        logger.error(f"FATAL ERROR: {fatal_err}")
        traceback.print_exc()
    finally:
        await shutdown_browser()

    elapsed = (datetime.datetime.now() - start_time).total_seconds()
    logger.info(f"\n========================================")
    logger.info(f"SCAN COMPLETE in {elapsed:.1f}s")
    logger.info(f"  URLs: {stats['totalUrls']} | Success: {stats['successfulScans']} | Failed: {stats['failedScans']}")
    logger.info(f"  Changes detected: {stats['totalChanges']}")
    logger.info(f"  Warnings: {len(all_warnings)}")
    logger.info(f"========================================\n")

    return {
        "stats": stats,
        "warnings": all_warnings,
        "changes": changes_by_site
    }

if __name__ == "__main__":
    asyncio.run(run_scan())
