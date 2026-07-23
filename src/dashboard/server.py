import json
import asyncio
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Header, Depends, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.config.env import config
from src.pipeline import run_scan
from src.db.simulate import simulate_change
from src.db.export import export_data
from src.db.init_db import init_database, seed_from_config
from src.db.queries import DBQueries
from src.scraper.browser import launch_stealth_browser, apply_stealth_to_page
from src.scraper.extract import extract_data
from src.scraper.ai_selector import discover_selectors
from src.scraper.route_discover import auto_discover_site_urls
from src.scraper.pagination_detector import build_next_page_url
from src.utils.logger import logger

app = FastAPI(title="Competitor Monitor API Server")

# Security configuration (CORS)
cors_origins = list(set([
    "http://localhost:4028",
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:4028",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:3000",
] + (config.get("allowed_origins") or [])))

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# File Paths
root_dir = Path(__file__).resolve().parents[3]
# Resolve data.json dynamically checking competitor-monitor-dashboardd first, then competitor-monitor-dashboard
DATA_JSON_PATH = root_dir / "competitor-monitor-dashboardd" / "public" / "data.json"
if not DATA_JSON_PATH.exists():
    _alt_path = root_dir / "competitor-monitor-dashboard" / "public" / "data.json"
    if _alt_path.exists():
        DATA_JSON_PATH = _alt_path
    else:
        _alt_path2 = root_dir / "New design" / "public" / "data.json"
        if _alt_path2.exists():
            DATA_JSON_PATH = _alt_path2

SITES_JSON_PATH = Path(__file__).resolve().parents[1] / "config" / "sites.json"
SETTINGS_JSON_PATH = Path(__file__).resolve().parents[1] / "config" / "settings.json"

# State variables
is_scanning = False
is_pre_scanning = False
scan_started_at: Optional[float] = None
SCAN_LOCK_TIMEOUT_SEC = 180  # auto-clear stuck locks after 3 minutes
scheduler = AsyncIOScheduler()
auto_scan_job_id = "auto_scan_job"


# --- Error responses match Node dashboard contract: { "error": "..." } ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, list):
        message = "; ".join(str(item) for item in detail)
    elif detail is None or detail == "":
        message = f"Request failed with status {exc.status_code}"
    else:
        message = str(detail)
    logger.error(f"[HTTP {exc.status_code}] {request.method} {request.url.path} → {message}")
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": message, "detail": message},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    messages = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", []))
        messages.append(f"{loc}: {err.get('msg', 'invalid')}")
    message = "; ".join(messages) or "Validation error"
    logger.error(f"[HTTP 422] {request.method} {request.url.path} → {message}")
    return JSONResponse(status_code=422, content={"error": message, "detail": message})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    import traceback
    tb = traceback.format_exc()
    message = str(exc).strip() or f"{type(exc).__name__}: unknown failure"
    logger.error(f"[UNHANDLED] {request.method} {request.url.path} → {message}\n{tb}")
    return JSONResponse(
        status_code=500,
        content={"error": message, "detail": message},
    )

