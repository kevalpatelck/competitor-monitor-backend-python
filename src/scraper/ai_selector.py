import os
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlparse
from typing import Dict, Any, Optional
from playwright.async_api import Page

from src.config.env import config
from src.utils.logger import logger
from src.utils.ai_client import call_llm
from src.scraper.pagination_detector import detect_pagination

FAILURES_FILE = Path(__file__).resolve().parents[2] / "src" / "config" / "selector_failures.json"

def get_failures_map() -> Dict[str, int]:
    try:
        if FAILURES_FILE.exists():
            with open(FAILURES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"[SELECTOR DISCOVERY] Failed to read selector_failures.json: {e}")
    return {}

def get_failure_count(key: str) -> int:
    return get_failures_map().get(key, 0)

def increment_failure_count(key: str):
    try:
        FAILURES_FILE.parent.mkdir(parents=True, exist_ok=True)
        m = get_failures_map()
        m[key] = m.get(key, 0) + 1
        with open(FAILURES_FILE, "w", encoding="utf-8") as f:
            json.dump(m, f, indent=2)
    except Exception as e:
        logger.error(f"[SELECTOR DISCOVERY] Failed to write selector_failures.json: {e}")

def reset_failure_count(key: str):
    try:
        m = get_failures_map()
        if key in m:
            del m[key]
            with open(FAILURES_FILE, "w", encoding="utf-8") as f:
                json.dump(m, f, indent=2)
    except Exception as e:
        logger.error(f"[SELECTOR DISCOVERY] Failed to write selector_failures.json: {e}")

def sanitize_selector(sel: Any) -> Optional[str]:
    if not sel or not isinstance(sel, str):
        return None
    # Strip trailing commas/dots/whitespace
    clean = sel.strip().rstrip("., \t\n\r")
    # Strip leading commas/whitespace only
    clean = re.sub(r"^[,\s]+", "", clean).strip()
    if not clean:
        return None

    # CSS selectors should not end with punctuation/combinators and should not have empty class parts
    if re.search(r"(\.#|\.$|#$|>$|\+$|~$|\[$)", clean):
        return None
    if re.search(r"\.(?=[\s,>+~\[\])]|$)", clean):
        return None

    return clean

