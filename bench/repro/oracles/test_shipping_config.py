"""Hidden check: quotes use the configured rates."""

from bookstore import shipping


def test_rate_per_kg_by_zone():
    assert shipping.quote("eu", 2.0, 20.0) == 18.0
    assert shipping.quote("domestic", 1.0, 10.0) == 4.5
    assert shipping.quote("world", 0.5, 30.0) == 7.0


def test_free_over_the_threshold():
    assert shipping.quote("world", 3.0, 60.0) == 0.0
