import logging

from bookstore.settings import SETTINGS
from bookstore.supplier import SupplierClient

log = logging.getLogger(__name__)

_supplier = SETTINGS["supplier"]
SUPPLIER = SupplierClient(_supplier["url"], _supplier["timeout_seconds"], _supplier["retries"])
STOCK = {}


def reorder(sku, qty):
    """Order ``qty`` more copies of ``sku`` from the supplier; returns the new stock level."""
    if qty <= 0:
        raise ValueError("qty must be positive")
    log.info("reorder sku=%s qty=%s", sku, qty)
    confirmed = SUPPLIER.restock(sku, qty)
    STOCK[sku] = STOCK.get(sku, 0) + confirmed["qty"]
    return STOCK[sku]