async def discover_selectors_heuristic(page: Page) -> Dict[str, Any]:
    logger.info("[HEURISTICS SELECTOR] Analyzing page structure...")

    result = await page.evaluate("""
        () => {
            function escapeSelectorToken(t) {
                return t.replace(/:/g, '\\\\:');
            }

            function normalizeClassString(cls) {
                if (!cls || typeof cls !== 'string') return '';
                const tokens = cls.split(/\\\\s+/)
                    .map(t => t.trim())
                    .filter(t => {
                        if (!t) return false;
                        if (/^(hover|active|focus|visited|disabled|first|last|odd|even|sm|md|lg|xl|2xl):/.test(t)) return false;
                        if (t.startsWith('style-') || t.startsWith('js-') || t.includes('[') || t.includes(']')) return false;
                        const lower = t.toLowerCase();
                        return !['active', 'selected', 'disabled', 'featured', 'loading', 'open', 'show', 'hide', 'hidden', 'visible'].some(kw => lower.includes(kw));
                    });
                tokens.sort();
                return tokens.join(' ');
            }

            const candidates = {};
            document.querySelectorAll('div, li, article').forEach(el => {
                const rawClass = el.className;
                if (!rawClass || typeof rawClass !== 'string') return;
                const normalized = normalizeClassString(rawClass);
                if (!normalized) return;

                const classes = normalized.split(' ').map(escapeSelectorToken);
                const tag = el.tagName.toLowerCase();
                const sig = `${tag}.${classes.join('.')}`;

                if (!candidates[sig]) {
                    candidates[sig] = { count: 0, sampleEl: el, classes, tag };
                }
                candidates[sig].count++;
            });

            const scoredCards = Object.values(candidates)
                .filter(c => c.count >= 3 && c.count <= 250)
                .map(c => {
                    let score = 0;
                    const text = c.sampleEl.textContent || '';
                    const hasImg = c.sampleEl.querySelector('img') !== null;
                    const hasPrice = /[₹$€£]\\\\s*\\\\d/.test(text);
                    const hasLink = c.sampleEl.querySelector('a') !== null;

                    if (hasImg) score += 30;
                    if (hasPrice) score += 40;
                    if (hasLink) score += 15;
                    if (c.count >= 8 && c.count <= 100) score += 15;

                    return { ...c, score };
                })
                .sort((a, b) => b.score - a.score);

            if (scoredCards.length === 0) return null;
            const bestCard = scoredCards[0];

            let skuPrimary = `${bestCard.tag}.${bestCard.classes[0]}`;
            let skuFallback = `${bestCard.tag}.${bestCard.classes.join('.')}`;
            let bestCardCount = bestCard.count;

            const attrCandidates = [
                'div[data-id]', 'div[data-testid]', 'div[data-product-id]', 'div[data-item-id]', 'div[data-sku]',
                'li[data-id]', 'li[data-testid]', 'article[data-id]', 'article[data-testid]'
            ];

            for (const attrSel of attrCandidates) {
                const attrEls = document.querySelectorAll(attrSel);
                if (attrEls.length < 3) continue;

                let attrScore = 0;
                const sampleEl = attrEls[0];
                const sampleText = sampleEl.textContent || '';
                if (sampleEl.querySelector('img')) attrScore += 30;
                if (/[₹$€£]\\\\s*\\\\d/.test(sampleText)) attrScore += 40;
                if (sampleEl.querySelector('a')) attrScore += 15;
                if (attrEls.length >= 8 && attrEls.length <= 100) attrScore += 15;

                if (attrScore >= 40 && attrEls.length > bestCardCount) {
                    skuPrimary = attrSel;
                    skuFallback = attrSel;
                    bestCardCount = attrEls.length;
                }
            }

            let pricePrimary = null;
            let priceFallback = null;
            let priceSample = null;

            const cardSample = document.querySelector(skuPrimary) || document.querySelector(skuFallback);
            if (cardSample) {
                const priceRegex = /[₹$€£]\\\\s*\\\\d+([.,]\\\\d+)?/i;
                const priceCandidates = [];

                cardSample.querySelectorAll('*').forEach(el => {
                    if (el.children.length > 0) return;
                    const text = el.textContent ? el.textContent.trim() : '';
                    if (text && priceRegex.test(text) && text.length < 25) {
                        const classes = (el.className || '').toString().trim().split(/\\\\s+/).filter(Boolean).map(escapeSelectorToken);
                        const hasStrike = getComputedStyle(el).textDecorationLine?.includes('line-through') || el.tagName.toLowerCase() === 'del' || el.tagName.toLowerCase() === 's';
                        priceCandidates.push({
                            tag: el.tagName.toLowerCase(),
                            classes,
                            text,
                            hasStrike
                        });
                    }
                });

                const activePrice = priceCandidates.find(c => !c.hasStrike) || priceCandidates[0];
                if (activePrice) {
                    pricePrimary = activePrice.classes.length ? `${activePrice.tag}.${activePrice.classes[0]}` : activePrice.tag;
                    priceFallback = activePrice.classes.length ? `${activePrice.tag}.${activePrice.classes.join('.')}` : activePrice.tag;
                    priceSample = activePrice.text;
                }
            }

            let msgPrimary = 'h1';
            let msgFallback = 'h1';
            let msgSample = null;

            const h1s = document.querySelectorAll('h1');
            if (h1s.length === 1) {
                msgPrimary = 'h1';
                msgFallback = 'h1';
                msgSample = h1s[0].textContent ? h1s[0].textContent.trim() : '';
            } else if (h1s.length > 1) {
                const longestH1 = Array.from(h1s).sort((a, b) => ((b.textContent || '').length) - ((a.textContent || '').length))[0];
                const classes = (longestH1.className || '').toString().trim().split(/\\\\s+/).filter(Boolean).map(escapeSelectorToken);
                msgPrimary = classes.length ? `h1.${classes[0]}` : 'h1';
                msgFallback = 'h1';
                msgSample = longestH1.textContent ? longestH1.textContent.trim() : '';
            } else {
                const h2s = document.querySelectorAll('h2');
                if (h2s.length > 0) {
                    const longestH2 = Array.from(h2s).sort((a, b) => ((b.textContent || '').length) - ((a.textContent || '').length))[0];
                    const classes = (longestH2.className || '').toString().trim().split(/\\\\s+/).filter(Boolean).map(escapeSelectorToken);
                    msgPrimary = classes.length ? `h2.${classes[0]}` : 'h2';
                    msgFallback = 'h2';
                    msgSample = longestH2.textContent ? longestH2.textContent.trim() : '';
                }
            }

            return {
                skuSelector: skuPrimary,
                skuFallback: skuFallback,
                priceSelector: pricePrimary,
                priceFallback: priceFallback,
                messagingSelector: msgPrimary,
                messagingFallback: msgFallback,
                _debug: {
                    cardCount: bestCard.count,
                    cardScore: bestCard.score,
                    priceSample,
                    msgSample
                }
            };
        }
    """)

    if not result:
        raise Exception("Heuristic scan could not find any product card candidate containers.")

    # Detect pagination configuration using robust multi-strategy detector
    pagination_config = await detect_pagination(page, result["skuSelector"])

    # Auto-decide SKU count method
    page_text = await page.evaluate("() => document.body.textContent || ''")
    of_regex = re.search(r"of\s+\d[\d,]*", page_text, re.IGNORECASE)
    count_method = "elements"
    if result["_debug"]["cardCount"] < 3 and of_regex:
        count_method = "text"

    return {
        "skuSelector": result["skuSelector"],
        "skuFallback": result["skuFallback"],
        "priceSelector": result["priceSelector"],
        "priceFallback": result["priceFallback"],
        "messagingSelector": result["messagingSelector"],
        "messagingFallback": result["messagingFallback"],
        "paginationSelector": pagination_config["selector"],
        "paginationFallback": pagination_config["fallback"],
        "paginationStrategy": pagination_config["strategy"],
        "paginationParam": pagination_config["param"],
        "skuCountMethod": count_method,
        "_debug": result["_debug"]
    }

