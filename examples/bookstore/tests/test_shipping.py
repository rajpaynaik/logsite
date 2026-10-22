from bookstore import shipping


def test_free_over_the_threshold():
    assert shipping.quote("eu", 3.0, 80.0) == 0.0