# Dependencies
async def verify_api_key(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    expected_key = config.get("backend_api_key")
    if not expected_key:
        return
    if x_api_key != expected_key:
        raise HTTPException(status_code=403, detail="Unauthorized request: Invalid or missing X-API-Key.")


# Schemas
class SiteUrlItem(BaseModel):
    url: str
    pageLabel: str
    maxPages: Optional[int] = 1
    paginationStrategy: Optional[str] = "click"
    paginationParam: Optional[str] = None
    priceSelector: Optional[str] = None
    priceFallback: Optional[str] = None
    skuSelector: Optional[str] = None
    skuFallback: Optional[str] = None
    skuCountMethod: Optional[str] = None
    messagingSelector: Optional[str] = None
    messagingFallback: Optional[str] = None
    paginationSelector: Optional[str] = None
    paginationFallback: Optional[str] = None

class AddSitePayload(BaseModel):
    siteName: str
    baseUrl: str
    url: Optional[str] = None
    pageLabel: Optional[str] = None
    priceSelector: Optional[str] = None
    priceFallback: Optional[str] = None
    skuSelector: Optional[str] = None
    skuFallback: Optional[str] = None
    skuCountMethod: Optional[str] = None
    messagingSelector: Optional[str] = None
    messagingFallback: Optional[str] = None
    paginationSelector: Optional[str] = None
    paginationFallback: Optional[str] = None
    maxPages: Optional[int] = None
    urls: Optional[List[SiteUrlItem]] = None

class SettingsPayload(BaseModel):
    autoScan: Optional[bool] = None
    intervalMinutes: Optional[int] = None
    emailNotifications: Optional[bool] = None

class CheckSitePayload(BaseModel):
    url: str
    priceSelector: Optional[str] = None
    priceFallback: Optional[str] = None
    skuSelector: Optional[str] = None
    skuFallback: Optional[str] = None
    skuCountMethod: Optional[str] = None
    messagingSelector: Optional[str] = None
    messagingFallback: Optional[str] = None

class DiscoverSelectorsPayload(BaseModel):
    url: str

class DiscoverRoutesPayload(BaseModel):
    url: str

class UpdateUrlPayload(BaseModel):
    price_selector: Optional[str] = None
    price_fallback: Optional[str] = None
    sku_selector: Optional[str] = None
    sku_fallback: Optional[str] = None
    sku_count_method: Optional[str] = None
    messaging_selector: Optional[str] = None
    messaging_fallback: Optional[str] = None
    pagination_selector: Optional[str] = None
    pagination_fallback: Optional[str] = None
    pagination_strategy: Optional[str] = None
    pagination_param: Optional[str] = None
    max_pages: Optional[int] = None
    page_label: Optional[str] = None


# Helpers
def read_data_json() -> Optional[Dict[str, Any]]:
    try:
        path = DATA_JSON_PATH
        if not path.exists():
            # Dynamically look for alternative path if data.json was written after startup
            root_dir = Path(__file__).resolve().parents[3]
            alt_path = root_dir / "competitor-monitor-dashboard" / "public" / "data.json"
            if alt_path.exists():
                path = alt_path
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error reading data.json: {e}")
    return None


def _clear_stale_scan_lock() -> bool:
    """Clear is_scanning if a previous scan hung past the timeout. Returns True if cleared."""
    global is_scanning, scan_started_at
    if not is_scanning:
        return False
    if scan_started_at and (time.time() - scan_started_at) > SCAN_LOCK_TIMEOUT_SEC:
        elapsed = int(time.time() - scan_started_at)
        logger.warning(f"[SCAN LOCK] Stale lock cleared after {elapsed}s (timeout={SCAN_LOCK_TIMEOUT_SEC}s).")
        is_scanning = False
        scan_started_at = None
        return True
    return False


def _acquire_scan_lock(source: str) -> bool:
    global is_scanning, scan_started_at
    _clear_stale_scan_lock()
    if is_scanning:
        elapsed = int(time.time() - scan_started_at) if scan_started_at else 0
        logger.info(f"[{source}] Scan skipped: previous scan still running ({elapsed}s).")
        return False
    is_scanning = True
    scan_started_at = time.time()
    return True


def _release_scan_lock():
    global is_scanning, scan_started_at
    is_scanning = False
    scan_started_at = None


def _scan_busy_message() -> str:
    elapsed = int(time.time() - scan_started_at) if scan_started_at else 0
    return (
        f"A competitor scan is already in progress ({elapsed}s so far). "
        "This often starts automatically after adding a site — wait for it to finish, then try again."
    )


async def run_scan_with_lock(source: str = "API") -> None:
    """Run a full scan while holding the is_scanning mutex."""
    if not _acquire_scan_lock(source):
        return
    try:
        await run_scan()
    finally:
        _release_scan_lock()


async def trigger_scheduled_scan():
    if not _acquire_scan_lock("SCHEDULER"):
        return
    logger.info("[SCHEDULER] Auto Scan triggered...")
    try:
        await run_scan()
        logger.info("[SCHEDULER] Auto Scan completed successfully.")
    except Exception as err:
        logger.error(f"[SCHEDULER ERROR] Auto Scan failed: {err}")
    finally:
        _release_scan_lock()


def start_auto_scan_timer(minutes: int):
    if not scheduler.running:
        try:
            scheduler.start()
        except Exception:
            pass
    if scheduler.get_job(auto_scan_job_id):
        scheduler.remove_job(auto_scan_job_id)
    logger.info(f"[SCHEDULER] Auto Scan timer started: executing scan every {minutes} minutes")
    scheduler.add_job(
        trigger_scheduled_scan,
        "interval",
        minutes=minutes,
        id=auto_scan_job_id
    )


def stop_auto_scan_timer():
    if scheduler.get_job(auto_scan_job_id):
        scheduler.remove_job(auto_scan_job_id)
        logger.info("[SCHEDULER] Auto Scan timer stopped.")


# Server Startup
@app.on_event("startup")
def on_startup():
    try:
        logger.info("[DB] Initializing/migrating database on startup...")
        conn = init_database()
        conn.close()
    except Exception as err:
        logger.error(f"[DB ERROR] Database initialization failed: {err}")

    settings = {"autoScan": False, "intervalMinutes": 10}
    try:
        if SETTINGS_JSON_PATH.exists():
            with open(SETTINGS_JSON_PATH, "r", encoding="utf-8") as f:
                settings = json.load(f)
        else:
            SETTINGS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(SETTINGS_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2)
    except Exception as e:
        logger.error(f"Error loading settings: {e}")

    scheduler.start()
    if settings.get("autoScan") and settings.get("intervalMinutes", 0) > 0:
        start_auto_scan_timer(settings["intervalMinutes"])


@app.on_event("shutdown")
def on_shutdown():
    scheduler.shutdown()


# Endpoints
@app.get("/api/data", dependencies=[Depends(verify_api_key)])
def get_data():
    current_data = read_data_json()
    if current_data:
        return current_data
    raise HTTPException(status_code=404, detail="data.json not found. Run a scan first.")


@app.post("/api/scan", dependencies=[Depends(verify_api_key)])
async def trigger_manual_scan():
    """Await full scan and return fresh dashboard data (Node parity)."""
    if not _acquire_scan_lock("API"):
        raise HTTPException(status_code=409, detail=_scan_busy_message())

    logger.info("[API] Triggering manual competitor scan...")
    try:
        await run_scan()
        fresh_data = read_data_json()
        return {"success": True, "message": "Scan completed successfully!", "data": fresh_data}
    except Exception as err:
        logger.error(f"[API ERROR] Scan failed: {err}")
        raise HTTPException(status_code=500, detail=f"Scan failed: {err}")
    finally:
        _release_scan_lock()


@app.get("/api/scan-stream")
async def scan_stream(api_key: Optional[str] = None, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    """Stream live scan logs via Server-Sent Events (SSE)."""
    expected_key = config.get("backend_api_key")
    provided = api_key or x_api_key
    if expected_key and provided != expected_key:
        raise HTTPException(status_code=403, detail="Unauthorized request: Invalid or missing X-API-Key.")

    if not _acquire_scan_lock("API_STREAM"):
        raise HTTPException(status_code=409, detail=_scan_busy_message())

    async def event_generator():
        log_queue = asyncio.Queue()

        def stream_logger(msg: str):
            logger.info(msg)
            try:
                loop = asyncio.get_running_loop()
                loop.call_soon_threadsafe(log_queue.put_nowait, str(msg))
            except Exception:
                pass

        scan_task = asyncio.create_task(run_scan(log_fn=stream_logger))

        try:
            while not scan_task.done() or not log_queue.empty():
                try:
                    msg = await asyncio.wait_for(log_queue.get(), timeout=0.3)
                    yield f"data: {json.dumps({'log': msg})}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'heartbeat': True})}\n\n"

            if scan_task.exception():
                err_msg = str(scan_task.exception())
                yield f"data: {json.dumps({'error': err_msg})}\n\n"
            else:
                fresh_data = read_data_json()
                yield f"data: {json.dumps({'done': True, 'data': fresh_data})}\n\n"
        finally:
            _release_scan_lock()

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/force-unlock-scan", dependencies=[Depends(verify_api_key)])
def force_unlock_scan():
    global is_scanning, is_pre_scanning, scan_started_at
    is_scanning = False
    is_pre_scanning = False
    scan_started_at = None
    logger.info("[SCAN LOCK] Force unlocked scan lock manually via API.")
    return {"success": True, "message": "Scan lock successfully cleared!"}


@app.get("/api/scan-status", dependencies=[Depends(verify_api_key)])
def scan_status():
    _clear_stale_scan_lock()
    elapsed = int(time.time() - scan_started_at) if is_scanning and scan_started_at else 0
    job = scheduler.get_job(auto_scan_job_id)
    if not job:
        try:
            settings = get_settings()
            if settings.get("autoScan") and settings.get("intervalMinutes", 0) > 0:
                start_auto_scan_timer(settings["intervalMinutes"])
                job = scheduler.get_job(auto_scan_job_id)
        except Exception as err:
            logger.error(f"Error starting timer in scan_status: {err}")

    next_run = None
    if job and getattr(job, 'next_run_time', None):
        next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")

    return {
        "isScanning": is_scanning,
        "isPreScanning": is_pre_scanning,
        "elapsedSeconds": elapsed,
        "nextRunTime": next_run,
        "autoScanEnabled": True if job else False,
    }


@app.post("/api/pre-scan-count", dependencies=[Depends(verify_api_key)])
async def pre_scan_count():
    global is_pre_scanning
    _clear_stale_scan_lock()
    if is_pre_scanning:
        raise HTTPException(status_code=409, detail="A pre-scan count is already in progress.")
    if is_scanning:
        # Do not share the Playwright browser with an active full scan — Flipkart returns 0 cards.
        raise HTTPException(status_code=409, detail=_scan_busy_message())

    is_pre_scanning = True
    logger.info("[API] Starting pre-scan product count...")

    context = None
    try:
        conn = init_database()
        seed_from_config(conn)
        conn.close()

        # JOIN sites so each URL row includes site_name (same as Node getAllUrls)
        urls = DBQueries().get_all_urls()

        if not urls:
            return {"sites": [], "message": "No URLs configured."}

        browser_launcher = await launch_stealth_browser()
        context = browser_launcher["context"]

        site_map: Dict[str, List[Dict[str, Any]]] = {}
        for u in urls:
            site_name = u.get("site_name") or "Unknown"
            site_map.setdefault(site_name, []).append(u)

        sites_result = []
        for site_name, site_urls in site_map.items():
            pages_result = []

            for url_record in site_urls:
                page_counts = []
                sku_selector = url_record.get("sku_selector") or url_record.get("sku_fallback")
                if not sku_selector:
                    pages_result.append({
                        "pageLabel": url_record["page_label"],
                        "url": url_record["url"],
                        "pageCounts": [{"page": 1, "count": 0}],
                        "totalProducts": 0,
                        "error": "No SKU selector configured"
                    })
                    continue

                try:
                    page = await context.new_page()
                    await apply_stealth_to_page(page)
                    await page.goto(url_record["url"], wait_until="domcontentloaded", timeout=45000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    # Wait until product cards appear (Flipkart hydrates slowly)
                    try:
                        await page.wait_for_selector(sku_selector, timeout=20000)
                    except Exception:
                        logger.warning(f"  [PRE-SCAN] Selector \"{sku_selector}\" not found within 20s on {url_record['url']}")
                    await asyncio.sleep(2.0)

                    await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(2.0)
                    await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(1.5)

                    page_1_count = await page.locator(sku_selector).count()
                    # Fallback selectors for Max Fashion, Flipkart, and general e-commerce listing cards
                    if page_1_count == 0:
                        fallback_list = [
                            "div[id^='product-']",
                            "a[href*='/p/']",
                            "div[class*='ProductCard']",
                            "div[class*='productCard']",
                            "div[class*='product-card']",
                            "div[data-id]",
                            "article.product-card",
                            "[data-grid-item]"
                        ]
                        for f_sel in fallback_list:
                            alt = await page.locator(f_sel).count()
                            if alt > 0:
                                logger.info(f"  [PRE-SCAN] Primary selector matched 0; fallback selector '{f_sel}' matched {alt}")
                                page_1_count = alt
                                sku_selector = f_sel
                                break

                    page_counts.append({"page": 1, "count": page_1_count})
                    logger.info(f"  [PRE-SCAN] {site_name} > {url_record['page_label']} — Page 1: {page_1_count} products")

                    strategy = url_record.get("pagination_strategy") or ("click" if url_record.get("pagination_selector") else "none")
                    max_pages = url_record.get("max_pages") or 1

                    if max_pages > 1 and page_1_count > 0:
                        current_page = 1
                        if strategy == "url_param":
                            while current_page < max_pages:
                                try:
                                    param_name = url_record.get("pagination_param") or "page"
                                    next_url = build_next_page_url(page.url, param_name, current_page + 1)
                                    await page.goto(next_url, wait_until="domcontentloaded", timeout=20000)
                                    await asyncio.sleep(2.0)
                                    await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                                    await asyncio.sleep(2.0)
                                    count = await page.locator(sku_selector).count()
                                    if count == 0:
                                        break
                                    page_counts.append({"page": current_page + 1, "count": count})
                                    logger.info(f"  [PRE-SCAN] {site_name} > {url_record['page_label']} — Page {current_page + 1}: {count} products")
                                    current_page += 1
                                except Exception as e:
                                    logger.warning(f"  [PRE-SCAN] Pagination failed at page {current_page + 1}: {e}")
                                    break
                        elif strategy == "click" and url_record.get("pagination_selector"):
                            while current_page < max_pages:
                                try:
                                    next_btn = page.locator(url_record["pagination_selector"]).first
                                    if await next_btn.count() > 0 and await next_btn.is_visible():
                                        await next_btn.click()
                                        await page.wait_for_load_state("networkidle", timeout=8000)
                                        await asyncio.sleep(3.0)
                                        await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                                        await asyncio.sleep(2.0)
                                        count = await page.locator(sku_selector).count()
                                        if count == 0:
                                            break
                                        page_counts.append({"page": current_page + 1, "count": count})
                                        logger.info(f"  [PRE-SCAN] {site_name} > {url_record['page_label']} — Page {current_page + 1}: {count} products")
                                        current_page += 1
                                    else:
                                        break
                                except Exception as e:
                                    logger.warning(f"  [PRE-SCAN] Click pagination failed at page {current_page + 1}: {e}")
                                    break
                        elif strategy == "infinite_scroll":
                            while current_page < max_pages:
                                before_count = await page.locator(sku_selector).count()
                                await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                                await asyncio.sleep(2.5)
                                after_count = await page.locator(sku_selector).count()
                                if after_count <= before_count:
                                    break
                                page_counts.append({"page": current_page + 1, "count": after_count - before_count})
                                logger.info(f"  [PRE-SCAN] {site_name} > {url_record['page_label']} — Scroll {current_page + 1}: +{after_count - before_count} products")
                                current_page += 1

                    await page.close()
                    total_products = sum(p["count"] for p in page_counts)
                    pages_result.append({
                        "pageLabel": url_record["page_label"],
                        "url": url_record["url"],
                        "pageCounts": page_counts,
                        "totalProducts": total_products
                    })
                except Exception as url_err:
                    logger.error(f"  [PRE-SCAN] Failed for {url_record['url']}: {url_err}")
                    pages_result.append({
                        "pageLabel": url_record["page_label"],
                        "url": url_record["url"],
                        "pageCounts": [],
                        "totalProducts": 0,
                        "error": str(url_err)
                    })

            sites_result.append({"siteName": site_name, "pages": pages_result})

        await context.close()
        context = None
        logger.info("[API] Pre-scan product count completed.")
        return {"sites": sites_result}
    except HTTPException:
        raise
    except Exception as err:
        logger.error(f"[API ERROR] Pre-scan count failed: {err}")
        raise HTTPException(status_code=500, detail=str(err))
    finally:
        if context:
            try:
                await context.close()
            except Exception:
                pass
        is_pre_scanning = False


@app.post("/api/simulate", dependencies=[Depends(verify_api_key)])
def trigger_simulate():
    logger.info("[API] Triggering change simulation...")
    try:
        success = simulate_change(logger.info)
        if success:
            fresh_data = read_data_json()
            return {
                "success": True,
                "message": "Simulated baseline changes written to SQLite! Trigger a scan next to detect them.",
                "data": fresh_data,
            }
        raise HTTPException(
            status_code=400,
            detail="Simulation skipped. No successful snapshots exist yet. Run a scan first.",
        )
    except HTTPException:
        raise
    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err))


@app.post("/api/sites", dependencies=[Depends(verify_api_key)])
async def add_site(payload: AddSitePayload, background_tasks: BackgroundTasks):
    logger.info(f"[API] Adding new competitor site: {payload.siteName}")
    context = None

    try:
        sites_config = {"sites": []}
        if SITES_JSON_PATH.exists():
            with open(SITES_JSON_PATH, "r", encoding="utf-8") as f:
                sites_config = json.load(f)

        site_exists = any(s["name"].lower() == payload.siteName.lower() for s in sites_config.get("sites", []))
        if not site_exists and len(sites_config.get("sites", [])) >= 3:
            raise HTTPException(
                status_code=400,
                detail="Maximum limit of 3 competitor sites reached. Please delete an existing site first."
            )

        is_batch = payload.urls is not None and len(payload.urls) > 0

        if is_batch and len(payload.urls) > 3:
            raise HTTPException(
                status_code=400,
                detail="Maximum limit of 3 routes/links per competitor site is allowed."
            )

        site_entry = None
        for s in sites_config["sites"]:
            if s["name"].lower() == payload.siteName.lower():
                site_entry = s
                break

        if not site_entry:
            site_entry = {
                "name": payload.siteName,
                "base_url": payload.baseUrl,
                "urls": []
            }
            sites_config["sites"].append(site_entry)

        if not is_batch:
            url_exists = any(u["url"].lower() == payload.url.lower() for u in site_entry.get("urls", []))
            if not url_exists and len(site_entry.get("urls", [])) >= 3:
                raise HTTPException(
                    status_code=400,
                    detail=f"Maximum limit of 3 routes/links per competitor site reached for \"{payload.siteName}\"."
                )

        if is_batch:
            for item in payload.urls:
                price_prim = item.priceSelector or ".product-card p.font-bold, .price, .amount, [class*=\"price\"]"
                price_fall = item.priceFallback or ".product-cont-size p, p"
                sku_prim = item.skuSelector or ".product-card, .product, .item"
                sku_fall = item.skuFallback or ".card-element"
                sku_cnt_m = item.skuCountMethod or "elements"
                msg_prim = item.messagingSelector or "h1"
                msg_fall = item.messagingFallback or ""
                pagi_prim = item.paginationSelector or ""
                pagi_fall = item.paginationFallback or ""
                pagi_strat = item.paginationStrategy or "click"
                pagi_param = item.paginationParam

                exists = any(u["url"].lower() == item.url.lower() for u in site_entry["urls"])
                if not exists:
                    site_entry["urls"].append({
                        "url": item.url,
                        "page_label": item.pageLabel,
                        "max_pages": int(item.maxPages or 1),
                        "pagination_strategy": pagi_strat,
                        "pagination_param": pagi_param,
                        "selectors": {
                            "price": {"primary": price_prim, "fallback": price_fall},
                            "sku": {"primary": sku_prim, "fallback": sku_fall, "countMethod": sku_cnt_m},
                            "messaging": {"primary": msg_prim, "fallback": msg_fall},
                            "pagination": {"primary": pagi_prim, "fallback": pagi_fall}
                        }
                    })
        else:
            if not payload.url or not payload.pageLabel:
                raise HTTPException(status_code=400, detail="URL and page label are required.")

            final_price_prim = payload.priceSelector or ""
            final_price_fall = payload.priceFallback or ""
            final_sku_prim = payload.skuSelector or ""
            final_sku_fall = payload.skuFallback or ""
            final_sku_cnt_m = payload.skuCountMethod or "elements"
            final_msg_prim = payload.messagingSelector or ""
            final_msg_fall = payload.messagingFallback or ""
            final_pagi_prim = payload.paginationSelector or ""
            final_pagi_fall = payload.paginationFallback or ""
            final_pagi_strat = "click"
            final_pagi_param = None

            if not final_price_prim or not final_sku_prim:
                target_hostname = urlparse(payload.url).hostname
                inherited_match = None
                inherited_strat = "click"
                inherited_param = None

                for s in sites_config["sites"]:
                    for u in s["urls"]:
                        try:
                            if urlparse(u["url"]).hostname == target_hostname and "selectors" in u:
                                inherited_match = u["selectors"]
                                inherited_strat = u.get("pagination_strategy", "click")
                                inherited_param = u.get("pagination_param")
                                break
                        except Exception:
                            pass
                    if inherited_match:
                        break

                if inherited_match:
                    logger.info(f"[API] Inheriting selectors for {target_hostname} from existing configuration.")
                    final_price_prim = final_price_prim or inherited_match.get("price", {}).get("primary") or ""
                    final_price_fall = final_price_fall or inherited_match.get("price", {}).get("fallback") or ""
                    final_sku_prim = final_sku_prim or inherited_match.get("sku", {}).get("primary") or ""
                    final_sku_fall = final_sku_fall or inherited_match.get("sku", {}).get("fallback") or ""
                    final_sku_cnt_m = final_sku_cnt_m or inherited_match.get("sku", {}).get("countMethod") or "elements"
                    final_msg_prim = final_msg_prim or inherited_match.get("messaging", {}).get("primary") or ""
                    final_msg_fall = final_msg_fall or inherited_match.get("messaging", {}).get("fallback") or ""
                    final_pagi_prim = final_pagi_prim or inherited_match.get("pagination", {}).get("primary") or ""
                    final_pagi_fall = final_pagi_fall or inherited_match.get("pagination", {}).get("fallback") or ""
                    final_pagi_strat = inherited_strat
                    final_pagi_param = inherited_param
                else:
                    logger.info(f"[API] Selectors not provided. Launching AI auto-discovery for: {payload.url}")
                    try:
                        launched = await launch_stealth_browser()
                        context = launched["context"]
                        page = await context.new_page()
                        await apply_stealth_to_page(page)
                        await _goto_for_discovery(page, payload.url)

                        ai_selectors = await discover_selectors(page)

                        final_price_prim = final_price_prim or ai_selectors.get("priceSelector") or ""
                        final_price_fall = final_price_fall or ai_selectors.get("priceFallback") or ""
                        final_sku_prim = final_sku_prim or ai_selectors.get("skuSelector") or ""
                        final_sku_fall = final_sku_fall or ai_selectors.get("skuFallback") or ""
                        final_msg_prim = final_msg_prim or ai_selectors.get("messagingSelector") or ""
                        final_msg_fall = final_msg_fall or ai_selectors.get("messagingFallback") or ""
                        final_pagi_prim = final_pagi_prim or ai_selectors.get("paginationSelector") or ""
                        final_pagi_fall = final_pagi_fall or ai_selectors.get("paginationFallback") or ""
                        final_pagi_strat = ai_selectors.get("paginationStrategy") or "click"
                        final_pagi_param = ai_selectors.get("paginationParam")

                        await context.close()
                        context = None
                    except Exception as ai_err:
                        logger.error(f"[API] AI Selector auto-discovery failed: {ai_err}. Falling back to defaults.")
                        if context:
                            await context.close()
                            context = None
                        final_price_prim = final_price_prim or ".product-card p.font-bold, .price, .amount, [class*=\"price\"]"
                        final_price_fall = final_price_fall or ".product-cont-size p, p"
                        final_sku_prim = final_sku_prim or ".product-card, .product, .item"
                        final_sku_fall = final_sku_fall or ".card-element"
                        final_msg_prim = final_msg_prim or "h1"

            if final_pagi_prim and payload.maxPages is None:
                logger.info(f"[API] Pagination detected: \"{final_pagi_prim}\". Requesting page limit confirmation from user.")
                return {
                    "success": True,
                    "needsPagePrompt": True,
                    "detectedPagination": True,
                    "selectors": {
                        "priceSelector": final_price_prim,
                        "priceFallback": final_price_fall,
                        "skuSelector": final_sku_prim,
                        "skuFallback": final_sku_fall,
                        "skuCountMethod": final_sku_cnt_m,
                        "messagingSelector": final_msg_prim,
                        "messagingFallback": final_msg_fall,
                        "paginationSelector": final_pagi_prim,
                        "paginationFallback": final_pagi_fall
                    },
                    "message": "AI has detected pagination elements on this website. Please confirm how many pages you want to monitor."
                }

            exists = any(u["url"].lower() == payload.url.lower() for u in site_entry["urls"])
            if not exists:
                site_entry["urls"].append({
                    "url": payload.url,
                    "page_label": payload.pageLabel,
                    "max_pages": int(payload.maxPages or 1),
                    "pagination_strategy": final_pagi_strat,
                    "pagination_param": final_pagi_param,
                    "selectors": {
                        "price": {"primary": final_price_prim, "fallback": final_price_fall},
                        "sku": {"primary": final_sku_prim, "fallback": final_sku_fall, "countMethod": final_sku_cnt_m},
                        "messaging": {"primary": final_msg_prim, "fallback": final_msg_fall},
                        "pagination": {"primary": final_pagi_prim, "fallback": final_pagi_fall}
                    }
                })

        with open(SITES_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(sites_config, f, indent=2)

        conn = init_database()
        seed_from_config(conn)
        conn.close()

        # Automatic crawl scan after adding site is disabled per user preference.
        # background_tasks.add_task(run_scan_with_lock, "SITES")

        export_data()
        fresh_data = read_data_json()
        return {"success": True, "message": "New competitor site added and SQLite database synchronized!", "data": fresh_data}

    except HTTPException:
        raise
    except Exception as err:
        logger.error(f"[API ERROR] Failed to save site: {err}")
        if context:
            try:
                await context.close()
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=str(err))


async def _goto_for_discovery(page, url: str, timeout: int = 30000):
    """Navigate for selector discovery. Handles heavy e-commerce pages gracefully without failing on timeout."""
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    except Exception as goto_err:
        logger.info(f"[API] domcontentloaded timeout on {url} ({goto_err}) — retrying with commit strategy...")
        try:
            await page.goto(url, wait_until="commit", timeout=20000)
        except Exception as retry_err:
            logger.warning(f"[API] Navigation fallback also timed out: {retry_err}")

    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        logger.info("[API] networkidle not reached — continuing with page analysis.")
    await asyncio.sleep(2.0)
    try:
        await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1.5)
        await page.evaluate("() => window.scrollTo(0, 0)")
        await asyncio.sleep(0.5)
    except Exception:
        pass


