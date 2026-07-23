import re
from typing import Dict, Any, List

BLOCK_SIGNATURES = [
    # Cloudflare
    {"pattern": "Checking your browser", "source": "Cloudflare JS Challenge"},
    {"pattern": "Just a moment...", "source": "Cloudflare Interstitial"},
    {"pattern": "cf-browser-verification", "source": "Cloudflare Verification"},
    {"pattern": "Enable JavaScript and cookies to continue", "source": "Cloudflare JS Required"},
    {"pattern": "ray ID", "source": "Cloudflare Block (Ray ID present)"},
    {"pattern": "cf-challenge-running", "source": "Cloudflare Challenge Running"},
    {"pattern": "Attention Required! | Cloudflare", "source": "Cloudflare Attention Page"},

    # DataDome
    {"pattern": "DataDome", "source": "DataDome Challenge"},
    {"pattern": "geo.captcha-delivery.com", "source": "DataDome CAPTCHA"},

    # PerimeterX / HUMAN
    {"pattern": "Press & Hold", "source": "PerimeterX Challenge"},
    {"pattern": "perimeterx", "source": "PerimeterX Block"},

    # Akamai (check for actual block message, not JS bundle URLs)
    {"pattern": "akamai access denied", "source": "Akamai Bot Manager"},
    {"pattern": "akamai bot manager", "source": "Akamai Bot Manager"},

    # Generic
    {"pattern": "Access denied", "source": "Generic Access Denied"},
    {"pattern": "Access Denied", "source": "Generic Access Denied"},
    {"pattern": "Please verify you are a human", "source": "CAPTCHA Challenge"},
    {"pattern": "Please verify you are human", "source": "CAPTCHA Challenge"},
    {"pattern": "are you a robot", "source": "Robot Check"},
    {"pattern": "blocked your access", "source": "IP Block"},
    {"pattern": "suspicious activity", "source": "Suspicious Activity Block"},
    {"pattern": "Too Many Requests", "source": "Rate Limited (429)"},
    {"pattern": "rate limit", "source": "Rate Limited"},

    # CAPTCHA services
    {"pattern": "hcaptcha.com", "source": "hCaptcha Challenge"},
    {"pattern": "recaptcha", "source": "reCAPTCHA Challenge"},
    {"pattern": "g-recaptcha", "source": "reCAPTCHA Widget"},
    {"pattern": "challenges.cloudflare.com", "source": "Cloudflare Turnstile"},
]

def is_blocked_page(html: str, url: str) -> Dict[str, Any]:
    html_lower = html.lower()

    for sig in BLOCK_SIGNATURES:
        if sig["pattern"].lower() in html_lower:
            # Check for reCAPTCHA false positives
            if sig["pattern"] in ["recaptcha", "g-recaptcha"]:
                # strip html tags to read plain text length
                text_content = re.sub(r"<[^>]*>", "", html).strip()
                if len(text_content) > 2000:
                    continue  # Legit page with reCAPTCHA widget
            
            return {
                "blocked": True,
                "reason": f"{sig['source']} detected on {url}"
            }

    # Suspiciously short page content
    text_content = re.sub(r"<[^>]*>", "", html).strip()
    if len(text_content) < 100 and "<!DOCTYPE" not in html:
        return {
            "blocked": True,
            "reason": f"Suspiciously short page content ({len(text_content)} chars) on {url}"
        }

    return {"blocked": False, "reason": ""}

def validate_extraction(data: Dict[str, Any], url: str) -> List[str]:
    warnings = []

    # Check if we got any data at all
    has_any_data = data.get("price") or (data.get("skuCount") is not None) or data.get("messagingText")
    if not has_any_data:
        warnings.append(
            f"[CRITICAL] No data extracted at all from {url} — all selectors returned empty. Check if selectors need updating."
        )

    # Price validation
    price = data.get("price")
    if price:
        try:
            price_num = float(price)
            if price_num <= 0:
                warnings.append(f"[WARN] Price {price_num} is zero or negative from {url} — suspicious")
            elif price_num > 100000:
                warnings.append(f"[WARN] Price {price_num} seems unusually high from {url}")
        except ValueError:
            warnings.append(f"[WARN] Price \"{price}\" is not a valid number from {url}")

    # SKU count validation
    sku_count = data.get("skuCount")
    if sku_count is not None:
        if sku_count <= 0:
            warnings.append(f"[WARN] SKU count is {sku_count} from {url} — expected positive number")

    # Messaging text validation
    messaging = data.get("messagingText")
    if messaging:
        if len(messaging) < 5:
            warnings.append(f"[WARN] Messaging text is very short ({len(messaging)} chars) from {url}")

    return warnings
