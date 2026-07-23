from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
from typing import Dict, Any, Optional
from playwright.async_api import Page

def build_next_page_url(current_url: str, param_name: str, page_number: int) -> str:
    try:
        parsed = urlparse(current_url)
        query = parse_qs(parsed.query)
        query[param_name] = [str(page_number)]
        new_query = urlencode(query, doseq=True)
        return urlunparse(parsed._replace(query=new_query))
    except Exception:
        return current_url

async def detect_pagination(page: Page, sku_selector: str) -> Dict[str, Any]:
    current_url = page.url

    # 1. URL parameter detection strategy
    try:
        parsed = urlparse(current_url)
        query = parse_qs(parsed.query)
        params = ['page', 'p', 'pg', 'pagenum', 'start', 'offset', 'pagination']
        found_param = None
        for param in params:
            if param in query:
                val = query[param][0]
                if val.isdigit():
                    found_param = param
                    break

        if found_param:
            print(f"[PAGINATION] Detected URL page parameter strategy: \"{found_param}\"")
            return {
                "strategy": "url_param",
                "param": found_param,
                "selector": None,
                "fallback": None
            }
    except Exception as e:
        print("[PAGINATION] URL parse error:", e)

    # 2. DOM Visual Scoring Strategy
    try:
        dom_result = await page.evaluate("""
            (skuSel) => {
                const grid = document.querySelector(skuSel);
                const gridRect = grid ? grid.getBoundingClientRect() : null;
                const gridBottom = gridRect ? (window.scrollY + gridRect.bottom) : 0;

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

                const candidates = [];
                const clickableElements = document.querySelectorAll('a, button, [role="button"], span, li');

                clickableElements.forEach(el => {
                    if (el.offsetWidth === 0 || el.offsetHeight === 0) return;

                    const text = (el.textContent || '').trim();
                    const rel = el.getAttribute('rel') || '';
                    const ariaLabel = (el.getAttribute('aria-label') || '').toLowerCase();
                    const title = (el.getAttribute('title') || '').toLowerCase();
                    const cls = (el.className || '').toString().toLowerCase();

                    const rect = el.getBoundingClientRect();
                    const absoluteTop = window.scrollY + rect.top;

                    if (gridBottom > 0 && absoluteTop < (window.scrollY + gridRect.top)) return;

                    let score = 0;
                    let isNext = false;
                    let isNumbered = false;

                    if (rel === 'next' || ariaLabel.includes('next') || title.includes('next') || cls.includes('pagination-next') || cls.includes('next-page')) {
                        score += 100;
                        isNext = true;
                    }

                    const lowerText = text.toLowerCase();
                    if (['next', 'next >', '>', '›', '»', 'next page', 'nextpage', 'load more', 'show more'].includes(lowerText)) {
                        score += 80;
                        isNext = true;
                    } else if (/^\\\\d+$/.test(text)) {
                        const pageNum = parseInt(text, 10);
                        if (pageNum > 1 && pageNum <= 10) {
                            score += 50;
                            isNumbered = true;
                        }
                    }

                    if (cls.includes('pagination') || cls.includes('pager') || cls.includes('paging')) {
                        score += 30;
                    }

                    if (gridBottom > 0) {
                        const verticalDistance = absoluteTop - gridBottom;
                        if (verticalDistance >= -50 && verticalDistance < 400) {
                            score += 40;
                        } else if (verticalDistance >= 400 && verticalDistance < 1000) {
                            score += 15;
                        } else if (verticalDistance < -50) {
                            score -= 30;
                        } else {
                            score -= 20;
                        }
                    }

                    const isDisabled = el.hasAttribute('disabled') ||
                        el.getAttribute('aria-disabled') === 'true' ||
                        cls.includes('disabled') ||
                        cls.includes('inactive');
                    if (isDisabled) {
                        score -= 120;
                    }

                    if (score > 0) {
                        candidates.push({ el, score, isNext, isNumbered, text });
                    }
                });

                if (candidates.length === 0) return null;

                candidates.sort((a, b) => b.score - a.score);
                const best = candidates[0];

                const primarySel = getSelector(best.el);
                const classes = Array.from(best.el.classList)
                    .filter(c => !c.includes('hover') && !c.includes('active') && !c.startsWith('style-'))
                    .map(escapeSelectorToken);
                const fallbackSel = classes.length ? `${best.el.tagName.toLowerCase()}.${classes[0]}` : best.el.tagName.toLowerCase();

                return {
                    isNext: best.isNext,
                    isNumbered: best.isNumbered,
                    selector: primarySel,
                    fallback: fallbackSel,
                    text: best.text
                };
            }
        """, sku_selector)

        if dom_result:
            if dom_result["isNext"] or dom_result["isNumbered"]:
                print(f"[PAGINATION] Detected Link: \"{dom_result['selector']}\" (text: \"{dom_result['text']}\")")
                return {
                    "strategy": "click",
                    "param": None,
                    "selector": dom_result["selector"],
                    "fallback": dom_result["fallback"]
                }
    except Exception as err:
        print("[PAGINATION] Heuristic DOM detection failed:", err)

    # 3. Infinite scroll fallback
    print("[PAGINATION] No clear pagination buttons found. Falling back to infinite scroll.")
    return {
        "strategy": "infinite_scroll",
        "param": None,
        "selector": None,
        "fallback": None
    }
