import random
from typing import Dict, Any, List

PROFILES: List[Dict[str, Any]] = [
    {
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "viewport": {"width": 1920, "height": 1080},
        "platform": "Win32",
        "locale": "en-US",
        "timezone": "America/New_York",
        "screenResolution": {"width": 1920, "height": 1080},
    },
    {
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "viewport": {"width": 1366, "height": 768},
        "platform": "Win32",
        "locale": "en-US",
        "timezone": "America/Chicago",
        "screenResolution": {"width": 1366, "height": 768},
    },
    {
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "viewport": {"width": 1536, "height": 864},
        "platform": "Win32",
        "locale": "en-US",
        "timezone": "America/Los_Angeles",
        "screenResolution": {"width": 1536, "height": 864},
    },
    {
        "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "viewport": {"width": 1440, "height": 900},
        "platform": "MacIntel",
        "locale": "en-US",
        "timezone": "America/New_York",
        "screenResolution": {"width": 2560, "height": 1440},
    },
    {
        "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "viewport": {"width": 1680, "height": 1050},
        "platform": "MacIntel",
        "locale": "en-US",
        "timezone": "America/Chicago",
        "screenResolution": {"width": 1680, "height": 1050},
    },
    {
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "viewport": {"width": 1920, "height": 1200},
        "platform": "Win32",
        "locale": "en-GB",
        "timezone": "Europe/London",
        "screenResolution": {"width": 1920, "height": 1200},
    },
    {
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "viewport": {"width": 2560, "height": 1440},
        "platform": "Win32",
        "locale": "en-US",
        "timezone": "America/Denver",
        "screenResolution": {"width": 2560, "height": 1440},
    },
    {
        "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "viewport": {"width": 1280, "height": 800},
        "platform": "MacIntel",
        "locale": "en-US",
        "timezone": "America/Los_Angeles",
        "screenResolution": {"width": 2560, "height": 1600},
    },
    {
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "viewport": {"width": 1600, "height": 900},
        "platform": "Win32",
        "locale": "en-US",
        "timezone": "America/New_York",
        "screenResolution": {"width": 1600, "height": 900},
    },
    {
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "viewport": {"width": 1280, "height": 720},
        "platform": "Win32",
        "locale": "en-US",
        "timezone": "America/Chicago",
        "screenResolution": {"width": 1280, "height": 720},
    },
]

ACCEPT_HEADERS = {
    "document": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "language": "en-US,en;q=0.9",
}

last_profile_index = -1

def get_random_profile() -> Dict[str, Any]:
    global last_profile_index
    index = random.randint(0, len(PROFILES) - 1)
    while index == last_profile_index and len(PROFILES) > 1:
        index = random.randint(0, len(PROFILES) - 1)
    last_profile_index = index
    
    profile = PROFILES[index].copy()
    profile["acceptHeaders"] = ACCEPT_HEADERS
    return profile

def get_profile(index: int) -> Dict[str, Any]:
    profile = PROFILES[index % len(PROFILES)].copy()
    profile["acceptHeaders"] = ACCEPT_HEADERS
    return profile
