"""U Care (ucare.co.il) availability checker.

item_id is the URL slug, e.g. "huggies_extra_care_3" for
https://ucare.co.il/baby/diapers/huggies_extra_care_3
"""

import logging
import re

import requests
from bs4 import BeautifulSoup

from checkers import Checker, StockResult, register

log = logging.getLogger(__name__)

URL_TEMPLATE = "https://ucare.co.il/baby/diapers/{slug}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}

OUT_OF_STOCK_MARKERS = ["חסר במלאי", "אזל מהמלאי", "לא במלאי", "אין במלאי", "מוצר אזל"]
IN_STOCK_MARKERS = ["הוסיפו לעגלה", "הוסף לסל", "הוספה לסל", "לרכישה"]


class UCareChecker(Checker):
    @property
    def source_name(self) -> str:
        return "ucare"

    def check(self, item_id: str) -> StockResult:
        url = URL_TEMPLATE.format(slug=item_id)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
        except Exception:
            log.exception("UCare fetch failed for %s", item_id)
            return StockResult(in_stock=False, name=f"UCare {item_id} (check failed)", url=url)

        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        title = (soup.title.string or "").strip() if soup.title else f"UCare {item_id}"

        price = None
        m = re.search(r"₪\s*([\d,]+\.?\d*)", text)
        if m:
            try:
                price = float(m.group(1).replace(",", ""))
            except ValueError:
                pass

        if any(m in text for m in OUT_OF_STOCK_MARKERS):
            in_stock = False
        elif any(m in text for m in IN_STOCK_MARKERS):
            in_stock = True
        else:
            in_stock = False

        return StockResult(in_stock=in_stock, price=price, name=title, url=url)


register(UCareChecker())