@app.post("/api/check-site", dependencies=[Depends(verify_api_key)])
async def check_site(payload: CheckSitePayload):
    logger.info(f"[API] Live inspecting selectors for: {payload.url}")
    context = None
    try:
        launched = await launch_stealth_browser()
        context = launched["context"]
        page = await context.new_page()
        await apply_stealth_to_page(page)

        logger.info(f"[API] Navigating to: {payload.url}")
        await _goto_for_discovery(page, payload.url)

        mock_record = {
            "url": payload.url,
            "price_selector": payload.priceSelector,
            "price_fallback": payload.priceFallback,
            "sku_selector": payload.skuSelector,
            "sku_fallback": payload.skuFallback,
            "sku_count_method": payload.skuCountMethod or "elements",
            "messaging_selector": payload.messagingSelector,
            "messaging_fallback": payload.messagingFallback
        }

        extraction_result = await extract_data(page, mock_record)
        await context.close()
        context = None

        return {"success": True, "extraction": extraction_result}
    except HTTPException:
        raise
    except Exception as err:
        logger.error(f"[API ERROR] Selector check failed: {err}")
        if context:
            try:
                await context.close()
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=str(err))


@app.post("/api/discover-selectors", dependencies=[Depends(verify_api_key)])
async def discover_selectors_api(payload: DiscoverSelectorsPayload):
    import traceback
    logger.info(f"[API] ========== discover-selectors START ==========")
    logger.info(f"[API] URL: {payload.url}")
    context = None
    try:
        logger.info("[API] Step 1/4: Launching stealth browser...")
        launched = await launch_stealth_browser()
        context = launched["context"]
        page = await context.new_page()
        await apply_stealth_to_page(page)
        logger.info("[API] Step 2/4: Navigating (domcontentloaded; networkidle optional)...")
        await _goto_for_discovery(page, payload.url)
        logger.info(f"[API] Step 2 done. Final page URL: {page.url}")

        logger.info("[API] Step 3/4: Running selector discovery (force_ai=True)...")
        selectors = await discover_selectors(page, force_ai=True)
        logger.info(f"[API] Step 3 done. Selectors: {selectors}")

        await context.close()
        context = None
        logger.info("[API] Step 4/4: Browser closed.")

        sku_sel = selectors.get("skuSelector") or selectors.get("skuFallback")
        price_sel = selectors.get("priceSelector") or selectors.get("priceFallback")

        if not selectors or not (sku_sel or price_sel):
            msg = "Could not discover usable selectors on this page. Try Live Inspect or enter selectors manually."
            logger.error(f"[API] {msg}")
            return JSONResponse(status_code=500, content={"error": msg, "detail": msg, "success": False})

        if not selectors.get("skuSelector"):
            selectors["skuSelector"] = sku_sel
        if not selectors.get("priceSelector"):
            selectors["priceSelector"] = price_sel

        logger.info("[API] ========== discover-selectors SUCCESS ==========")
        return {"success": True, "selectors": selectors}
    except HTTPException as http_err:
        message = str(http_err.detail).strip() or f"HTTP {http_err.status_code}"
        logger.error(f"[API] discover-selectors HTTPException: {message}")
        return JSONResponse(
            status_code=http_err.status_code,
            content={"error": message, "detail": message, "success": False},
        )
    except Exception as err:
        tb = traceback.format_exc()
        message = str(err).strip() or f"{type(err).__name__}: selector discovery failed"
        logger.error(f"[API ERROR] Selector discovery failed: {message}\n{tb}")
        if context:
            try:
                await context.close()
            except Exception:
                pass
        return JSONResponse(
            status_code=500,
            content={"error": message, "detail": message, "success": False, "type": type(err).__name__},
        )

