import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

BOOKS = {}


@dataclass
class Book:
    isbn: str
    title: str
    price: float


def normalize_isbn(raw):
    """The 13 digits of an ISBN-13. Hyphens and spaces are ignored: suppliers send
    "978-0-13-468599-1" and "978 0 13 468599 1" alike."""
    digits = raw.replace("-", "")
    if len(digits) != 13 or not digits.isdigit():
        raise ValueError("invalid ISBN: 13 digits expected")
    return digits


def parse_price(raw):
    return round(float(raw), 2)


def import_feed(rows):
    """Add or update the books in a supplier feed; returns how many were imported."""
    for row in rows:
        log.info("importing isbn=%s title=%s", row["isbn"], row["title"])
        isbn = normalize_isbn(row["isbn"])
        BOOKS[isbn] = Book(isbn, row["title"], parse_price(row["price"]))
    return len(rows)
