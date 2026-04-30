from __future__ import annotations

import json
import queue
import ssl
import threading
import time
from typing import Any, Callable

import structlog
import websocket

from angel_bot.market_data.ws_binary import parse_ws_tick_binary

log = structlog.get_logger(__name__)

WS_ROOT = "wss://smartapisocket.angelone.in/smart-stream"
SUBSCRIBE_ACTION = 1


def _jwt_for_ws(jwt: str | None) -> str:
    if not jwt:
        return ""
    s = jwt.strip()
    if s.lower().startswith("bearer "):
        return s[7:].strip()
    return s


class AngelWebSocketFeed:
    """
    Smart Stream v2 in a background thread; parsed ticks pushed to a thread-safe queue.
    Uses the same headers as Angel's official SmartWebSocketV2.
    """

    def __init__(
        self,
        *,
        jwt: str,
        api_key: str,
        client_code: str,
        feed_token: str,
        token_list: list[dict[str, Any]],
        mode: int = 1,
        correlation_id: str = "a1b2c3d4e5",
        out_queue: queue.Queue | None = None,
        on_tick: Callable[[dict[str, Any]], None] | None = None,
    ):
        self._jwt = _jwt_for_ws(jwt)
        self._api_key = api_key
        self._client_code = client_code
        self._feed_token = feed_token
        self._token_list = token_list
        self._mode = mode
        self._correlation_id = correlation_id[:10].ljust(10, "0")[:10]
        self._out: queue.Queue = out_queue or queue.Queue(maxsize=50_000)
        self._on_tick = on_tick
        self._wsapp: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None

    @property
    def queue(self) -> queue.Queue:
        return self._out

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def run() -> None:
            headers = [
                f"Authorization: {self._jwt}",
                f"x-api-key: {self._api_key}",
                f"x-client-code: {self._client_code}",
                f"x-feed-token: {self._feed_token}",
            ]

            def on_open(ws: websocket.WebSocketApp) -> None:
                payload = {
                    "correlationID": self._correlation_id,
                    "action": SUBSCRIBE_ACTION,
                    "params": {"mode": self._mode, "tokenList": self._token_list},
                }
                ws.send(json.dumps(payload))
                log.info("ws_subscribed", mode=self._mode, token_list=self._token_list)

            def on_data(ws: websocket.WebSocketApp, data: str | bytes, typ: int, flag: bool) -> None:
                if typ != 2 or not isinstance(data, (bytes, bytearray)):
                    return
                try:
                    tick = parse_ws_tick_binary(bytes(data))
                    if self._on_tick:
                        self._on_tick(tick)
                    else:
                        self._out.put_nowait(tick)
                except Exception as e:
                    log.warning("ws_tick_parse_error", error=str(e))

            def on_message(ws: websocket.WebSocketApp, message: str) -> None:
                if message == "pong":
                    return

            def on_error(ws: websocket.WebSocketApp, error: Any) -> None:
                log.warning("ws_error", error=str(error))

            def on_close(ws: websocket.WebSocketApp, code: Any, msg: Any) -> None:
                log.info("ws_closed", code=code, msg=msg)

            self._wsapp = websocket.WebSocketApp(
                WS_ROOT,
                header=headers,
                on_open=on_open,
                on_data=on_data,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            self._wsapp.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE}, ping_interval=10, ping_timeout=5)

        self._thread = threading.Thread(target=run, name="angel-ws-feed", daemon=True)
        self._thread.start()
        # brief yield so on_open can fire
        time.sleep(0.05)

    def stop(self) -> None:
        if self._wsapp:
            try:
                self._wsapp.close()
            except Exception:
                pass
            self._wsapp = None