@app.post("/api/discover-routes", dependencies=[Depends(verify_api_key)])
async def discover_routes_api(payload: DiscoverRoutesPayload):
    logger.info(f"[API] Route/URL auto-discovery request for: {payload.url}")
    try:
        discovered_urls = await auto_discover_site_urls(payload.url)
        return {"success": True, "urls": discovered_urls}
    except HTTPException:
        raise
    except Exception as err:
        logger.error(f"[API ERROR] Route discovery failed: {err}")
        raise HTTPException(status_code=500, detail=str(err))


@app.get("/api/discover-routes-stream", dependencies=[Depends(verify_api_key)])
async def discover_routes_stream(url: str):
    from fastapi.responses import StreamingResponse
    logger.info(f"[API] Route/URL auto-discovery stream request for: {url}")

    async def event_generator():
        queue = asyncio.Queue()

        def log_callback(msg: str):
            queue.put_nowait(msg)

        async def run_discovery():
            try:
                urls = await auto_discover_site_urls(url, log_fn=log_callback)
                queue.put_nowait({"type": "result", "urls": urls})
            except Exception as e:
                queue.put_nowait({"type": "error", "message": str(e)})

        # Run discovery as background task
        asyncio.create_task(run_discovery())

        while True:
            item = await queue.get()
            if isinstance(item, dict):
                yield f"data: {json.dumps(item)}\n\n"
                break
            else:
                yield f"data: {json.dumps({'type': 'log', 'message': item})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/settings", dependencies=[Depends(verify_api_key)])
