import logging

log = logging.getLogger(__name__)


class Cache:
    """A best-effort cache: when it's down, callers carry on without it."""

    def __init__(self):
        self.data = {}
        self.up = True

    def get(self, key):
        if not self.up:
            raise ConnectionRefusedError("cache unavailable")
        return self.data.get(key)

    def put(self, key, value):
        if self.up:
            self.data[key] = value


CACHE = Cache()
