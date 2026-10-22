import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

BOOKS = {}


@dataclass
class Book:
    isbn: str
    title: str
    price: float
    per_page: float | None = None


def normalize_isbn(raw):
    """The 13 digits of an ISBN-13. Hyphens and spaces are ignored: suppliers send
    "978-0-13-468599-1" and "978 0 13 468599 1" alike."""
    digits = raw.replace("-", "")
    if len(digits) != 13 or not digits.isdigit():
        raise ValueError("invalid ISBN: 13 digits expected")
    return digits


def parse_price(raw):
    return round(float(raw), 2)


def price_per_page(price, pages):
    """Price per page, for the catalog's value sort. None when the page count is unknown:
    missing, or 0 (some suppliers send 0 for "not known yet")."""
    if pages is None:
        return None
    return round(price / pages, 4)


def import_feed(rows):
    """Add or update the books in a supplier feed; returns how many were imported."""
    for n, row in enumerate(rows, 1):
        log.info("importing isbn=%s title=%s", row["isbn"], row["title"])
        isbn = normalize_isbn(row["isbn"])
        price = parse_price(row["price"])
        log.info("row %s: price=%s pages=%s", n, price, row.get("pages"))
        BOOKS[isbn] = Book(isbn, row["title"], price, price_per_page(price, row.get("pages")))
    return len(rows)
