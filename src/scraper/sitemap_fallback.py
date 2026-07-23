import re
import time
import json
import urllib.request
import urllib.parse
import urllib.robotparser
import xml.etree.ElementTree as ET
from typing import Dict, Any, List, Optional, Tuple

BOT_PROTECTION_MARKERS = [
    "access denied",
    "reference #",
    "bm-verify",
    "just a moment...",
    "checking your browser",
    "cf-mitigation",
    "attention required! | cloudflare",
    "please verify you are a human",
    "blocked your access",
    "permission to access",
]

USER_AGENT = "Mozilla/5.0 (compatible; CompetitorMonitorBot/1.0; +http://localhost)"

def detect_bot_protection(
    status_code: Optional[int],
    content: str,
    url: str,
    card_count: int = 0
) -> Dict[str, Any]:
    """
    Step 2: Detect bot-protection blocking based on:
    - HTTP status code 403 or 429
    - Response body size below ~5KB when product page expected
    - Presence of known markers ("Access Denied", "Reference #", "_abck", etc.)
    - Missing DOM elements (0 product card count)
    """
    # If page loaded product cards cleanly, it is NOT blocked
    if card_count > 0 and (status_code is None or status_code == 200):
        return {"blocked": False, "block_type": None, "reason": None}

    content_lower = content.lower() if content else ""
    content_len = len(content) if content else 0

    if status_code in [403, 429]:
        return {
            "blocked": True,
            "block_type": f"HTTP {status_code} Access Denied",
            "reason": f"Server returned HTTP status {status_code} (Forbidden/Rate Limited)."
        }

    for marker in BOT_PROTECTION_MARKERS:
        if marker in content_lower:
            return {
                "blocked": True,
                "block_type": f"Bot Marker ({marker})",
                "reason": f"Response contains bot-protection marker '{marker}'."
            }

    if content_len > 0 and content_len < 5120 and card_count == 0:
        return {
            "blocked": True,
            "block_type": "Suspiciously Small Response (<5KB)",
            "reason": f"Response size ({content_len} bytes) is below expected 5KB threshold with 0 cards parsed."
        }

    return {"blocked": False, "block_type": None, "reason": None}


def parse_robots_txt(domain_url: str) -> Tuple[urllib.robotparser.RobotFileParser, Dict[str, Any]]:
    """
    Fetch and parse robots.txt using standard urllib.robotparser.RobotFileParser.
    Tracks sitemaps and Crawl-delay rules per User-agent block.
    """
    parsed = urllib.parse.urlparse(domain_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)

    metadata = {
        "sitemaps": [],
        "crawl_delay": 2.0
    }

    try:
        req = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            lines = resp.read().decode("utf-8", errors="ignore").splitlines()
            rp.parse(lines)

            current_ua = "*"
            for line in lines:
                line_str = line.strip()
                if not line_str or line_str.startswith("#"):
                    continue

                if line_str.lower().startswith("user-agent:"):
                    current_ua = line_str.split(":", 1)[1].strip().lower()
                elif line_str.lower().startswith("sitemap:"):
                    smap = line_str.split(":", 1)[1].strip()
                    if smap and smap not in metadata["sitemaps"]:
                        metadata["sitemaps"].append(smap)
                elif line_str.lower().startswith("crawl-delay:"):
                    if current_ua in ["*", "competitormonitorbot"]:
                        try:
                            metadata["crawl_delay"] = float(line_str.split(":", 1)[1].strip())
                        except ValueError:
                            pass
    except Exception:
        # Default sitemap fallback if robots.txt is blocked or unreadable
        metadata["sitemaps"].append(f"{parsed.scheme}://{parsed.netloc}/sitemap.xml")

    return rp, metadata


