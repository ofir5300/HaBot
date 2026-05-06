"""Huggies Israel official site checker.

The item_id is the product slug under /wipes/ or /diapers/ (e.g. "huggies-extra-care").
The page renders a "where to buy" widget; if no retailers are listed we treat it as
out of stock. Any retailer link present → in stock.
"""

import logging

import requests
from bs4 import BeautifulSoup

from checkers import Checker, StockResult, register

log = logging.getLogger(__name__)

URL_TEMPLATE = "https://www.huggies.co.il/wipes/{slug}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}

OUT_OF_STOCK_MARKERS = [
    "No Retailers Found",
    "לא נמצאו קמעונאים",
    "אזל מהמלאי",
    "אין במלאי",
]


class HuggiesChecker(Checker):
    @property
    def source_name(self) -> str:
        return "huggies"

    def check(self, item_id: str) -> StockResult:
        url = URL_TEMPLATE.format(slug=item_id)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
        except Exception:
            log.exception("Huggies fetch failed for %s", item_id)
            return StockResult(in_stock=False, name=f"Huggies {item_id} (check failed)", url=url)

        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        title = (soup.title.string or "").strip() if soup.title else f"Huggies {item_id}"

        out_of_stock = any(m in text for m in OUT_OF_STOCK_MARKERS)
        return StockResult(in_stock=not out_of_stock, name=title, url=url)


register(HuggiesChecker())
