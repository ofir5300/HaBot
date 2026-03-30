import logging
import re
from concurrent.futures import ThreadPoolExecutor

from playwright.sync_api import sync_playwright

from checkers import Checker, StockResult, register

PRODUCT_URL = "https://ksp.co.il/web/item/{uin}"

log = logging.getLogger(__name__)

# Dedicated thread pool for Playwright (sync API can't run inside asyncio loop).
_executor = ThreadPoolExecutor(max_workers=1)

# Reuse a single browser instance across checks to avoid startup overhead.
_browser = None
_playwright = None


def _get_browser():
    global _browser, _playwright
    if _browser is None or not _browser.is_connected():
        if _playwright is not None:
            try:
                _playwright.stop()
            except Exception:
                pass
        _playwright = sync_playwright().start()
        _browser = _playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        log.info("KSP: launched persistent Chromium instance")
    return _browser


def _check_sync(item_id: str) -> StockResult:
    """Run the actual Playwright check (must run outside asyncio loop)."""
    url = PRODUCT_URL.format(uin=item_id)
    try:
        browser = _get_browser()
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        page.add_init_script(
            'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
        )

        page.goto(url, timeout=20000)
        page.wait_for_timeout(5000)

        title = page.title()
        if "403" in title or "Just a moment" in title:
            ctx.close()
            return StockResult(
                in_stock=False, price=None,
                name=f"KSP #{item_id} (site blocked)", url=url,
            )

        # Extract product name from page title
        name = title.strip() or f"KSP #{item_id}"

        # Extract price
        price = None
        price_el = page.query_selector('[class*=price], [class*=Price]')
        if price_el:
            price_text = price_el.text_content().strip()
            m = re.search(r"[\d,]+\.?\d*", price_text.replace(",", ""))
            if m:
                price = float(m.group())

        # Determine stock status from page text.
        # NOTE: do NOT check for addToCart buttons — the page has them
        # for recommended products, not necessarily for the main item.
        body_text = page.text_content("body") or ""
        in_stock = False
        if "לא זמין" in body_text or "אזל מהמלאי" in body_text:
            in_stock = False
        elif "הוסף לסל" in body_text or "הוספה לסל" in body_text:
            in_stock = True

        ctx.close()
        return StockResult(in_stock=in_stock, price=price, name=name, url=url)

    except Exception:
        log.exception("KSP Playwright check failed for %s", item_id)
        return StockResult(
            in_stock=False, price=None,
            name=f"KSP #{item_id} (check failed)", url=url,
        )


class KSPChecker(Checker):
    @property
    def source_name(self) -> str:
        return "ksp"

    def check(self, item_id: str) -> StockResult:
        # Submit to dedicated thread to avoid "sync API inside asyncio" error.
        future = _executor.submit(_check_sync, item_id)
        return future.result(timeout=30)


register(KSPChecker())
