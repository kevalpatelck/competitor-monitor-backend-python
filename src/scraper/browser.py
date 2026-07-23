import asyncio
from typing import Dict, Any
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from playwright_stealth import Stealth
from src.config.env import config
from src.scraper.profiles import get_random_profile
from src.scraper.humanize import humanize_page, generate_referer

# Warm singleton browser instance processes
_playwright_manager = None
_browser_instance: Browser = None
_browser_uses_proxy = False


def _browser_is_connected(browser: Browser) -> bool:
    connected = getattr(browser, "is_connected", None)
    if connected is None:
        return False
    return connected() if callable(connected) else bool(connected)


async def get_singleton_browser(use_proxy: bool = False) -> Browser:
    """Reuse one Chromium process, but recreate when proxy mode changes."""
    global _playwright_manager, _browser_instance, _browser_uses_proxy
    if not _playwright_manager:
        _playwright_manager = await async_playwright().start()

    wants_proxy = bool(use_proxy and config.get("proxy_url"))
    needs_recreation = (
        not _browser_instance
        or not _browser_is_connected(_browser_instance)
        or (_browser_uses_proxy != wants_proxy)
    )

    if needs_recreation:
        if _browser_instance:
            try:
                await _browser_instance.close()
            except Exception:
                pass
            _browser_instance = None

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-infobars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
            "--disable-http2",
        ]
        launch_options = {
            "headless": True,
            "args": launch_args,
        }
        if wants_proxy:
            launch_options["proxy"] = {"server": config["proxy_url"]}

        _browser_instance = await _playwright_manager.chromium.launch(**launch_options)
        _browser_uses_proxy = wants_proxy

    return _browser_instance


_INIT_SCRIPT = """
    // Override navigator properties that stealth might miss
    Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

    // Fake the connection info
    if (navigator.connection) {
        Object.defineProperty(navigator.connection, 'rtt', { get: () => 50 });
        Object.defineProperty(navigator.connection, 'downlink', { get: () => 10 });
        Object.defineProperty(navigator.connection, 'effectiveType', { get: () => '4g' });
    }

    // Override Permissions API to avoid detection
    const originalQuery = window.navigator.permissions?.query;
    if (originalQuery) {
        window.navigator.permissions.query = (parameters) => {
            if (parameters.name === 'notifications') {
                return Promise.resolve({ state: Notification.permission });
            }
            return originalQuery(parameters);
        };
    }
"""


async def launch_stealth_browser(options: Dict[str, Any] = None) -> Dict[str, Any]:
    if options is None:
        options = {}

    profile = options.get("profile") or get_random_profile()
    use_proxy = options.get("use_proxy", False)

    browser = await get_singleton_browser(use_proxy)

    context = await browser.new_context(
        user_agent=profile["userAgent"],
        viewport=profile["viewport"],
        locale=profile["locale"],
        timezone_id=profile["timezone"],
        extra_http_headers={"DNT": "1"},
        permissions=["geolocation"],
        screen=profile["screenResolution"],
        ignore_https_errors=True,
    )

    await context.add_init_script(_INIT_SCRIPT)

    # Don't abort images — Flipkart product names often come from img[alt],
    # and blocking image requests can delay/prevent card hydration.
    async def route_interceptor(route):
        resource_type = route.request.resource_type
        if resource_type in ["font", "media"]:
            await route.abort()
        else:
            await route.continue_()

    await context.route("**/*", route_interceptor)

    return {"browser": browser, "context": context, "profile": profile}


async def apply_stealth_to_page(page: Page):
    await Stealth().apply_stealth_async(page)


async def visit_page(context: BrowserContext, url: str, options: Dict[str, Any] = None) -> Page:
    if options is None:
        options = {}

    page = await context.new_page()
    await apply_stealth_to_page(page)

    referer = generate_referer(url)

    try:
        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=config["scrape_timeout"],
                referer=referer if referer else None,
            )
        except Exception as goto_err:
            if "Timeout" in str(goto_err):
                # Fallback to commit strategy if DOMContentLoaded times out on slow assets
                await page.goto(
                    url,
                    wait_until="commit",
                    timeout=20000,
                    referer=referer if referer else None,
                )
            else:
                raise goto_err

        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        await humanize_page(page)
        return page

    except Exception as e:
        await page.close()
        raise e


async def shutdown_browser():
    global _playwright_manager, _browser_instance, _browser_uses_proxy
    if _browser_instance:
        await _browser_instance.close()
        _browser_instance = None
    _browser_uses_proxy = False
    if _playwright_manager:
        await _playwright_manager.stop()
        _playwright_manager = None
