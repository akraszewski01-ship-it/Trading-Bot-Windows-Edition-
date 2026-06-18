"""
Real-time Kalshi orderbook maintained over WebSocket.

Kalshi binary markets quote two resting books, ``yes`` and ``no`` (prices in
cents, 1..99). The marketable prices follow from the binary identity
``yes_ask = 100 - best_no_bid`` (and symmetrically for ``no``):

    yes_bid = highest price in the YES book
    yes_ask = 100 - highest price in the NO book
    spread  = yes_ask - yes_bid

We subscribe to the ``orderbook_delta`` channel, seed each market from an
``orderbook_snapshot`` and apply incremental deltas, tracking the per-stream
``seq`` to detect gaps (which trigger a clean resubscribe).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, Optional, Set

import websockets

from ..utils.config import Config
from ..utils.logger import get_logger
from .kalshi_client import KalshiClient

log = get_logger("orderbook")


# --------------------------------------------------------------------------- #
#  websockets cross-version connect helper
# --------------------------------------------------------------------------- #
def _ws_connect(url: str, headers: Dict[str, str]):
    """Open a connection across websockets versions (header kwarg changed)."""
    kwargs = dict(open_timeout=10, ping_interval=10, ping_timeout=10, max_queue=256)
    try:  # websockets >= 14
        return websockets.connect(url, additional_headers=headers, **kwargs)
    except TypeError:  # websockets <= 13
        return websockets.connect(url, extra_headers=headers, **kwargs)


# --------------------------------------------------------------------------- #
#  Quote + book
# --------------------------------------------------------------------------- #
@dataclass
class Quote:
    """Top-of-book snapshot for the YES side of a market."""

    ticker: str
    yes_bid: Optional[int]      # cents
    yes_ask: Optional[int]      # cents
    yes_bid_size: int
    yes_ask_size: int
    ts: datetime

    @property
    def spread(self) -> Optional[int]:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid

    @property
    def mid(self) -> Optional[float]:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2.0

    @property
    def implied_prob(self) -> Optional[float]:
        mid = self.mid
        return None if mid is None else mid / 100.0

    @property
    def is_two_sided(self) -> bool:
        return self.yes_bid is not None and self.yes_ask is not None


@dataclass
class OrderBook:
    """Full depth for one market (price[cents] -> resting size)."""

    ticker: str
    yes: Dict[int, int] = field(default_factory=dict)
    no: Dict[int, int] = field(default_factory=dict)
    seq: int = 0
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def apply_snapshot(self, yes_levels, no_levels, seq: int) -> None:
        self.yes = {int(p): int(s) for p, s in (yes_levels or []) if int(s) > 0}
        self.no = {int(p): int(s) for p, s in (no_levels or []) if int(s) > 0}
        self.seq = seq
        self.ts = datetime.now(timezone.utc)

    def apply_delta(self, side: str, price: int, delta: int, seq: int) -> None:
        book = self.yes if side == "yes" else self.no
        new_size = book.get(int(price), 0) + int(delta)
        if new_size > 0:
            book[int(price)] = new_size
        else:
            book.pop(int(price), None)
        self.seq = seq
        self.ts = datetime.now(timezone.utc)

    # ------------------------------------------------------------- derived bbo
    def _best(self, book: Dict[int, int]):
        if not book:
            return None, 0
        price = max(book.keys())
        return price, book[price]

    def quote(self) -> Quote:
        best_yes, yes_sz = self._best(self.yes)
        best_no, no_sz = self._best(self.no)
        yes_ask = (100 - best_no) if best_no is not None else None
        # Size available at the YES ask equals the resting NO bid size.
        return Quote(
            ticker=self.ticker,
            yes_bid=best_yes,
            yes_ask=yes_ask,
            yes_bid_size=yes_sz,
            yes_ask_size=no_sz,
            ts=self.ts,
        )


# --------------------------------------------------------------------------- #
#  Manager
# --------------------------------------------------------------------------- #
class OrderBookManager:
    """Maintains live orderbooks for a dynamic set of market tickers."""

    def __init__(self, config: Config, client: KalshiClient) -> None:
        self.config = config
        self.client = client
        self._books: Dict[str, OrderBook] = {}
        self._tickers: Set[str] = set()
        self._sid_to_ticker: Dict[int, str] = {}
        self._cmd_id = 0
        self._stop = False
        self._resubscribe = asyncio.Event()
        self._connected = False

    # ------------------------------------------------------------------ public
    def set_tickers(self, tickers: Iterable[str]) -> None:
        """Update the desired subscription set; triggers a resubscribe."""
        new = {t for t in tickers if t}
        if new != self._tickers:
            log.info("Orderbook ticker set changed (%d markets)", len(new))
            self._tickers = new
            self._resubscribe.set()

    def get_book(self, ticker: str) -> Optional[OrderBook]:
        return self._books.get(ticker)

    def get_quote(self, ticker: str) -> Optional[Quote]:
        book = self._books.get(ticker)
        return book.quote() if book else None

    @property
    def is_connected(self) -> bool:
        return self._connected

    def stop(self) -> None:
        self._stop = True
        self._resubscribe.set()

    # ------------------------------------------------------------------ run loop
    async def run(self) -> None:
        backoff = 1
        while not self._stop:
            try:
                await self._connect_and_listen()
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connected = False
                log.error("Orderbook WS error: %s - reconnecting in %ss", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _connect_and_listen(self) -> None:
        url = self.config.kalshi_ws_url
        headers = self.client.ws_auth_headers()
        if not headers:
            log.warning("Connecting to Kalshi WS without auth (no key) - %s", url)

        async with _ws_connect(url, headers) as ws:
            self._connected = True
            log.info("Connected to Kalshi WS: %s", url)
            await self._subscribe(ws)

            while not self._stop:
                recv_task = asyncio.ensure_future(ws.recv())
                resub_task = asyncio.ensure_future(self._resubscribe.wait())
                done, pending = await asyncio.wait(
                    {recv_task, resub_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()

                if resub_task in done:
                    self._resubscribe.clear()
                    recv_task.cancel()
                    log.info("Resubscribing orderbook channel")
                    await self._subscribe(ws)
                    continue

                raw = recv_task.result()
                self._handle_message(raw)

    # --------------------------------------------------------------- subscribe
    async def _subscribe(self, ws) -> None:
        if not self._tickers:
            return
        self._cmd_id += 1
        cmd = {
            "id": self._cmd_id,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": sorted(self._tickers),
            },
        }
        await ws.send(json.dumps(cmd))
        log.info("Subscribed to orderbook_delta for %d markets", len(self._tickers))

    # ----------------------------------------------------------------- handler
    def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.debug("Non-JSON WS message ignored")
            return

        mtype = msg.get("type")
        if mtype == "orderbook_snapshot":
            self._on_snapshot(msg)
        elif mtype == "orderbook_delta":
            self._on_delta(msg)
        elif mtype == "subscribed":
            sid = msg.get("msg", {}).get("sid")
            log.debug("Subscription ack sid=%s", sid)
        elif mtype == "error":
            log.error("Kalshi WS error message: %s", msg.get("msg"))
        # other message types (ok/unsubscribed) are ignored

    def _on_snapshot(self, msg: dict) -> None:
        body = msg.get("msg", {})
        ticker = body.get("market_ticker")
        if not ticker:
            return
        sid = msg.get("sid")
        seq = msg.get("seq", 0)
        book = self._books.setdefault(ticker, OrderBook(ticker=ticker))
        book.apply_snapshot(body.get("yes"), body.get("no"), seq)
        if sid is not None:
            self._sid_to_ticker[sid] = ticker
        log.debug("Snapshot %s seq=%s (yes=%d no=%d levels)",
                  ticker, seq, len(book.yes), len(book.no))

    def _on_delta(self, msg: dict) -> None:
        body = msg.get("msg", {})
        ticker = body.get("market_ticker") or self._sid_to_ticker.get(msg.get("sid"))
        if not ticker or ticker not in self._books:
            return
        book = self._books[ticker]
        seq = msg.get("seq", book.seq + 1)

        # Gap detection: a missed sequence means our book is stale.
        if seq != book.seq + 1:
            log.warning(
                "Seq gap on %s (have %d, got %d) - forcing resubscribe",
                ticker, book.seq, seq,
            )
            self._resubscribe.set()
            return

        book.apply_delta(
            side=body.get("side"),
            price=body.get("price"),
            delta=body.get("delta"),
            seq=seq,
        )