async def discover_selectors_with_ai(page: Page) -> Dict[str, Any]:
    logger.info("[AI SELECTOR] Running GPT/Claude discovery...")

    page_details = await page.evaluate("""
        () => {
            function escapeSelectorToken(t) {
                return t.replace(/:/g, '\\\\:');
            }

            function getSelector(el) {
                let sel = el.tagName.toLowerCase();
                if (el.id) {
                    return `${sel}#${el.id}`;
                }
                if (el.className) {
                    const classes = Array.from(el.classList)
                        .filter(c => !c.includes('hover') && !c.includes('active') && !c.startsWith('style-'))
                        .map(escapeSelectorToken);
                    if (classes.length > 0) {
                        sel += `.${classes.join('.')}`;
                    }
                }
                return sel;
            }

            function normalizeClassString(cls) {
                if (!cls || typeof cls !== 'string') return '';
                const tokens = cls.split(/\\\\s+/)
                    .map(t => t.trim())
                    .filter(t => {
                        if (!t) return false;
                        if (/^(hover|active|focus|visited|disabled|first|last|odd|even|sm|md|lg|xl|2xl):/.test(t)) return false;
                        if (t.startsWith('style-') || t.startsWith('js-') || t.includes('[') || t.includes(']')) return false;
                        const lower = t.toLowerCase();
                        return !['active', 'selected', 'disabled', 'featured', 'loading', 'open', 'show', 'hide', 'hidden', 'visible'].some(kw => lower.includes(kw));
                    });
                tokens.sort();
                return tokens.join(' ');
            }

            const headings = [];
            document.querySelectorAll('h1, h2, h3').forEach(h => {
                const txt = h.textContent ? h.textContent.trim() : '';
                if (txt && txt.length > 3 && txt.length < 100) {
                    headings.push({ selector: getSelector(h), text: txt });
                }
            });

            const priceRegex = /[₹$€£]\\\\s*\\\\d+([.,]\\\\d+)?/i;
            const priceElements = [];
            document.querySelectorAll('p, span, div, font, h3, h4').forEach(el => {
                if (el.children.length === 0) {
                    const txt = el.textContent ? el.textContent.trim() : '';
                    if (txt && priceRegex.test(txt) && txt.length < 25) {
                        priceElements.push({ selector: getSelector(el), text: txt });
                    }
                }
            });

            const repeatingDivs = {};
            document.querySelectorAll('div').forEach(el => {
                const cls = el.className;
                if (cls && typeof cls === 'string' && cls.length > 0) {
                    const norm = normalizeClassString(cls);
                    if (norm) {
                        repeatingDivs[norm] = (repeatingDivs[norm] || 0) + 1;
                    }
                }
            });

            const repeatingCandidates = Object.entries(repeatingDivs)
                .filter(([_, count]) => count >= 2 && count < 150)
                .map(([cls, count]) => {
                    const validTokens = cls.split(/\\\\s+/).filter(t => t && /^[a-zA-Z0-9_-][a-zA-Z0-9_\\-:/\\\\[\\\\]\\\\.%]*$/.test(t) && !t.endsWith('.'));
                    if (!validTokens.length) return null;
                    const cleanCls = validTokens.map(escapeSelectorToken).join('.');
                    let first = null;
                    try { first = document.querySelector(`div.${cleanCls}`); } catch (_) { }
                    const selector = first ? getSelector(first) : `div.${cleanCls}`;
                    return { selector, count };
                })
                .filter(Boolean);

            const paginationKeywords = ['next', 'load more', 'show more', '>', '»', 'arrow', 'page'];
            const paginationElements = [];
            document.querySelectorAll('a, button, div, span').forEach(el => {
                const txt = el.textContent ? el.textContent.trim().toLowerCase() : '';
                if (txt && txt.length > 0 && txt.length < 30) {
                    const isMatch = paginationKeywords.some(kw => txt.includes(kw)) ||
                        (el.className && el.className.toLowerCase().includes('next')) ||
                        (el.className && el.className.toLowerCase().includes('pagination'));
                    if (isMatch) {
                        paginationElements.push({ selector: getSelector(el), text: txt });
                    }
                }
            });

            const attrSelectors = [
                'div[data-id]', 'div[data-testid]', 'div[data-product-id]', 'div[data-item-id]', 'div[data-sku]',
                'li[data-id]', 'li[data-testid]', 'article[data-id]', 'article[data-testid]'
            ];
            const attrCandidates = [];
            for (const attrSel of attrSelectors) {
                const els = document.querySelectorAll(attrSel);
                if (els.length >= 3) {
                    attrCandidates.push({ selector: attrSel, count: els.length });
                }
            }

            return {
                headings: headings.slice(0, 15),
                prices: priceElements.slice(0, 20),
                repeatingCards: repeatingCandidates.slice(0, 20),
                attributeCards: attrCandidates,
                pagination: paginationElements.slice(0, 15),
                pageTitle: document.title
            };
        }
    """)

    prompt = f"""
You are an expert web scraping selector generator AI. Analyze this DOM highlight structure and recommend CSS selectors to scrape:
1. "priceSelector": Targets the product price (e.g. ₹27, $299). Choose the class of the price element inside the cards.
2. "skuSelector": Targets the parent product card container in the product grid (e.g. .product-card).
3. "messagingSelector": Targets the page h1/h2 main heading title.
4. "paginationSelector": Targets the pagination "Next Page" link, or "Load More" button (e.g. a.next, .next-page, button.more).

Page Highlights:
Page Title: "{page_details.get('pageTitle', '')}"

Headings:
{json.dumps(page_details.get('headings', []), indent=2)}

Price Elements:
{json.dumps(page_details.get('prices', []), indent=2)}

Repeating Containers (class-based):
{json.dumps(page_details.get('repeatingCards', []), indent=2)}

Attribute-Based Containers (stable, preferred over class-based when they match more elements):
{json.dumps(page_details.get('attributeCards', []), indent=2)}

Pagination Candidates:
{json.dumps(page_details.get('pagination', []), indent=2)}

IMPORTANT: For skuSelector, prefer attribute-based selectors (e.g. div[data-id]) over class-based selectors when the attribute-based ones match more product cards. Attribute selectors are more stable across site updates.

Respond with a raw JSON block only (no markdown code blocks, no other text):
{{
  "priceSelector": "CSS selector matching the price inside the product card",
  "priceFallback": "Fallback CSS selector for price",
  "skuSelector": "CSS selector matching the product card container",
  "skuFallback": "Fallback CSS selector for product cards",
  "messagingSelector": "CSS selector matching page main title",
  "messagingFallback": "Fallback CSS selector for title",
  "paginationSelector": "CSS selector matching the Next Page / Load More element",
  "paginationFallback": "Fallback CSS selector for pagination"
}}
"""

    content = await call_llm(prompt, json_mode=True, prefer_anthropic=False)
    try:
        raw = json.loads(content)
        
        selectors = {}
        for key in ["priceSelector", "priceFallback", "skuSelector", "skuFallback", "messagingSelector", "messagingFallback", "paginationSelector", "paginationFallback"]:
            raw_val = raw.get(key)
            cleaned = sanitize_selector(raw_val)
            if raw_val and not cleaned:
                logger.warning(f"[AI SELECTOR] ⚠️ Discarding invalid selector for \"{key}\": {raw_val}")
            selectors[key] = cleaned

        # Promote fallback selector to primary if primary was not identified
        if not selectors.get("skuSelector") and selectors.get("skuFallback"):
            selectors["skuSelector"] = selectors["skuFallback"]
        if not selectors.get("priceSelector") and selectors.get("priceFallback"):
            selectors["priceSelector"] = selectors["priceFallback"]
        if not selectors.get("messagingSelector") and selectors.get("messagingFallback"):
            selectors["messagingSelector"] = selectors["messagingFallback"]
        if not selectors.get("paginationSelector") and selectors.get("paginationFallback"):
            selectors["paginationSelector"] = selectors["paginationFallback"]

        # If primary selectors are still None, inject robust universal e-commerce CSS fallbacks
        if not selectors.get("skuSelector"):
            selectors["skuSelector"] = "div[id^='product-'], a[href*='/p/'], div[class*='productCard'], div[class*='product-card'], div[data-id]"
        if not selectors.get("priceSelector"):
            selectors["priceSelector"] = "span[class*='price'], div[class*='price'], .price, .amount"
        if not selectors.get("messagingSelector"):
            selectors["messagingSelector"] = "h1, h2"

        logger.info(f"[AI SELECTOR] AI successfully identified selectors: {selectors}")
        return selectors
    except Exception as err:
        logger.error(f"[AI SELECTOR ERROR] Selector parsing failed: {err}. Raw output was:\n{content}")
        raise err

