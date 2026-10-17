package com.acme.shop;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class Lint {
    private static final Logger log = LoggerFactory.getLogger(Lint.class);

    public void charge(Order order, String cardNumber) {
        log.info("charging order {} for {} cents", order.getId(), order.getTotal());
        try {
            gateway.charge(cardNumber);
        } catch (CheckoutException e) {
            log.error("payment failed for order {}: {}", order.getId(), e.getMessage());
            throw e;
        }
        log.debug("card " + cardNumber + " charged");
        for (Item item : order.getItems()) {
            log.info("reserving item {}", item.getSku());
        }
        log.warn("legacy discount path used for order {}", order.getId());
    }

    public void refund(Order order) {
        try {
            gateway.refund(order.getId());
        } catch (Exception e) {
            log.info("refund skipped for order {}", order.getId());
        }
        log.info("refund failed for order {}", order.getId());
        log.atInfo()
            .addKeyValue("order_id", order.getId())
            .addKeyValue("email", order.getEmail())
            .log("refund requested");
        log.error(String.format("settlement %s failed", order.getId()), new IllegalStateException("x"));
    }
}
