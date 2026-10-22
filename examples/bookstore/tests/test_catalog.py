import pytest
from bookstore import catalog


def test_hyphens_are_ignored():
    assert catalog.normalize_isbn("978-0-13-468599-1") == "9780134685991"


def test_too_short_is_rejected():
    with pytest.raises(ValueError):
        catalog.normalize_isbn("978-0-13")


def test_import_feed():
    rows = [{"isbn": "978-1-59327-584-6", "title": "The Linux Command Line", "price": "39.95"}]
    assert catalog.import_feed(rows) == 1
    assert catalog.BOOKS["9781593275846"].price == 39.95