async def discover_selectors(page: Page, force_ai: bool = False) -> Dict[str, Any]:
    logger.info("[SELECTOR DISCOVERY] Starting hybrid selector discovery pipeline...")
    current_url = page.url

    # 1. Try to fetch cached selector from SQLite database
    cached_record = None
    try:
        conn = sqlite3.connect(config["db_path"])
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM urls WHERE url = ?", (current_url,))
        cached_record = cursor.fetchone()
        if not cached_record:
            domain = urlparse(current_url).hostname
            cursor.execute("SELECT * FROM urls WHERE url LIKE ?", (f"%{domain}%",))
            cached_record = cursor.fetchone()
        conn.close()
    except Exception as db_err:
        logger.warning(f"[SELECTOR DISCOVERY] Could not fetch cached URL record: {db_err}")

    # 2. If cached selector works, skip discovery (unless user explicitly requested Auto-Fill AI)
    if cached_record and cached_record["sku_selector"] and not force_ai:
        try:
            sku_sel = cached_record["sku_selector"]
            card_count = await page.locator(sku_sel).count()
            if card_count >= 3:
                logger.info(f"[SELECTOR DISCOVERY] Cached selector \"{sku_sel}\" is working ({card_count} elements matched). Skipping discovery.")
                reset_failure_count(current_url)
                return {
                    "skuSelector": cached_record["sku_selector"],
                    "skuFallback": cached_record["sku_fallback"],
                    "priceSelector": cached_record["price_selector"],
                    "priceFallback": cached_record["price_fallback"],
                    "messagingSelector": cached_record["messaging_selector"],
                    "messagingFallback": cached_record["messaging_fallback"],
                    "paginationSelector": cached_record["pagination_selector"],
                    "paginationFallback": cached_record["pagination_fallback"],
                    "paginationStrategy": cached_record["pagination_strategy"],
                    "paginationParam": cached_record["pagination_param"],
                    "skuCountMethod": cached_record["sku_count_method"]
                }
            logger.info(f"[SELECTOR DISCOVERY] Cached selector \"{sku_sel}\" matched only {card_count} elements. Marked as failed.")
        except Exception as test_err:
            logger.info(f"[SELECTOR DISCOVERY] Cached selector test failed: {test_err}")

    if cached_record and not force_ai:
        increment_failure_count(current_url)

    failures = get_failure_count(current_url)
    max_consecutive_failures = 3

    # 3. Try heuristics
    heuristic_result = None
    try:
        heuristic_result = await discover_selectors_heuristic(page)
        if (
            heuristic_result and
            heuristic_result.get("skuSelector") and
            heuristic_result.get("priceSelector") and
            heuristic_result.get("_debug") and
            heuristic_result["_debug"].get("cardScore", 0) >= 40
        ):
            logger.info(f"[SELECTOR DISCOVERY] Heuristics succeeded with high confidence (Score: {heuristic_result['_debug']['cardScore']}). Skipping AI.")
            reset_failure_count(current_url)
            return heuristic_result
        logger.info("[SELECTOR DISCOVERY] Heuristic confidence low.")
    except Exception as heur_err:
        logger.warning(f"[SELECTOR DISCOVERY] Heuristic search failed: {heur_err}")

    # 4. AI fallback — always for Auto-Fill (force_ai), otherwise after consecutive failures
    should_run_ai = force_ai or failures >= max_consecutive_failures
    if not should_run_ai:
        logger.info(f"[SELECTOR DISCOVERY] Skipping AI fallback (consecutive failures {failures}/{max_consecutive_failures} < {max_consecutive_failures}). Returning heuristics.")
        if heuristic_result:
            return heuristic_result
        return {
            "skuSelector": cached_record["sku_selector"] if cached_record else None,
            "skuFallback": cached_record["sku_fallback"] if cached_record else None,
            "priceSelector": cached_record["price_selector"] if cached_record else None,
            "priceFallback": cached_record["price_fallback"] if cached_record else None,
            "messagingSelector": cached_record["messaging_selector"] if cached_record else None,
            "messagingFallback": cached_record["messaging_fallback"] if cached_record else None,
        }

    logger.info("[SELECTOR DISCOVERY] Running AI selector discovery...")
    try:
        ai_result = await discover_selectors_with_ai(page)
        # Merge pagination strategy from heuristics when AI omitted it
        if heuristic_result:
            for key in ("paginationStrategy", "paginationParam", "skuCountMethod", "paginationSelector", "paginationFallback"):
                if not ai_result.get(key) and heuristic_result.get(key):
                    ai_result[key] = heuristic_result[key]
        reset_failure_count(current_url)
        return ai_result
    except Exception as ai_err:
        logger.error(f"[SELECTOR DISCOVERY] AI discovery failed: {ai_err}")
        if heuristic_result and (heuristic_result.get("skuSelector") or heuristic_result.get("priceSelector")):
            logger.info("[SELECTOR DISCOVERY] Falling back to heuristic selectors after AI failure.")
            return heuristic_result
        if cached_record and cached_record["sku_selector"]:
            return {
                "skuSelector": cached_record["sku_selector"],
                "skuFallback": cached_record["sku_fallback"],
                "priceSelector": cached_record["price_selector"],
                "priceFallback": cached_record["price_fallback"],
                "messagingSelector": cached_record["messaging_selector"],
                "messagingFallback": cached_record["messaging_fallback"],
                "paginationSelector": cached_record["pagination_selector"],
                "paginationFallback": cached_record["pagination_fallback"],
                "paginationStrategy": cached_record["pagination_strategy"],
                "paginationParam": cached_record["pagination_param"],
                "skuCountMethod": cached_record["sku_count_method"],
            }
        raise