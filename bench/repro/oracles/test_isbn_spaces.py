"""Hidden check: spaces in an ISBN are ignored, as normalize_isbn's docstring says."""

import pytest
from bookstore import catalog


def test_spaces_are_ignored():
    assert catalog.normalize_isbn("978 1 59327 584 6") == "9781593275846"


def test_hyphens_still_are():
    assert catalog.normalize_isbn("978-0-13-468599-1") == "9780134685991"


def test_a_feed_with_spaced_isbns_imports():
    rows = [{"isbn": "978 0 596 52068 7", "title": "Programming Python", "price": "44.99"}]
    assert catalog.import_feed(rows) == 1


def test_garbage_is_still_rejected():
    with pytest.raises(ValueError):
        catalog.normalize_isbn("978 0 13")
