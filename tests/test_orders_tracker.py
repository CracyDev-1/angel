from angel_bot.orders.tracker import normalize_order_lifecycle


def test_normalize_lifecycle_complete():
    life, _, filled, pending, _ = normalize_order_lifecycle(
        {"orderstatus": "complete", "quantity": "10", "filledshares": "10", "unfilledshares": "0"}
    )
    assert life == "executed"
    assert filled == 10
    assert pending == 0


def test_normalize_lifecycle_partial():
    life, _, filled, pending, _ = normalize_order_lifecycle(
        {"orderstatus": "open", "quantity": "10", "filledshares": "3", "unfilledshares": "7"}
    )
    assert life == "partial"
    assert filled == 3
    assert pending == 7