def extract_jsonld_products(html_content: str, url: str) -> List[Dict[str, Any]]:
    """
    Extract products via JSON-LD (<script type="application/ld+json">) using standard library regex.
    """
    products = []
    if not html_content:
        return products

    script_pattern = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE)
    matches = script_pattern.findall(html_content)

    for script_text in matches:
        script_text = script_text.strip()
        if not script_text:
            continue
        try:
            data = json.loads(script_text)
            nodes = data if isinstance(data, list) else [data]
            if isinstance(data, dict) and "@graph" in data:
                nodes.extend(data["@graph"])

            for node in nodes:
                if not isinstance(node, dict):
                    continue
                ntype = str(node.get("@type", "")).lower()
                if "product" in ntype or "offer" in ntype:
                    name = node.get("name")
                    price = None
                    currency = None

                    offers = node.get("offers")
                    if isinstance(offers, dict):
                        price = offers.get("price")
                        currency = offers.get("priceCurrency")
                    elif isinstance(offers, list) and len(offers) > 0:
                        first = offers[0]
                        if isinstance(first, dict):
                            price = first.get("price")
                            currency = first.get("priceCurrency")

                    if name:
                        products.append({
                            "name": str(name).strip(),
                            "price": float(price) if price else None,
                            "priceCurrency": str(currency) if currency else "INR"
                        })
        except Exception:
            continue

    return products


