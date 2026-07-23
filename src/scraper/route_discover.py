import re
import asyncio
import urllib.request
from urllib.parse import urlparse, urljoin
from typing import Dict, Any, List, Set
from src.scraper.browser import launch_stealth_browser

def classify_page_type(url_str: str, link_text: str = "") -> str:
    try:
        parsed = urlparse(url_str)
        path = parsed.path.lower()
        query = parsed.query.lower()
        text = (link_text or "").lower()

        # Skip patterns
        skip_patterns = [
            "/about", "/contact", "/privacy", "/terms", "/login", "/logout", "/register",
            "/cart", "/checkout", "/account", "/my-account", "/faq", "/help", "/support",
            "/blog", "/news", "/press", "/careers", "/jobs", "/search"
        ]
        if any(p in path for p in skip_patterns):
            return "skip"

        # File extensions
        if re.search(r"\.(pdf|jpg|jpeg|png|gif|svg|zip|mp4|mov|avi|css|js|xml|json)$", path):
            return "skip"

        # High confidence listing patterns
        listing_patterns = ["/shop", "/category", "/collections", "/products", "/catalog", "/store", "/men", "/women", "/kids", "/sale", "/new-in"]
        if any(p in path or p in query for p in listing_patterns):
            return "listing"

        # Anchor text patterns
        listing_text_patterns = ["shop", "category", "collection", "men", "women", "kids", "sale", "new", "clearance", "clothing", "apparels"]
        if any(p in text for p in listing_text_patterns):
            return "listing"

        return "unknown"
    except Exception:
        return "skip"

def _log(msg: str, log_fn=None):
    print(msg)
    if log_fn:
        try:
            log_fn(msg)
        except Exception:
            pass

def discover_from_sitemap(base_url: str, log_fn=None) -> List[str]:
    try:
        hostname = urlparse(base_url).scheme + "://" + urlparse(base_url).netloc
    except Exception:
        return []

    sitemap_urls = [
        f"{hostname}/sitemap.xml",
        f"{hostname}/sitemap_index.xml",
        f"{hostname}/sitemap-products.xml",
        f"{hostname}/sitemap-categories.xml",
    ]

    all_urls: Set[str] = set()

    for sitemap_url in sitemap_urls:
        try:
            _log(f"[Crawl] Trying to fetch sitemap: {sitemap_url}", log_fn)
            req = urllib.request.Request(
                sitemap_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                xml = response.read().decode("utf-8", errors="ignore")
                
            loc_matches = re.findall(r"<loc>(https?://[^<]+)</loc>", xml, re.IGNORECASE)
            for url in loc_matches:
                sanitized = url.strip().replace("&amp;", "&")
                all_urls.add(sanitized)

            if all_urls:
                _log(f"[Crawl] Successfully extracted {len(all_urls)} URLs from sitemap {sitemap_url}", log_fn)
                break
        except Exception as e:
            _log(f"[Crawl] Failed fetching sitemap {sitemap_url}: {e}", log_fn)

    return list(all_urls)

async def discover_links_from_page(page, base_url: str, options: Dict[str, Any] = None, log_fn=None) -> List[Dict[str, Any]]:
    if options is None:
        options = {}
        
    max_depth = options.get("maxDepth", 1)
    max_pages = options.get("maxPages", 30)
    same_origin_only = options.get("sameOriginOnly", True)

    try:
        origin = urlparse(base_url).scheme + "://" + urlparse(base_url).netloc
    except Exception:
        return []

    visited: Set[str] = set()
    queue = [{"url": base_url, "depth": 0}]
    discovered = []

    while queue and len(discovered) < max_pages:
        item = queue.pop(0)
        url = item["url"]
        depth = item["depth"]

        if url in visited:
            continue
        visited.add(url)

        _log(f"[Crawl] Visiting page: {url} (depth {depth}/{max_depth})", log_fn)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)  # pause for hydration

            page_links = await page.evaluate("""
                () => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                    href: a.href,
                    text: a.innerText ? a.innerText.trim() : ''
                }))
            """)

            discovered.append({"url": url, "depth": depth, "type": classify_page_type(url)})

            if depth < max_depth:
                for link in page_links:
                    try:
                        target_url = urljoin(origin, link["href"])
                        target_parsed = urlparse(target_url)
                        
                        if same_origin_only and (target_parsed.scheme + "://" + target_parsed.netloc) != origin:
                            continue
                        if target_url in visited:
                            continue

                        classification = classify_page_type(target_url, link["text"])
                        if classification == "skip":
                            continue

                        queue.append({"url": target_url, "depth": depth + 1})
                    except Exception:
                        continue
        except Exception as e:
            _log(f"[Crawl] Failed to visit {url}: {e}", log_fn)

    return discovered

async def auto_discover_site_urls(base_url: str, log_fn=None) -> List[Dict[str, Any]]:
    _log(f"[Crawl] Starting auto-discovery for: {base_url}", log_fn)
    
    # 1. Sitemap check
    urls = discover_from_sitemap(base_url, log_fn)

    # 2. Page crawl fallback
    if not urls:
        _log("[Crawl] No sitemaps found. Falling back to Page crawl...", log_fn)
        browser = None
        context = None
        try:
            launched = await launch_stealth_browser()
            browser = launched["browser"]
            context = launched["context"]
            page = await context.new_page()
            crawled = await discover_links_from_page(page, base_url, {"maxDepth": 1, "maxPages": 40}, log_fn)
            urls = [c["url"] for c in crawled]
        except Exception as err:
            _log(f"[Crawl Error] Crawling failed: {err}", log_fn)
        finally:
            if context:
                await context.close()

    # 3. Classify and structure URLs
    classified = []
    seen = set()
    for u in urls:
        u_type = classify_page_type(u)
        if u_type != "skip" and u not in seen:
            seen.add(u)
            classified.append({
                "url": u,
                "type": u_type
            })

    _log(f"[Crawl] Finished auto-discovery. Found {len(classified)} relevant URLs.", log_fn)
    return classified
