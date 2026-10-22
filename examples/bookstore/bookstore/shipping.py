import logging

from bookstore.settings import SETTINGS

log = logging.getLogger(__name__)


def quote(zone, weight_kg, subtotal):
    """Shipping cost for an order: the zone's rate per kg, free over ``free_over``."""
    log.info("quote zone=%s weight_kg=%s subtotal=%s", zone, weight_kg, subtotal)
    shipping = SETTINGS["shipping"]
    if subtotal >= shipping["free_over"]:
        return 0.0
    rate = shipping["rate_per_kg"][zone]
    return round(rate * weight_kg, 2)
