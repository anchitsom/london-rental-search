"""
Stage 1: Playwright smoke test.
Verifies Chromium launches and can navigate to a real URL.
Run inside container: python tests/test_stage1_playwright.py
"""

import asyncio


async def main():
    print("--- Import check ---")
    try:
        from playwright.async_api import async_playwright
        print("  playwright: OK")
    except ImportError as e:
        print(f"  playwright FAILED: {e}")
        return

    try:
        from playwright_stealth import stealth_async
        print("  playwright_stealth: OK")
    except ImportError as e:
        print(f"  playwright_stealth FAILED: {e}")
        return

    print("\n--- Chromium launch ---")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        print("  Browser launched: OK")

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()
        await stealth_async(page)
        print("  Stealth applied: OK")

        print("\n--- Navigate to example.com (sanity check) ---")
        await page.goto("https://example.com", wait_until="domcontentloaded", timeout=15000)
        title = await page.title()
        print(f"  Title: {title}")

        print("\n--- Navigate to Rightmove homepage ---")
        try:
            await page.goto("https://www.rightmove.co.uk/", wait_until="domcontentloaded", timeout=20000)
            rm_title = await page.title()
            rm_url = page.url
            print(f"  Title: {rm_title}")
            print(f"  Final URL: {rm_url}")
            blocked = "page-not-found" in rm_url or "blocked" in rm_title.lower()
            print(f"  WAF blocked: {blocked}")
        except Exception as e:
            print(f"  FAILED: {e}")

        await context.close()
        await browser.close()
        print("\nStage 1: DONE")


asyncio.run(main())
