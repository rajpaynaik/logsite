"""Hidden check: a read timeout is retried, as the supplier client's docstring says."""

import io
import json
import urllib.error
from unittest import mock

import pytest
from bookstore import inventory


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def reply(qty):
    return Response(json.dumps({"qty": qty}).encode())


def test_a_timeout_is_retried():
    inventory.STOCK.clear()
    with mock.patch("urllib.request.urlopen", side_effect=[TimeoutError("timed out"), reply(3)]):
        assert inventory.reorder("BK-7", 3) == 3


def test_connection_errors_are_still_retried():
    inventory.STOCK.clear()
    err = urllib.error.URLError("refused")
    with mock.patch("urllib.request.urlopen", side_effect=[err, err, reply(2)]):
        assert inventory.reorder("BK-8", 2) == 2


def test_it_gives_up_after_the_last_attempt():
    with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        with pytest.raises(TimeoutError):
            inventory.reorder("BK-9", 1)
