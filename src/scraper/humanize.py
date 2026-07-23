import asyncio
import random
from playwright.async_api import Page

async def random_delay(min_ms: int = 1000, max_ms: int = 3000):
    delay = random.randint(min_ms, max_ms) / 1000.0
    await asyncio.sleep(delay)

async def simulate_mouse_movement(page: Page):
    try:
        viewport = page.viewport_size
        if not viewport:
            return
        
        steps = random.randint(3, 5)
        for _ in range(steps):
            x = random.randint(int(viewport["width"] * 0.1), int(viewport["width"] * 0.9))
            y = random.randint(int(viewport["height"] * 0.1), int(viewport["height"] * 0.9))
            
            # Move mouse smoothly over steps
            await page.mouse.move(x, y, steps=random.randint(10, 30))
            await random_delay(50, 200)
    except Exception:
        pass  # Mouse simulation is best-effort

async def simulate_scroll(page: Page, max_scrolls: int = 3):
    try:
        scroll_count = random.randint(1, max_scrolls)
        for _ in range(scroll_count):
            scroll_amount = random.randint(100, 400)
            await page.evaluate(f"window.scrollBy({{ top: {scroll_amount}, behavior: 'smooth' }})")
            await random_delay(300, 800)
            
        # Scroll back to top after reading - instant auto to avoid animation conflicts
        await page.evaluate("window.scrollTo({ top: 0, behavior: 'auto' })")
        await random_delay(200, 500)
    except Exception:
        pass

async def humanize_page(page: Page):
    # Wait for the page to feel "settled"
    await random_delay(500, 1500)
    
    # Move mouse around naturally
    await simulate_mouse_movement(page)
    
    # Scroll page like a real viewer
    await simulate_scroll(page)
    
    # Final small pause
    await random_delay(200, 600)

def generate_referer(target_url: str) -> str:
    from urllib.parse import quote_plus
    referers = [
        "https://www.google.com/",
        f"https://www.google.com/search?q={quote_plus(target_url)}",
        ""
    ]
    return random.choice(referers)