def get_settings():
    settings = {"autoScan": False, "intervalMinutes": 10, "emailNotifications": False}
    try:
        if SETTINGS_JSON_PATH.exists():
            with open(SETTINGS_JSON_PATH, "r", encoding="utf-8") as f:
                settings = json.load(f)
    except Exception as e:
        logger.error(f"Error reading settings: {e}")
    return settings


@app.post("/api/settings", dependencies=[Depends(verify_api_key)])
def save_settings(payload: SettingsPayload):
    settings = {"autoScan": False, "intervalMinutes": 10, "emailNotifications": False}
    try:
        if SETTINGS_JSON_PATH.exists():
            with open(SETTINGS_JSON_PATH, "r", encoding="utf-8") as f:
                settings = json.load(f)
    except Exception as e:
        logger.error(f"Error reading settings before save: {e}")

    if payload.autoScan is not None:
        settings["autoScan"] = bool(payload.autoScan)
    if payload.intervalMinutes is not None:
        settings["intervalMinutes"] = max(1, int(payload.intervalMinutes))
    if payload.emailNotifications is not None:
        settings["emailNotifications"] = bool(payload.emailNotifications)

    try:
        with open(SETTINGS_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)

        if settings.get("autoScan"):
            start_auto_scan_timer(settings["intervalMinutes"])
        else:
            stop_auto_scan_timer()

        return {"success": True, "settings": settings}
    except Exception as err:
        logger.error(f"[API ERROR] Failed to save settings: {err}")
        raise HTTPException(status_code=500, detail=f"Failed to save settings: {err}")