def fetch_sitemap_urls_two_level(target_url: str) -> Tuple[List[str], Dict[str, Any]]:
    """
    Step 3b: Two-level sitemap traversal:
    Index Sitemap -> Child Sitemaps -> Product/Listing <loc> URLs matching scope.
    """
    parsed = urllib.parse.urlparse(target_url)
    rp, metadata = parse_robots_txt(target_url)
    sitemap_index_list = metadata.get("sitemaps", [])

    discovered_product_urls = []
    target_keyword = parsed.path.strip("/").split("/")[0] if parsed.path else ""

    # Level 1: Iterate index sitemaps
    child_sitemaps = []
    for smap_url in sitemap_index_list:
        try:
            req = urllib.request.Request(smap_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as resp:
                xml_data = resp.read()
                root = ET.fromstring(xml_data)
                
                for elem in root.iter():
                    if elem.tag.endswith("loc") and elem.text:
                        loc_val = elem.text.strip()
                        if "sitemap" in loc_val.lower() and loc_val != smap_url:
                            child_sitemaps.append(loc_val)
                        elif target_keyword and target_keyword in loc_val:
                            child_sitemaps.append(smap_url)
        except Exception:
            continue

    if not child_sitemaps:
        child_sitemaps = sitemap_index_list

    # Level 2: Iterate child sitemaps for actual product/category <loc> URLs
    for child_url in list(set(child_sitemaps))[:3]:
        try:
            req = urllib.request.Request(child_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as resp:
                xml_data = resp.read()
                root = ET.fromstring(xml_data)
                
                for elem in root.iter():
                    if elem.tag.endswith("loc") and elem.text:
                        loc_url = elem.text.strip()
                        # Verify against robots.txt
                        if rp.can_fetch("CompetitorMonitorBot", loc_url):
                            if target_keyword and target_keyword in loc_url:
                                discovered_product_urls.append(loc_url)
                            elif any(k in loc_url.lower() for k in ["product", "item", "p/", "goods"]):
                                discovered_product_urls.append(loc_url)
        except Exception:
            continue

    return list(set(discovered_product_urls)), metadata


def process_bot_protected_url(
    url_record: Dict[str, Any],
    block_info: Dict[str, Any],
    logger_fn: Any = print
) -> Dict[str, Any]:
    """
    Steps 3, 4, 5: Execute compliant fallback policy when bot protection is detected.
    """
    log = logger_fn
    target_url = url_record["url"]
    block_type = block_info.get("block_type", "Bot Protection Blocked")
    reason = block_info.get("reason", "403/Bot Protection detected")

    # Step 4: LOG fallback event
    log(f"  [SCRAPING FALLBACK POLICY] Bot Protection Detected!")
    log(f"     Target URL: {target_url}")
    log(f"     Block Type: {block_type}")
    log(f"     Reason: {reason}")
    log(f"  [STOP DIRECT RETRIES] Switching to sitemap-based fallback path...")

    # Step 3b: Locate sitemap URLs via 2-level traversal
    sitemap_urls, metadata = fetch_sitemap_urls_two_level(target_url)
    crawl_delay = metadata.get("crawl_delay", 2.0)

    if not sitemap_urls:
        log(f"  [SITEMAP FALLBACK] Direct sitemap lookup blocked/empty for {target_url}.")
        log(f"  [COMPLIANT ACCESS] Item marked as 'unavailable via compliant access'. Surfacing for manual review.")
        return {
            "price": None,
            "priceCurrency": None,
            "skuCount": 0,
            "messagingText": None,
            "rawHtmlHash": "COMPLIANT_UNAVAILABLE",
            "products": [],
            "warnings": [
                f"[COMPLIANT ACCESS POLICY] {target_url} returned {block_type}. "
                f"Sitemap lookup unavailable. Item marked as unavailable via compliant access."
            ],
            "fallbackOutcome": "unavailable_via_compliant_access"
        }

    log(f"  [SITEMAP FALLBACK] Found {len(sitemap_urls)} compliant sitemap product URLs (Crawl delay: {crawl_delay}s)...")

    # Step 3c: Attempt compliant fetch of sitemap URLs
    scraped_products = []
    for idx, smap_url in enumerate(sitemap_urls[:5]):
        time.sleep(crawl_delay)
        try:
            req = urllib.request.Request(smap_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=12) as resp:
                if resp.status in [403, 429]:
                    # Step 3d: Log & Skip
                    log(f"  [SITEMAP_BLOCKED] Sitemap URL {smap_url} returned {resp.status}. Skipping without forcing access.")
                    continue
                
                body = resp.read().decode("utf-8", errors="ignore")
                check = detect_bot_protection(resp.status, body, smap_url)
                if check["blocked"]:
                    log(f"  [SITEMAP_BLOCKED] Sitemap URL {smap_url} returned bot protection ({check['block_type']}). Skipping.")
                    continue

                # Step 4: Extract JSON-LD product structured data
                parsed_prods = extract_jsonld_products(body, smap_url)
                if parsed_prods:
                    scraped_products.extend(parsed_prods)

                log(f"  [SITEMAP SUCCESS] Compliant data retrieved from sitemap URL: {smap_url} ({len(parsed_prods)} products parsed)")
                return {
                    "price": None,
                    "priceCurrency": "INR",
                    "skuCount": len(scraped_products),
                    "messagingText": f"Retrieved via sitemap fallback: {smap_url}",
                    "rawHtmlHash": "SITEMAP_SUCCESS",
                    "products": scraped_products,
                    "warnings": [f"[SITEMAP FALLBACK] Data retrieved via compliant sitemap path ({smap_url})."],
                    "fallbackOutcome": "success"
                }
        except Exception as smap_err:
            log(f"  [SITEMAP_SKIP] {smap_url} failed: {smap_err}. Skipping.")
            continue

    # Step 5: Mark as unavailable via compliant access if all sitemap URLs fail/block
    log(f"  [COMPLIANT ACCESS] All direct & sitemap endpoints for {target_url} returned bot protection.")
    log(f"  [COMPLIANT ACCESS] Marking item as 'unavailable via compliant access' for manual review.")

    return {
        "price": None,
        "priceCurrency": None,
        "skuCount": 0,
        "messagingText": None,
        "rawHtmlHash": "COMPLIANT_UNAVAILABLE",
        "products": [],
        "warnings": [
            f"[COMPLIANT ACCESS POLICY] {target_url} and sitemap endpoints returned bot protection. "
            f"Marked as unavailable via compliant access."
        ],
        "fallbackOutcome": "unavailable_via_compliant_access"
    }
