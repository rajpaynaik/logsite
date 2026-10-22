"""Hidden check: a page count of 0 means "not known", as price_per_page's docstring says."""

from bookstore import catalog


def test_zero_pages_is_unknown():
    assert catalog.price_per_page(30.0, 0) is None


def test_missing_pages_is_unknown():
    assert catalog.price_per_page(30.0, None) is None


def test_price_per_page():
    assert catalog.price_per_page(30.0, 300) == 0.1


def test_a_feed_with_a_zero_page_count_imports():
    rows = [{"isbn": "978-0-262-03384-8", "title": "Introduction to Algorithms",
             "price": "55.71", "pages": 0}]  # fmt: skip
    assert catalog.import_feed(rows) == 1
    assert catalog.BOOKS["9780262033848"].per_page is None
