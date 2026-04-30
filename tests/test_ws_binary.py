import struct

from angel_bot.market_data.ws_binary import parse_ws_subscriptions, parse_ws_tick_binary


def test_parse_ws_subscriptions():
    spec = "1:26000,26009|2:12345"
    g = parse_ws_subscriptions(spec)
    assert g == [
        {"exchangeType": 1, "tokens": ["26000", "26009"]},
        {"exchangeType": 2, "tokens": ["12345"]},
    ]
    assert parse_ws_subscriptions("") == []


def test_parse_ws_tick_binary_ltp_packet():
    buf = bytearray(51)
    buf[0] = 1
    buf[1] = 1
    tok = b"26000" + b"\x00" * 20
    buf[2:27] = tok
    struct.pack_into("<q", buf, 27, 7)
    struct.pack_into("<q", buf, 35, 1_700_000_000)
    struct.pack_into("<q", buf, 43, 19_850_00)
    d = parse_ws_tick_binary(bytes(buf))
    assert d["token"] == "26000"
    assert d["last_traded_price"] == 19850.0
