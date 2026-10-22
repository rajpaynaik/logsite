import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


class SupplierClient:
    """Orders stock from the book supplier.

    The supplier is slow under load, so a timed-out request is retried (``retries``
    attempts in all) before giving up: a retry usually gets through.
    """

    def __init__(self, url, timeout, retries):
        self.url = url
        self.timeout = timeout
        self.retries = retries

    def restock(self, sku, qty):
        body = json.dumps({"sku": sku, "qty": qty}).encode()
        request = urllib.request.Request(
            f"{self.url}/restock",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        for attempt in range(1, self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    return json.loads(resp.read())
            except urllib.error.URLError as exc:
                if attempt == self.retries:
                    raise
                log.warning("supplier attempt %s/%s failed: %s", attempt, self.retries, exc)