@app.post("/api/clear-product-data", dependencies=[Depends(verify_api_key)])
def clear_product_data():
    logger.info("[API] Clearing scraped product data history...")
    try:
        queries = DBQueries()
        queries.clear_product_data()

        export_data()
        fresh_data = read_data_json()
        return {"success": True, "message": "Scraped product and snapshot data successfully cleared!", "data": fresh_data}
    except Exception as err:
        logger.error(f"[API ERROR] Clear product data failed: {err}")
        raise HTTPException(status_code=500, detail=str(err))


@app.post("/api/clear-changelog", dependencies=[Depends(verify_api_key)])
def clear_changelog():
    logger.info("[API] Clearing change log database history...")
    try:
        queries = DBQueries()
        queries.clear_changelog()

        export_data()
        fresh_data = read_data_json()
        return {"success": True, "message": "Change log timeline history successfully cleared!", "data": fresh_data}
    except Exception as err:
        logger.error(f"[API ERROR] Clear changelog failed: {err}")
        raise HTTPException(status_code=500, detail=str(err))


@app.put("/api/urls/{id}", dependencies=[Depends(verify_api_key)])
def update_url(id: int, payload: UpdateUrlPayload):
    logger.info(f"[API] Updating selectors for URL ID {id}...")
    try:
        conn = init_database()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM urls WHERE id = ?", (id,))
        url_record = cursor.fetchone()
        if not url_record:
            conn.close()
            raise HTTPException(status_code=404, detail="URL record not found")

        cursor.execute("SELECT * FROM sites WHERE id = ?", (url_record["site_id"],))
        site_record = cursor.fetchone()
        if not site_record:
            conn.close()
            raise HTTPException(status_code=404, detail="Associated site record not found")

        cursor.execute("""
            UPDATE urls
            SET price_selector = ?, price_fallback = ?,
                sku_selector = ?, sku_fallback = ?, sku_count_method = ?,
                messaging_selector = ?, messaging_fallback = ?,
                pagination_selector = ?, pagination_fallback = ?,
                pagination_strategy = ?, pagination_param = ?,
                max_pages = ?, page_label = ?
            WHERE id = ?
        """, (
            payload.price_selector if payload.price_selector is not None else url_record["price_selector"],
            payload.price_fallback if payload.price_fallback is not None else url_record["price_fallback"],
            payload.sku_selector if payload.sku_selector is not None else url_record["sku_selector"],
            payload.sku_fallback if payload.sku_fallback is not None else url_record["sku_fallback"],
            payload.sku_count_method if payload.sku_count_method is not None else url_record["sku_count_method"],
            payload.messaging_selector if payload.messaging_selector is not None else url_record["messaging_selector"],
            payload.messaging_fallback if payload.messaging_fallback is not None else url_record["messaging_fallback"],
            payload.pagination_selector if payload.pagination_selector is not None else url_record["pagination_selector"],
            payload.pagination_fallback if payload.pagination_fallback is not None else url_record["pagination_fallback"],
            payload.pagination_strategy if payload.pagination_strategy is not None else url_record["pagination_strategy"],
            payload.pagination_param if payload.pagination_param is not None else url_record["pagination_param"],
            payload.max_pages if payload.max_pages is not None else url_record["max_pages"],
            payload.page_label if payload.page_label is not None else url_record["page_label"],
            id
        ))
        conn.commit()
        conn.close()

        if SITES_JSON_PATH.exists():
            with open(SITES_JSON_PATH, "r", encoding="utf-8") as f:
                sites_config = json.load(f)

            site_entry = None
            for s in sites_config["sites"]:
                if s["name"].lower() == site_record["name"].lower():
                    site_entry = s
                    break

            if site_entry:
                for u in site_entry["urls"]:
                    if u["url"].lower() == url_record["url"].lower():
                        u["page_label"] = payload.page_label if payload.page_label is not None else u.get("page_label")
                        u["max_pages"] = int(payload.max_pages if payload.max_pages is not None else u.get("max_pages", 1))
                        u["pagination_strategy"] = payload.pagination_strategy if payload.pagination_strategy is not None else u.get("pagination_strategy")
                        u["pagination_param"] = payload.pagination_param if payload.pagination_param is not None else u.get("pagination_param")
                        u["selectors"] = {
                            "price": {
                                "primary": payload.price_selector if payload.price_selector is not None else u.get("selectors", {}).get("price", {}).get("primary"),
                                "fallback": payload.price_fallback if payload.price_fallback is not None else u.get("selectors", {}).get("price", {}).get("fallback")
                            },
                            "sku": {
                                "primary": payload.sku_selector if payload.sku_selector is not None else u.get("selectors", {}).get("sku", {}).get("primary"),
                                "fallback": payload.sku_fallback if payload.sku_fallback is not None else u.get("selectors", {}).get("sku", {}).get("fallback"),
                                "countMethod": payload.sku_count_method if payload.sku_count_method is not None else u.get("selectors", {}).get("sku", {}).get("countMethod")
                            },
                            "messaging": {
                                "primary": payload.messaging_selector if payload.messaging_selector is not None else u.get("selectors", {}).get("messaging", {}).get("primary"),
                                "fallback": payload.messaging_fallback if payload.messaging_fallback is not None else u.get("selectors", {}).get("messaging", {}).get("fallback")
                            },
                            "pagination": {
                                "primary": payload.pagination_selector if payload.pagination_selector is not None else u.get("selectors", {}).get("pagination", {}).get("primary"),
                                "fallback": payload.pagination_fallback if payload.pagination_fallback is not None else u.get("selectors", {}).get("pagination", {}).get("fallback")
                            }
                        }
                        break

            with open(SITES_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(sites_config, f, indent=2)

        export_data()
        fresh_data = read_data_json()
        return {"success": True, "message": "URL selectors updated successfully!", "data": fresh_data}
    except HTTPException:
        raise
    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err))


