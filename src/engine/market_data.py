"""
Spot price feed feeding TimesFM.

Maintains a rolling window of ``context_len`` (default 512) one-minute close
prices per symbol, seeded from REST history and kept current over WebSocket.

Two providers are supported behind one interface:

* **coinbase** (default, US-friendly) — REST candles + ``ticker`` channel
  aggregated into one-minute bars.
* **binance** — REST klines + ``@kline_1m`` combined stream (uses the exchange's
  own one-minute bar close).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, List, Optional

import httpx
import websockets

from ..utils.config import Config
from ..utils.logger import get_logger

log = get_logger("marketdata")

_COINBASE_REST = "https://api.exchange.coinbase.com"
_COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
_BINANCE_REST = "https://api.binance.com"
_BINANCE_WS = "wss://stream.binance.com:9443/stream"


def _ws_connect(url: str):
    return websockets.connect(
        url, open_timeout=10, ping_interval=15, ping_timeout=15, max_queue=512
    )


class MarketDataFeed:
    """Rolling one-minute spot windows for a set of symbols."""

    def __init__(self, config: Config, symbols: List[str]) -> None:
        self.config = config
        self.provider = config.spot_provider
        self.context_len = config.context_len
        self.symbols = [s for s in symbols if s]

        self._windows: Dict[str, Deque[float]] = {
            s: deque(maxlen=self.context_len) for s in self.symbols
        }
        # Coinbase tick->minute aggregation state.
        self._cur_minute: Dict[str, int] = {}
        self._last_px: Dict[str, float] = {}
        self._stop = False
        self._connected = False

    # ------------------------------------------------------------------ access
    def get_window(self, symbol: str) -> List[float]:
        return list(self._windows.get(symbol, ()))

    def latest(self, symbol: str) -> Optional[float]:
        win = self._windows.get(symbol)
        return win[-1] if win else None

    def ready(self, symbol: str, minimum: int = 64) -> bool:
        return len(self._windows.get(symbol, ())) >= minimum

    @property
    def is_connected(self) -> bool:
        return self._connected

    def stop(self) -> None:
        self._stop = True

    def _append(self, symbol: str, price: float) -> None:
        win = self._windows.get(symbol)
        if win is not None and price > 0:
            win.append(float(price))

    # -------------------------------------------------------------------- seed
    async def seed(self) -> None:
        """Backfill each symbol's window from REST history."""
        async with httpx.AsyncClient(timeout=15.0) as http:
            for symbol in self.symbols:
                try:
                    if self.provider == "binance":
                        closes = await self._seed_binance(http, symbol)
                    else:
                        closes = await self._seed_coinbase(http, symbol)
                    for px in closes[-self.context_len:]:
                        self._append(symbol, px)
                    log.info(
                        "Seeded %s with %d/%d minutes (last=%.2f)",
                        symbol, len(self._windows[symbol]), self.context_len,
                        self._windows[symbol][-1] if self._windows[symbol] else float("nan"),
                    )
                except Exception as exc:
                    log.error("Failed to seed %s: %s", symbol, exc)

    async def _seed_binance(self, http: httpx.AsyncClient, symbol: str) -> List[float]:
        resp = await http.get(
            f"{_BINANCE_REST}/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "limit": self.context_len},
        )
        resp.raise_for_status()
        # kline = [openTime, open, high, low, close, volume, closeTime, ...]
        return [float(k[4]) for k in resp.json()]

    async def _seed_coinbase(self, http: httpx.AsyncClient, symbol: str) -> List[float]:
        # Coinbase returns <=300 candles/request, newest first: page backwards.
        collected: Dict[int, float] = {}
        end = datetime.now(timezone.utc)
        for _ in range(3):  # up to ~900 minutes, plenty for 512
            start = end - timedelta(minutes=300)
            resp = await http.get(
                f"{_COINBASE_REST}/products/{symbol}/candles",
                params={
                    "granularity": 60,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
            )
            resp.raise_for_status()
            rows = resp.json()  # [[time, low, high, open, close, volume], ...]
            if not rows:
                break
            for row in rows:
                collected[int(row[0])] = float(row[4])  # close
            end = start
            if len(collected) >= self.context_len + 5:
                break
            await asyncio.sleep(0.25)  # be polite to the public endpoint
        ordered = [collected[t] for t in sorted(collected)]
        return ordered

    # -------------------------------------------------------------------- run
    async def run(self) -> None:
        backoff = 1
        while not self._stop:
            try:
                if self.provider == "binance":
                    await self._listen_binance()
                else:
                    await self._listen_coinbase()
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connected = False
                log.error("Spot feed (%s) error: %s — reconnect in %ss",
                          self.provider, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    # ----------------------------------------------------------- binance listen
    async def _listen_binance(self) -> None:
        streams = "/".join(f"{s.lower()}@kline_1m" for s in self.symbols)
        url = f"{_BINANCE_WS}?streams={streams}"
        async with _ws_connect(url) as ws:
            self._connected = True
            log.info("Spot feed connected (binance): %s", url)
            while not self._stop:
                raw = await ws.recv()
                msg = json.loads(raw)
                data = msg.get("data", msg)
                kline = data.get("k", {})
                if kline.get("x"):  # candle closed
                    symbol = kline.get("s")
                    self._append(symbol, float(kline.get("c")))

    # ---------------------------------------------------------- coinbase listen
    async def _listen_coinbase(self) -> None:
        async with _ws_connect(_COINBASE_WS) as ws:
            self._connected = True
            sub = {
                "type": "subscribe",
                "product_ids": self.symbols,
                "channels": ["ticker"],
            }
            await ws.send(json.dumps(sub))
            log.info("Spot feed connected (coinbase): %s products=%s",
                     _COINBASE_WS, self.symbols)
            while not self._stop:
                raw = await ws.recv()
                msg = json.loads(raw)
                if msg.get("type") != "ticker" or "price" not in msg:
                    continue
                self._aggregate_tick(msg["product_id"], float(msg["price"]))

    def _aggregate_tick(self, symbol: str, price: float) -> None:
        """Fold a raw tick into one-minute close bars."""
        if symbol not in self._windows:
            return
        minute = int(time.time() // 60)
        prev = self._cur_minute.get(symbol)
        if prev is None:
            self._cur_minute[symbol] = minute
            self._last_px[symbol] = price
        elif minute > prev:
            # The previous minute has closed — commit its last price.
            self._append(symbol, self._last_px[symbol])
            self._cur_minute[symbol] = minute
            self._last_px[symbol] = price
        else:
            self._last_px[symbol] = price
