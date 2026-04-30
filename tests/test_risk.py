from angel_bot.risk.engine import position_size_for_stop


def test_position_size_rounds_to_lots():
    qty = position_size_for_stop(
        capital=5_000,
        risk_pct=1.0,
        entry=100,
        stop=99,
        lot_size=50,
    )
    assert qty == 50
