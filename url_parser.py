"""URL → (source, item_id) parsing utility.

Extensible: add new patterns as checkers are created.
"""

import re

# Each pattern: (compiled regex, source_name, group index for item_id)
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"ksp\.co\.il/web/item/(\d+)"), "ksp"),
]


def parse_product_url(url: str) -> tuple[str, str] | None:
    """Parse a product URL into (source, item_id).

    Returns None for unrecognized URLs.
    """
    for pattern, source in _PATTERNS:
        m = pattern.search(url)
        if m:
            return (source, m.group(1))
    return None


def add_pattern(regex: str, source: str):
    """Register a new URL pattern at runtime."""
    _PATTERNS.append((re.compile(regex), source))
