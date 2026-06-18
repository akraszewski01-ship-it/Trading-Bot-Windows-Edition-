"""
Async Kalshi v2 REST client with RSA-PSS request signing.

Kalshi's v2 API authenticates each request with three headers:

    KALSHI-ACCESS-KEY        : the API key id
    KALSHI-ACCESS-TIMESTAMP  : current unix time in milliseconds
    KALSHI-ACCESS-SIGNATURE  : base64( RSA-PSS-SHA256( timestamp + METHOD + path ) )

The signed ``path`` includes the ``/trade-api/v2`` prefix but **excludes** any
query string. The same signing scheme is used to authenticate the WebSocket
handshake (method ``GET``, path ``/trade-api/ws/v2``).

Public market-data endpoints (``GET /markets`` ...) work without signing, so the
client degrades gracefully in paper mode when no private key is configured.
"""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from ..utils.config import Config
from ..utils.logger import get_logger

log = get_logger("kalshi")

_WS_PATH = "/trade-api/ws/v2"
_API_PREFIX = "/trade-api/v2"


class KalshiAuthError(RuntimeError):
    """Raised when a signed request is attempted without valid credentials."""


class KalshiClient:
    """Thin async wrapper over the Kalshi v2 REST API."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.key_id = config.kalshi_api_key_id
        self._private_key: Optional[RSAPrivateKey] = self._maybe_load_key(
            config.kalshi_private_key_path
        )
        self._http = httpx.AsyncClient(
            base_url=config.kalshi_rest_base,
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )

    # ----------------------------------------------------------------- key load
    @staticmethod
    def _maybe_load_key(path: str) -> Optional[RSAPrivateKey]:
        try:
            with open(path, "rb") as fh:
                key = serialization.load_pem_private_key(fh.read(), password=None)
            if not isinstance(key, RSAPrivateKey):
                log.error("Kalshi private key at %s is not an RSA key", path)
                return None
            log.info("Loaded Kalshi RSA private key from %s", path)
            return key
        except FileNotFoundError:
            log.warning("Kalshi private key not found (%s); signed endpoints disabled", path)
            return None
        except Exception as exc:  # pragma: no cover - defensive
            log.error("Failed to load Kalshi private key: %s", exc)
            return None

    @property
    def is_authenticated(self) -> bool:
        return self._private_key is not None and bool(self.key_id)

    # ------------------------------------------------------------------ signing
    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        if self._private_key is None:
            raise KalshiAuthError("No Kalshi private key loaded")
        message = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _auth_headers(self, method: str, signed_path: str) -> Dict[str, str]:
        timestamp_ms = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": self._sign(timestamp_ms, method, signed_path),
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        }

    def ws_auth_headers(self) -> Dict[str, str]:
        """Signed headers for the WebSocket handshake (or ``{}`` if no key)."""
        if not self.is_authenticated:
            return {}
        return self._auth_headers("GET", _WS_PATH)

    # ------------------------------------------------------------------ request
    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        signed: bool = True,
        retries: int = 3,
    ) -> Dict[str, Any]:
        if signed and not self.is_authenticated:
            raise KalshiAuthError(
                f"{method} {endpoint} requires authentication but no key is loaded"
            )

        headers: Dict[str, str] = {}
        if signed:
            headers = self._auth_headers(method, f"{_API_PREFIX}{endpoint}")

        last_exc: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                resp = await self._http.request(
                    method, endpoint, params=params, json=json, headers=headers
                )
                if resp.status_code >= 400:
                    # 4xx are not retryable; surface the body for debugging.
                    body = resp.text[:500]
                    if resp.status_code < 500:
                        raise httpx.HTTPStatusError(
                            f"{method} {endpoint} -> {resp.status_code}: {body}",
                            request=resp.request,
                            response=resp,
                        )
                    raise httpx.HTTPStatusError(
                        f"server error {resp.status_code}: {body}",
                        request=resp.request,
                        response=resp,
                    )
                return resp.json() if resp.content else {}
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                retryable = isinstance(exc, httpx.TransportError) or (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response is not None
                    and exc.response.status_code >= 500
                )
                if not retryable or attempt == retries:
                    raise
                backoff = 2 ** (attempt - 1)
                log.warning(
                    "Kalshi %s %s failed (attempt %d/%d): %s - retrying in %ss",
                    method, endpoint, attempt, retries, exc, backoff,
                )
                await asyncio.sleep(backoff)
        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------- market data
    async def get_markets(
        self, series_ticker: str, status: str = "open", limit: int = 200
    ) -> List[Dict[str, Any]]:
        """Return open markets for a series (public endpoint)."""
        data = await self._request(
            "GET",
            "/markets",
            params={"series_ticker": series_ticker, "status": status, "limit": limit},
            signed=False,
        )
        return data.get("markets", [])

    async def get_market(self, ticker: str) -> Dict[str, Any]:
        data = await self._request("GET", f"/markets/{ticker}", signed=False)
        return data.get("market", {})

    async def get_orderbook(self, ticker: str, depth: int = 10) -> Dict[str, Any]:
        """REST orderbook snapshot - a fallback for the WebSocket feed."""
        data = await self._request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": depth}, signed=False
        )
        return data.get("orderbook", {})

    # --------------------------------------------------------------- portfolio
    async def get_balance(self) -> Dict[str, Any]:
        return await self._request("GET", "/portfolio/balance")

    async def get_positions(self) -> List[Dict[str, Any]]:
        data = await self._request("GET", "/portfolio/positions")
        return data.get("market_positions", [])

    async def get_settlements(self, limit: int = 100) -> List[Dict[str, Any]]:
        data = await self._request(
            "GET", "/portfolio/settlements", params={"limit": limit}
        )
        return data.get("settlements", [])

    async def get_fills(self, limit: int = 100) -> List[Dict[str, Any]]:
        data = await self._request("GET", "/portfolio/fills", params={"limit": limit})
        return data.get("fills", [])

    # ------------------------------------------------------------------- orders
    async def create_order(
        self,
        ticker: str,
        side: str,                # "yes" | "no"
        action: str = "buy",      # "buy" | "sell"
        count: int = 1,
        order_type: str = "limit",  # "limit" | "market"
        price_cents: Optional[int] = None,
        buy_max_cost_cents: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Submit an order.

        A *marketable limit* (``order_type='limit'`` priced at the opposing best)
        is preferred for crossing the spread because it bounds slippage while
        still filling immediately. ``order_type='market'`` is also supported and
        honours ``buy_max_cost_cents`` for slippage protection.
        """
        body: Dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": side,
            "action": action,
            "count": int(count),
            "type": order_type,
        }
        if order_type == "limit":
            if price_cents is None:
                raise ValueError("limit order requires price_cents")
            body["yes_price" if side == "yes" else "no_price"] = int(price_cents)
        elif order_type == "market" and buy_max_cost_cents is not None:
            body["buy_max_cost"] = int(buy_max_cost_cents)

        log.info("Submitting order: %s", body)
        return await self._request("POST", "/portfolio/orders", json=body)

    # -------------------------------------------------------------------- close
    async def close(self) -> None:
        await self._http.aclose()
