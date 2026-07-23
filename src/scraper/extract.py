import re
import hashlib
import asyncio
from typing import Dict, Any, List, Tuple, Optional
from playwright.async_api import Page
from src.scraper.validate import validate_extraction

async def wait_for_stable_card_count(page: Page, selector: str, max_wait_ms: int = 2500):
    if not selector:
        return
    interval = 200
    last_count = 0
    stable_for = 0
    start = asyncio.get_event_loop().time()
    
    while (asyncio.get_event_loop().time() - start) * 1000 < max_wait_ms:
        try:
            count = await page.locator(selector).count()
            if count > 0 and count == last_count:
                stable_for += interval
                if stable_for >= 600:
                    break
            else:
                stable_for = 0
                last_count = count
        except Exception:
            break
        await asyncio.sleep(interval / 1000.0)

async def extract_with_fallback(page: Page, primary: str, fallback: str, field_name: str) -> str:
    if primary:
        try:
            await page.wait_for_selector(primary, timeout=5000)
            text = await page.locator(primary).first.text_content()
            if text and text.strip():
                return clean_text(text)
        except Exception:
            pass
            
    if fallback:
        try:
            await page.wait_for_selector(fallback, timeout=3000)
            text = await page.locator(fallback).first.text_content()
            if text and text.strip():
                return clean_text(text)
        except Exception:
            pass
            
    return None

async def count_elements(page: Page, primary: str, fallback: str) -> int:
    if primary:
        try:
            await page.wait_for_selector(primary, timeout=5000)
            count = await page.locator(primary).count()
            if count > 0:
                return count
        except Exception:
            pass
            
    if fallback:
        try:
            await page.wait_for_selector(fallback, timeout=3000)
            count = await page.locator(fallback).count()
            if count > 0:
                return count
        except Exception:
            pass
            
    return 0

CURRENCY_MAP = {
    '₹': 'INR', 'Rs.': 'INR', 'Rs': 'INR', 'INR': 'INR',
    '$': 'USD', 'USD': 'USD',
    '€': 'EUR', 'EUR': 'EUR',
    '£': 'GBP', 'GBP': 'GBP',
    '¥': 'JPY', 'JPY': 'JPY',
    'AED': 'AED', 'د.إ': 'AED',
}

def detect_currency(price_text: str) -> Tuple[Optional[str], Optional[str]]:
    for token, code in CURRENCY_MAP.items():
        if token in price_text:
            return token, code
    return None, None

def normalize_price(price_text: str) -> Dict[str, Any]:
    symbol, code = detect_currency(price_text)
    # Strip everything except digits, dots, minus
    cleaned = re.sub(r"[^0-9.,\-]", "", price_text)
    cleaned = re.sub(r",(\d{3})", r"\1", cleaned)  # remove thousands comma
    cleaned = cleaned.replace(",", ".")             # convert comma decimal to dot
    cleaned = cleaned.strip()

    match = re.search(r"-?\d+\.?\d*", cleaned)
    amount = match.group(0) if match else "N/A"
    formatted = f"{symbol}{amount}" if symbol else amount
    
    return {
        "amount": amount,
        "currency": code,
        "symbol": symbol,
        "formatted": formatted
    }

def clean_text(text: str) -> str:
    if not text:
        return ""
    # Strip html script/style leftover characters and whitespaces
    cleaned = re.sub(r"\s+", " ", text)
    return cleaned.strip()

def parse_count(text: str) -> int:
    match = re.search(r"\d+", text.replace(",", ""))
    return int(match.group(0)) if match else 0

