import io
import json
from unittest import mock

import pytest
from bookstore import inventory


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_reorder_adds_the_confirmed_quantity():
    inventory.STOCK.clear()
    reply = Response(json.dumps({"qty": 4}).encode())
    with mock.patch("urllib.request.urlopen", return_value=reply):
        assert inventory.reorder("BK-1", 4) == 4


def test_reorder_needs_a_positive_quantity():
    with pytest.raises(ValueError):
        inventory.reorder("BK-1", 0)
