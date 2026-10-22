"""The service's request handler: one call per HTTP request."""

import logging
import uuid

from bookstore import catalog, inventory, shipping
from bookstore.cache import CACHE
from bookstore.logs import trace_id

log = logging.getLogger(__name__)

ROUTES = {
    "POST /feed": lambda body: catalog.import_feed(body["rows"]),
    "POST /reorder": lambda body: inventory.reorder(body["sku"], body["qty"]),
    "POST /quote": lambda body: shipping.quote(body["zone"], body["weight_kg"], body["subtotal"]),
}


def lookup(key):
    try:
        return CACHE.get(key)
    except ConnectionRefusedError:
        log.warning("cache lookup failed for %s", key, exc_info=True)
        return None


def handle(route, body, trace=None):
    """Handle one request; returns (status, result)."""
    trace_id.set(trace or uuid.uuid4().hex[:16])
    log.info("request %s", route)
    if route.startswith("POST /quote") and lookup(f"quote:{body['zone']}") is not None:
        return 200, CACHE.get(f"quote:{body['zone']}")
    try:
        return 200, ROUTES[route](body)
    except Exception:
        log.exception("request failed: %s", route)
        return 500, None