async def extract_structured_metadata(page: Page) -> Dict[str, Any]:
    """
    Extract JSON-LD (<script type="application/ld+json">) and OpenGraph meta tags
    (e.g., og:price:amount, og:price:currency, twitter:data1).
    Serves as a high-stability fallback and cross-check signal against DOM selectors.
    """
    try:
        data = await page.evaluate("""() => {
            const metaPrice = document.querySelector('meta[property="og:price:amount"], meta[name="twitter:data1"], meta[property="product:price:amount"]')?.content;
            const metaCurrency = document.querySelector('meta[property="og:price:currency"], meta[name="twitter:label1"], meta[property="product:price:currency"]')?.content;
            
            const jsonLdScripts = Array.from(document.querySelectorAll('script[type="application/ld+json"]'));
            const jsonLdData = [];
            for (const s of jsonLdScripts) {
                try {
                    const parsed = JSON.parse(s.textContent || '{}');
                    if (parsed) jsonLdData.push(parsed);
                } catch (_) {}
            }
            return { metaPrice, metaCurrency, jsonLdData };
        }""")
        
        meta_price = data.get("metaPrice")
        meta_currency = data.get("metaCurrency")
        
        jsonld_price = None
        jsonld_currency = None
        jsonld_name = None
        
        for item in data.get("jsonLdData") or []:
            items_to_check = item if isinstance(item, list) else [item]
            if isinstance(item, dict) and "@graph" in item:
                items_to_check.extend(item["@graph"])

            for node in items_to_check:
                if not isinstance(node, dict):
                    continue
                node_type = str(node.get("@type", "")).lower()
                if "product" in node_type or "offer" in node_type:
                    jsonld_name = node.get("name") or jsonld_name
                    offers = node.get("offers")
                    if isinstance(offers, dict):
                        jsonld_price = offers.get("price") or jsonld_price
                        jsonld_currency = offers.get("priceCurrency") or jsonld_currency
                    elif isinstance(offers, list) and len(offers) > 0:
                        first_offer = offers[0]
                        if isinstance(first_offer, dict):
                            jsonld_price = first_offer.get("price") or jsonld_price
                            jsonld_currency = first_offer.get("priceCurrency") or jsonld_currency

        return {
            "metaPrice": meta_price,
            "metaCurrency": meta_currency,
            "jsonldPrice": str(jsonld_price) if jsonld_price is not None else None,
            "jsonldCurrency": jsonld_currency,
            "jsonldName": jsonld_name
        }
    except Exception:
        return {"metaPrice": None, "metaCurrency": None, "jsonldPrice": None, "jsonldCurrency": None, "jsonldName": None}


