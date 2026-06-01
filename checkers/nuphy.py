"""NuPhy availability checker — uses Shopify's public product .js endpoint.

The `.js` endpoint exposes real per-variant `available` booleans and prices in
cents; the `.json` endpoint reports `available: null`, so it can't detect stock.
`br` is omitted from Accept-Encoding because `requests` only decodes brotli when
the optional `brotli` package is installed — otherwise the body is unreadable.

Stock alone is not enough: NuPhy does not ship to Israel for most items, so a
"back in stock" alert is useless unless they also deliver here. We treat an item
as available only when a variant is in stock AND Shopify's shipping-rates API
offers at least one rate to an Israeli address.
"""

import logging

import requests

from checkers import Checker, StockResult, register

log = logging.getLogger(__name__)

PRODUCT_URL = "https://nuphy.com/products/{handle}"
JSON_URL = "https://nuphy.com/products/{handle}.js"
CART_ADD_URL = "https://nuphy.com/cart/add.js"
SHIPPING_RATES_URL = "https://nuphy.com/cart/shipping_rates.json"
# Tel Aviv ZIP — only used to ask "do you ship to Israel at all?".
IL_ADDRESS = {
    "shipping_address[country]": "Israel",
    "shipping_address[zip]": "6100000",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://nuphy.com/",
}


class NuPhyChecker(Checker):
    @property
    def source_name(self) -> str:
        return "nuphy"

    def check(self, item_id: str) -> StockResult:
        url = PRODUCT_URL.format(handle=item_id)
        try:
            r = requests.get(
                JSON_URL.format(handle=item_id),
                headers=HEADERS,
                timeout=15,
            )
            r.raise_for_status()
            product = r.json()
            variants = product.get("variants", [])
            available = next((v for v in variants if v.get("available")), None)
            name = product.get("title") or f"NuPhy {item_id}"
            # .js prices are integer cents.
            sample = available or (variants[0] if variants else None)
            price = sample["price"] / 100 if sample and sample.get("price") else None

            # Stock + ships-to-Israel are both required. Only pay for the
            # shipping check when something is actually in stock.
            in_stock = available is not None and self._ships_to_israel(available["id"])
            return StockResult(in_stock=in_stock, price=price, name=name, url=url)
        except Exception:
            log.exception("NuPhy check failed for %s", item_id)
            return StockResult(
                in_stock=False,
                name=f"NuPhy {item_id} (check failed)",
                url=url,
            )

    def _ships_to_israel(self, variant_id: int) -> bool:
        """True only if Shopify offers a shipping rate to Israel for this item.

        Uses an ephemeral cart session: add the variant, then ask the legacy
        shipping-rates API for an Israeli address. An empty list means NuPhy
        does not deliver here. On error we return False so a broken check never
        produces a false "buy it now" alert (the failure is logged for jarvis).
        """
        try:
            s = requests.Session()
            s.headers.update(HEADERS)
            s.post(
                CART_ADD_URL,
                json={"id": variant_id, "quantity": 1},
                headers={"Content-Type": "application/json"},
                timeout=15,
            ).raise_for_status()
            r = s.get(SHIPPING_RATES_URL, params=IL_ADDRESS, timeout=25)
            r.raise_for_status()
            return bool(r.json().get("shipping_rates"))
        except Exception:
            log.exception("NuPhy Israel shipping check failed for variant %s", variant_id)
            return False


register(NuPhyChecker())
