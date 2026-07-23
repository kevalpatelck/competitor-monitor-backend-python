import asyncio
import datetime
import sqlite3
from typing import Dict, Any, List, Callable, Tuple
from src.config.env import config
from src.scraper.browser import launch_stealth_browser, visit_page
from src.scraper.validate import is_blocked_page
from src.scraper.humanize import random_delay
from src.scraper.sitemap_fallback import detect_bot_protection, process_bot_protected_url

# Config constants matching Node scraperConfig
RETRY_COUNT = 3
BACKOFF_BASE_MS = 3000
MAX_CONSECUTIVE_FAILURES = 5
COOLDOWN_DURATION_MS = 3600000  # 1 hour

async def scrape_with_retry(
    url_record: Dict[str, Any],
    extract_fn: Callable,
    db_path: str = None,
    logger_fn: Callable = print
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Compliant scrape executor with standard retry ladder for transient failures
    (timeouts, 5xx, DNS errors).
    Does NOT escalate with proxy rotation, fingerprint evasion, or CAPTCHA solving.
    """
    log = logger_fn
    db_file = db_path or config["db_path"]
    max_retries = config.get("scrape_max_retries", RETRY_COUNT)
    warnings = []

    url_id = url_record.get("id")

    # 1. Check Circuit Breaker Cooldown status
    if url_id:
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT consecutive_failures, cooldown_until FROM urls WHERE id = ?", (url_id,))
        row = cursor.fetchone()
        conn.close()

        if row and row["cooldown_until"]:
            try:
                cooldown_time = datetime.datetime.fromisoformat(row["cooldown_until"].replace("Z", "+00:00"))
                now = datetime.datetime.now(datetime.timezone.utc)
                if now < cooldown_time:
                    remaining_mins = int((cooldown_time - now).total_seconds() / 60)
                    cooldown_msg = f"[CIRCUIT BREAKER] URL {url_record['url']} is in cooldown for another {remaining_mins} mins. Skipping scan."
                    log(f"  {cooldown_msg}")
                    raise Exception(cooldown_msg)
                # Cooldown has expired, reset it
                conn = sqlite3.connect(db_file)
                conn.execute("UPDATE urls SET cooldown_until = NULL WHERE id = ?", (url_id,))
                conn.commit()
                conn.close()
            except Exception as e:
                if "CIRCUIT BREAKER" in str(e):
                    raise
                # Invalid cooldown timestamp — clear and continue
                log(f"  [CIRCUIT BREAKER] Ignoring invalid cooldown_until value: {e}")
                conn = sqlite3.connect(db_file)
                conn.execute("UPDATE urls SET cooldown_until = NULL WHERE id = ?", (url_id,))
                conn.commit()
                conn.close()

    # 2. Compliant Scrape Retry Ladder (Plain backoff for transient network errors only)
    for attempt in range(1, max_retries + 1):
        browser = None
        context = None
        page = None
        try:
            log(f"  [Attempt {attempt}/{max_retries}] Scraping: {url_record['url']}")
            
            # Plain backoff for transient retries — no proxy rotation or fingerprint patching
            if attempt > 1:
                delay = BACKOFF_BASE_MS * (2 ** (attempt - 2))
                log(f"  [Attempt {attempt}] Backoff delay for {delay}ms...")
                await random_delay(delay, delay + 1500)

            # Launch Playwright browser
            launched = await launch_stealth_browser()
            browser = launched["browser"]
            context = launched["context"]
            
            status_code = None
            try:
                page = await visit_page(context, url_record["url"])
            except Exception as nav_err:
                if "403" in str(nav_err) or "Access Denied" in str(nav_err):
                    status_code = 403
                else:
                    raise nav_err

            # Inspect page content for Bot Protection
            page_content = await page.content() if page else ""
            sku_selector = url_record.get("sku_selector") or url_record.get("sku_fallback") or "div[id^='product-']"
            card_count = await page.locator(sku_selector).count() if page else 0
            block_check = detect_bot_protection(status_code, page_content, url_record["url"], card_count=card_count)

            if not block_check["blocked"]:
                legacy_check = is_blocked_page(page_content, url_record["url"])
                if legacy_check["blocked"]:
                    block_check = {
                        "blocked": True,
                        "block_type": "Security Block",
                        "reason": legacy_check["reason"]
                    }

            # --- SCRAPING FALLBACK POLICY ENFORCEMENT ---
            if block_check["blocked"]:
                if context:
                    await context.close()
                context = None
                browser = None

                # Rule 3a: STOP retrying directly & SWITCH to sitemap-based fallback path
                fallback_data = process_bot_protected_url(
                    url_record=url_record,
                    block_info=block_check,
                    logger_fn=log
                )
                return fallback_data, fallback_data.get("warnings", [])

            # Extract product data cleanly
            data = await extract_fn(page)
            
            if context:
                await context.close()
            context = None
            browser = None

            # Reset circuit breaker on success
            if url_id:
                conn = sqlite3.connect(db_file)
                conn.execute("UPDATE urls SET consecutive_failures = 0, cooldown_until = NULL WHERE id = ?", (url_id,))
                conn.commit()
                conn.close()

            return data, warnings

        except Exception as err:
            warnings.append(f"Attempt {attempt}: {err}")
            log(f"  [ERROR] Attempt {attempt} failed: {err}")
            if context:
                await context.close()
            context = None
            browser = None

    # 3. Record failure and trigger cooldown
    if url_id:
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT consecutive_failures FROM urls WHERE id = ?", (url_id,))
        row = cursor.fetchone()
        
        new_failures = (row["consecutive_failures"] if row else 0) + 1
        cooldown_until = None
        if new_failures >= MAX_CONSECUTIVE_FAILURES:
            cooldown_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(milliseconds=COOLDOWN_DURATION_MS)
            cooldown_until = cooldown_dt.isoformat().replace("+00:00", "Z")
            log(f"  [CIRCUIT BREAKER] URL {url_record['url']} hit consecutive failure limit ({new_failures}/{MAX_CONSECUTIVE_FAILURES}). Entering cooldown until {cooldown_until}")
        
        conn.execute("UPDATE urls SET consecutive_failures = ?, cooldown_until = ? WHERE id = ?", (new_failures, cooldown_until, url_id))
        conn.commit()
        conn.close()

    raise Exception(f"All {max_retries} scrape attempts failed for {url_record['url']}. Errors: {warnings}")