async def extract_products_from_page(page: Page, url_record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parse product cards into {name, price, priceCurrency}.

    Flipkart/etc. often lack h2–h5 titles; names live in img[alt] / product links,
    and price classes change frequently — so we use broad in-card fallbacks.
    """
    products: List[Dict[str, Any]] = []
    selector = url_record.get("sku_selector") or url_record.get("sku_fallback")
    if not selector:
        return products

    price_selector = url_record.get("price_selector") or ""
    price_fallback = url_record.get("price_fallback") or ""

    try:
        raw_products = await page.evaluate(
            """({ cardSel, priceSel, priceFallback }) => {
                function pickName(card) {
                    const nameSels = [
                        'a[title]',
                        '[title]',
                        'a[href*="/p/"]',
                        'a[href*="/product"]',
                        '.s1Q9rs', '.WKTcLC', '.IRpwTa', '.wjcEIp',
                        'h3', 'h4', 'h5', 'h2',
                        'a.title', '.product-title', '.title', '.name',
                        '[class*="title"]', '[class*="name"]', '[class*="Title"]'
                    ];
                    for (const sel of nameSels) {
                        try {
                            const el = card.querySelector(sel);
                            if (!el) continue;
                            const t = (el.getAttribute('title') || el.getAttribute('alt') || el.textContent || '').trim();
                            if (t && t.length > 2 && t.length < 300) return t;
                        } catch (_) {}
                    }
                    const img = card.querySelector('img[alt]');
                    if (img) {
                        const alt = (img.getAttribute('alt') || '').trim();
                        if (alt && alt.length > 2) return alt;
                    }
                    // Longest meaningful link text inside the card
                    let best = '';
                    card.querySelectorAll('a').forEach(a => {
                        const t = (a.textContent || '').trim().replace(/\\s+/g, ' ');
                        if (t.length > best.length && t.length > 5 && t.length < 200) best = t;
                    });
                    if (best) return best;
                    const dataId = card.getAttribute('data-id');
                    if (dataId) return 'Product ' + dataId;
                    return null;
                }

                function pickPrice(card) {
                    const trySels = [priceSel, priceFallback, '.price', '.amount', 'p.font-bold', 'span.price',
                        '[class*="price"]', '[class*="Price"]', 'div[class*="selling"]'].filter(Boolean);
                    for (const sel of trySels) {
                        try {
                            const el = card.querySelector(sel);
                            if (!el) continue;
                            const t = (el.textContent || '').trim();
                            if (t && /[₹$€£]|\\d/.test(t) && t.length < 40) return t;
                        } catch (_) {}
                    }
                    // Scan leaf nodes for currency amounts (Flipkart classes rotate often)
                    const priceRe = /[₹$€£]\\s*[\\d,]+(?:\\.\\d+)?|\\bINR\\s*[\\d,]+/i;
                    let found = null;
                    card.querySelectorAll('*').forEach(el => {
                        if (found || el.children.length > 0) return;
                        const style = window.getComputedStyle(el);
                        if (style && style.textDecorationLine && style.textDecorationLine.includes('line-through')) return;
                        const t = (el.textContent || '').trim();
                        if (t && priceRe.test(t) && t.length < 30) found = t;
                    });
                    return found;
                }

                let cards = [];
                try {
                    cards = Array.from(document.querySelectorAll(cardSel));
                } catch (_) {}
                
                if (cards.length === 0) {
                    const altSels = [
                        priceFallback,
                        "div[id^='product-']",
                        "a[href*='/p/']",
                        "div[class*='ProductCard']",
                        "div[class*='productCard']",
                        "div[class*='product-card']",
                        "div[data-id]",
                        "article.product-card",
                        "[data-grid-item]"
                    ];
                    for (const alt of altSels) {
                        if (!alt) continue;
                        try {
                            const found = Array.from(document.querySelectorAll(alt));
                            if (found.length > 0) {
                                cards = found;
                                break;
                            }
                        } catch (_) {}
                    }
                }

                return cards.map(card => {
                    const name = pickName(card);
                    const price = pickPrice(card);
                    return name ? { name, priceRaw: price || null } : null;
                }).filter(Boolean);
            }""",
            {
                "cardSel": selector,
                "priceSel": price_selector,
                "priceFallback": price_fallback,
            },
        )
    except Exception as e:
        print(f"[EXTRACT] Failed parsing catalog page products: {e}")
        return products

    for item in raw_products or []:
        name = clean_text(item.get("name") or "")
        if not name:
            continue
        parsed_price = "N/A"
        parsed_currency = None
        price_raw = item.get("priceRaw")
        if price_raw:
            p_res = normalize_price(price_raw)
            parsed_price = p_res["amount"]
            parsed_currency = p_res["currency"]
        products.append({
            "name": name,
            "price": parsed_price,
            "priceCurrency": parsed_currency,
        })

    # Fallback: Extract from embedded <script> tags containing JSON-LD or state JSON (e.g. Myntra / e-commerce)
    if not products:
        try:
            script_products = await page.evaluate("""() => {
                const prods = [];
                const scripts = Array.from(document.querySelectorAll('script'));
                for (const s of scripts) {
                    const text = (s.textContent || '').trim();
                    if (!text || (!text.includes('price') && !text.includes('Product') && !text.includes('offers'))) continue;
                    try {
                        let parsed = null;
                        if (s.type === 'application/ld+json' || text.startsWith('{') || text.startsWith('[')) {
                            parsed = JSON.parse(text.replace(/\\t/g, '').replace(/\\n/g, ''));
                        }
                        if (!parsed) continue;

                        const nodes = Array.isArray(parsed) ? parsed : [parsed];
                        if (parsed['@graph']) nodes.push(...parsed['@graph']);

                        for (const n of nodes) {
                            if (!n || typeof n !== 'object') continue;
                            const type = String(n['@type'] || '').toLowerCase();
                            if (type.includes('product') || type.includes('offer') || n.name) {
                                const name = n.name || n.title;
                                let price = null;
                                let currency = null;
                                if (n.offers) {
                                    const off = Array.isArray(n.offers) ? n.offers[0] : n.offers;
                                    if (off) {
                                        price = off.price || off.lowPrice || off.priceAmount;
                                        currency = off.priceCurrency;
                                    }
                                }
                                price = price || n.price;
                                currency = currency || n.priceCurrency || 'INR';
                                if (name && String(name).length > 2) {
                                    prods.push({ name: String(name).trim(), priceRaw: price ? String(price) : null, currency });
                                }
                            }
                        }
                    } catch (_) {}
                }
                return prods;
            }""")

            for item in script_products or []:
                name = clean_text(item.get("name") or "")
                if not name:
                    continue
                price_raw = item.get("priceRaw")
                p_res = normalize_price(price_raw) if price_raw else {"amount": "N/A", "currency": item.get("currency") or "INR"}
                products.append({
                    "name": name,
                    "price": p_res["amount"],
                    "priceCurrency": p_res["currency"] or item.get("currency") or "INR"
                })
            if products:
                print(f"  [SCRIPT JSON EXTRACTION] Successfully parsed {len(products)} products from embedded <script> tags!")
        except Exception as script_err:
            print(f"  [SCRIPT JSON EXTRACTION] Embedded script parsing error: {script_err}")

    if not products:
        try:
            card_count = await page.locator(selector).count()
            if card_count > 0:
                print(f"[EXTRACT] Found {card_count} cards via \"{selector}\" but parsed 0 product names/prices.")
        except Exception:
            pass

    return products

async def extract_data(page: Page, url_record: Dict[str, Any]) -> Dict[str, Any]:
    result = {
        "price": None,
        "priceCurrency": None,
        "priceFormatted": None,
        "skuCount": None,
        "messagingText": None,
        "rawHtmlHash": None,
        "products": [],
        "warnings": []
    }

    # Hash raw HTML
    try:
        html = await page.content()
        result["rawHtmlHash"] = hashlib.sha256(html.encode("utf-8")).hexdigest()[:16]
    except Exception as err:
        result["warnings"].append(f"HTML hash failed: {err}")

    # Scroll down to lazy load products
    try:
        print("  [EXTRACT] Scrolling down page to lazy-load elements...")
        sku_selector = url_record.get("sku_selector") or url_record.get("sku_fallback")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        
        # Stability counts checks
        await asyncio.gather(
            page.wait_for_load_state("networkidle", timeout=2000),
            wait_for_stable_card_count(page, sku_selector, 2500),
            return_exceptions=True
        )
        
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.5)")
        await asyncio.sleep(0.4)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        
        await asyncio.gather(
            page.wait_for_load_state("networkidle", timeout=1500),
            wait_for_stable_card_count(page, sku_selector, 2000),
            return_exceptions=True
        )
    except Exception as scroll_err:
        print(f"  [EXTRACT] Scroll helper failed: {scroll_err}")

    # Extract Structured Metadata (JSON-LD & Meta Tags) for Fallback & Signal Cross-Checking
    struct_meta = await extract_structured_metadata(page)

    # 1. Extract Page Price
    price_text = await extract_with_fallback(
        page,
        url_record.get("price_selector"),
        url_record.get("price_fallback"),
        "price"
    )
    if price_text:
        p_res = normalize_price(price_text)
        result["price"] = p_res["amount"]
        result["priceCurrency"] = p_res["currency"]
        result["priceFormatted"] = p_res["formatted"]
    elif struct_meta.get("jsonldPrice") or struct_meta.get("metaPrice"):
        # Structured Data Fallback
        s_price = struct_meta.get("jsonldPrice") or struct_meta.get("metaPrice")
        p_res = normalize_price(str(s_price))
        result["price"] = p_res["amount"]
        result["priceCurrency"] = struct_meta.get("jsonldCurrency") or struct_meta.get("metaCurrency") or p_res["currency"]
        result["priceFormatted"] = p_res["formatted"]
        print(f"  [STRUCTURED DATA FALLBACK] Used JSON-LD/Meta price fallback ({result['price']}) for {url_record['url']}")
    else:
        result["warnings"].append(f"[SELECTOR DRIFT] Price selector returned nothing for {url_record['url']}")

    # Signal Cross-Check: Compare DOM price with JSON-LD / Meta schema price if both present
    if result.get("price") and (struct_meta.get("jsonldPrice") or struct_meta.get("metaPrice")):
        schema_raw = struct_meta.get("jsonldPrice") or struct_meta.get("metaPrice")
        schema_res = normalize_price(str(schema_raw))
        if schema_res.get("amount") and schema_res["amount"] != "N/A":
            try:
                dom_p = float(result["price"])
                schema_p = float(schema_res["amount"])
                if abs(dom_p - schema_p) > 0.01:
                    result["warnings"].append(
                        f"[SIGNAL MISMATCH] DOM price ({dom_p}) differs from JSON-LD schema price ({schema_p}) on {url_record['url']}"
                    )
                    print(f"  ⚠️ [SIGNAL MISMATCH] DOM: {dom_p} | Schema: {schema_p} on {url_record['url']}")
            except ValueError:
                pass


    # 2. Extract SKU Count
    sku_method = url_record.get("sku_count_method", "elements")
    if sku_method == "elements":
        result["skuCount"] = await count_elements(
            page,
            url_record.get("sku_selector"),
            url_record.get("sku_fallback")
        )
    else:
        sku_text = await extract_with_fallback(
            page,
            url_record.get("sku_selector"),
            url_record.get("sku_fallback"),
            "sku"
        )
        result["skuCount"] = parse_count(sku_text) if sku_text else 0

    if result["skuCount"] is None:
        result["warnings"].append(f"[SELECTOR DRIFT] SKU selector returned nothing for {url_record['url']}")

    # 3. Extract Messaging
    msg_text = await extract_with_fallback(
        page,
        url_record.get("messaging_selector"),
        url_record.get("messaging_fallback"),
        "messaging"
    )
    if msg_text:
        result["messagingText"] = msg_text
    else:
        result["warnings"].append(f"[SELECTOR DRIFT] Messaging selector returned nothing for {url_record['url']}")

    # 4. Extract Products List (Page 1)
    result["products"] = await extract_products_from_page(page, url_record)

    # 5. Handle Pagination
    strategy = url_record.get("pagination_strategy") or ("click" if url_record.get("pagination_selector") else "none")
    max_pages = url_record.get("max_pages", 1)

    print("[PAGINATION DEBUG]", {
        "pagination_strategy": url_record.get("pagination_strategy"),
        "pagination_selector": url_record.get("pagination_selector"),
        "pagination_param": url_record.get("pagination_param"),
        "max_pages": url_record.get("max_pages"),
        "resolved_strategy": strategy,
        "resolved_maxPages": max_pages
    })

    if max_pages > 1 and len(result["products"]) > 0:
        current_page = 1
        print(f"  [PAGINATION] Branching pagination traversal (Strategy: \"{strategy}\", Max Pages: {max_pages})")
        
        if strategy == "url_param":
            from src.scraper.pagination_detector import build_next_page_url
            while current_page < max_pages:
                try:
                    param_name = url_record.get("pagination_param") or "page"
                    next_url = build_next_page_url(page.url, param_name, current_page + 1)
                    print(f"  [PAGINATION] Navigating to URL: {next_url}")
                    
                    await page.goto(next_url, wait_until="domcontentloaded", timeout=20000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=2000)
                    except Exception:
                        pass
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await wait_for_stable_card_count(page, url_record.get("sku_selector"), 3000)

                    next_products = await extract_products_from_page(page, url_record)
                    prev_keys = {f"{p['name']}|{p['price']}" for p in result["products"]}
                    new_products = [p for p in next_products if f"{p['name']}|{p['price']}" not in prev_keys]

                    if len(new_products) > 0:
                        result["products"].extend(new_products)
                        print(f"  [PAGINATION] Page {current_page + 1}: extracted {len(new_products)} products (running total: {len(result['products'])})")
                        current_page += 1
                    else:
                        print(f"  [PAGINATION] Page {current_page + 1} returned no NEW products. Stopping.")
                        break
                except Exception as pag_err:
                    print(f"  [PAGINATION] Parameter navigation failed: {pag_err}")
                    break
                    
        elif strategy == "infinite_scroll":
            while current_page < max_pages:
                try:
                    before_count = await page.locator(url_record.get("sku_selector")).count()
                    print(f"  [PAGINATION] Scrolling for infinite scroll (Before Count: {before_count})...")
                    
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(1.5)
                    await wait_for_stable_card_count(page, url_record.get("sku_selector"), 3000)
                    
                    after_count = await page.locator(url_record.get("sku_selector")).count()
                    new_scraped = after_count - before_count
                    print(f"  [PAGINATION] Scroll Page {current_page + 1}: extracted {new_scraped} products (running total: {after_count})")
                    
                    if after_count <= before_count:
                        print("  [PAGINATION] No new items loaded after scroll. Stopping.")
                        break
                    current_page += 1
                except Exception as pag_err:
                    print(f"  [PAGINATION] Infinite scroll scroll failed: {pag_err}")
                    break
                    
            final_products = await extract_products_from_page(page, url_record)
            existing_keys = {f"{p['name']}|{p['price']}" for p in result["products"]}
            for p in final_products:
                if f"{p['name']}|{p['price']}" not in existing_keys:
                    result["products"].append(p)
                    
        elif strategy == "click" and url_record.get("pagination_selector"):
            while current_page < max_pages:
                try:
                    pag_selector = url_record["pagination_selector"]
                    pag_btn = page.locator(pag_selector).first
                    
                    if await pag_btn.count() == 0 and url_record.get("pagination_fallback"):
                        pag_selector = url_record["pagination_fallback"]
                        pag_btn = page.locator(pag_selector).first

                    if await pag_btn.count() > 0:
                        # Scroll button into view to click safely
                        await pag_btn.scroll_into_view_if_needed()
                        await asyncio.sleep(0.5)
                        
                        # Fetch original product names before click
                        original_names = {p["name"] for p in result["products"]}
                        
                        # Click button
                        print(f"  [PAGINATION] Clicking next page button: \"{pag_selector}\"")
                        await pag_btn.click(timeout=8000)
                        
                        await page.wait_for_load_state("domcontentloaded", timeout=15000)
                        await page.wait_for_load_state("networkidle", timeout=3000)
                        
                        # Wait for catalog cards to settle
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await wait_for_stable_card_count(page, url_record.get("sku_selector"), 3000)

                        next_products = await extract_products_from_page(page, url_record)
                        
                        # Check if any new names are found
                        new_products = [p for p in next_products if p["name"] not in original_names]
                        if len(new_products) > 0:
                            result["products"].extend(new_products)
                            print(f"  [PAGINATION] Click Page {current_page + 1}: extracted {len(new_products)} products (running total: {len(result['products'])})")
                            current_page += 1
                        else:
                            print(f"  [PAGINATION] Click Page {current_page + 1} yielded no new products names. Stopping.")
                            break
                    else:
                        print("  [PAGINATION] Next button selector not found. Stopping.")
                        break
                except Exception as pag_err:
                    print(f"  [PAGINATION] Click / Navigate page failed: {pag_err}")
                    break

    # Standardize SKU count to length of actual products list if we found product listings
    if len(result["products"]) > 0:
        result["skuCount"] = len(result["products"])
        # FIX: Clear page-level price only when catalog listing products are extracted!
        result["price"] = None
        result["priceCurrency"] = None

    return result