@app.delete("/api/sites/{id}", dependencies=[Depends(verify_api_key)])
def delete_site(id: int):
    logger.info(f"[API] Deleting competitor site ID {id}...")
    try:
        conn = init_database()
        cursor = conn.cursor()
        
        # 1. Fetch site name
        cursor.execute("SELECT name FROM sites WHERE id = ?", (id,))
        site_record = cursor.fetchone()
        if not site_record:
            conn.close()
            raise HTTPException(status_code=404, detail="Site not found")
        
        site_name = site_record["name"]

        # 2. Get associated URL IDs
        cursor.execute("SELECT id FROM urls WHERE site_id = ?", (id,))
        url_records = cursor.fetchall()
        url_ids = [r["id"] for r in url_records]

        # 3. Delete related rows transactionally
        if url_ids:
            placeholder = ",".join("?" for _ in url_ids)
            cursor.execute(f"DELETE FROM products WHERE url_id IN ({placeholder})", url_ids)
            cursor.execute(f"DELETE FROM changes WHERE url_id IN ({placeholder})", url_ids)
            cursor.execute(f"DELETE FROM snapshots WHERE url_id IN ({placeholder})", url_ids)
            cursor.execute(f"DELETE FROM urls WHERE site_id = ?", (id,))

        cursor.execute("DELETE FROM sites WHERE id = ?", (id,))
        conn.commit()
        conn.close()

        # 4. Remove entry from sites.json
        if SITES_JSON_PATH.exists():
            with open(SITES_JSON_PATH, "r", encoding="utf-8") as f:
                sites_config = json.load(f)
            
            sites_config["sites"] = [
                s for s in sites_config.get("sites", []) 
                if s["name"].lower() != site_name.lower()
            ]

            with open(SITES_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(sites_config, f, indent=2)

        # 5. Export fresh database dataset to data.json
        export_data()
        fresh_data = read_data_json()
        return {"success": True, "message": "Site and associated data deleted successfully!", "data": fresh_data}
    except HTTPException:
        raise
    except Exception as err:
        logger.error(f"[API ERROR] Failed to delete competitor site: {err}")
        raise HTTPException(status_code=500, detail=str(err))


@app.post("/api/reset-all", dependencies=[Depends(verify_api_key)])
def reset_all():
    logger.info("[API] Fully resetting database and clearing sites.json...")
    try:
        empty_config = {"sites": []}
        with open(SITES_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(empty_config, f, indent=2)

        conn = init_database()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM products")
        cursor.execute("DELETE FROM changes")
        cursor.execute("DELETE FROM snapshots")
        cursor.execute("DELETE FROM urls")
        cursor.execute("DELETE FROM sites")
        conn.commit()
        conn.close()

        export_data()
        fresh_data = read_data_json()
        return {"success": True, "message": "Database and sites.json configuration fully wiped!", "data": fresh_data}
    except Exception as err:
        logger.error(f"[API ERROR] Reset failed: {err}")
        raise HTTPException(status_code=500, detail=str(err))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.dashboard.server:app", host="0.0.0.0", port=3456, reload=True)
