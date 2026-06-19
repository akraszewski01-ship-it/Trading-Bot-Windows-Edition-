"""
Execution engine.

Fuses every signal into trade decisions and (paper or live) orders:

* **Market discovery** - pulls open markets per series, parses strike/expiry and
  keeps the orderbook WebSocket subscribed to soon-to-expire markets.
* **Timing gate** - a market is only *evaluated* when it is between
  ``trade_window_min`` and ``trade_window_max`` minutes from expiry (default
  6-9 min).
* **Spread-crossing logic** - a market order is only sent when the statistical
  edge (model probability vs. orderbook mid) exceeds the live spread *plus* a
  dynamic buffer (base buffer + Captain's ``min_edge_threshold`` + a volatility
  term).
* **Risk** - Kelly sizing scaled by the Captain's multiplier, behind hard
  circuit breakers.
* **Settlement** - paper trades settle against realised spot; live trades reconcile
  from Kalshi settlements.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..ai.captain import CaptainDecision
from ..ai.timesfm_predictor import Forecast
from ..utils.config import Config
from ..utils.logger import get_logger
from ..utils.risk import RiskManager, TradeResult
from .kalshi_client import KalshiClient
from .market_data import MarketDataFeed
from .orderbook import OrderBookManager

log = get_logger("execution")


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------- #
#  Market specification
# --------------------------------------------------------------------------- #
@dataclass
class MarketSpec:
    ticker: str
    series: str
    close_time: Optional[datetime]
    strike_type: Optional[str]
    floor_strike: Optional[float]
    cap_strike: Optional[float]
    title: str = ""

    @classmethod
    def from_raw(cls, raw: dict, series: str) -> "MarketSpec":
        def _num(v):
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        return cls(
            ticker=raw.get("ticker", ""),
            series=series,
            close_time=_parse_dt(raw.get("close_time") or raw.get("expiration_time")),
            strike_type=raw.get("strike_type"),
            floor_strike=_num(raw.get("floor_strike")),
            cap_strike=_num(raw.get("cap_strike")),
            title=raw.get("title") or raw.get("subtitle") or "",
        )

    def minutes_to_expiry(self, now: Optional[datetime] = None) -> Optional[float]:
        if self.close_time is None:
            return None
        now = now or datetime.now(timezone.utc)
        return (self.close_time - now).total_seconds() / 60.0

    def classify(self) -> str:
        st = (self.strike_type or "").lower()
        if "between" in st or (self.floor_strike is not None and self.cap_strike is not None):
            return "between"
        if "less" in st or (self.cap_strike is not None and self.floor_strike is None):
            return "below"
        if "greater" in st or (self.floor_strike is not None and self.cap_strike is None):
            return "above"
        return "unknown"

    def model_probability(self, forecast: Forecast, minute: int) -> Optional[float]:
        kind = self.classify()
        if kind == "above":
            return forecast.prob_above(minute, self.floor_strike)
        if kind == "below":
            return forecast.prob_below(minute, self.cap_strike)
        if kind == "between":
            return forecast.prob_between(minute, self.floor_strike, self.cap_strike)
        return None

    def settles_yes(self, spot: float) -> Optional[bool]:
        kind = self.classify()
        if kind == "above":
            return spot >= self.floor_strike
        if kind == "below":
            return spot <= self.cap_strike
        if kind == "between":
            return self.floor_strike <= spot <= self.cap_strike
        return None


# --------------------------------------------------------------------------- #
#  Decisions & positions
# --------------------------------------------------------------------------- #
@dataclass
class TradeDecision:
    ticker: str
    series: str
    minutes_to_expiry: float
    side: Optional[str] = None          # "yes" | "no"
    model_prob: float = 0.0
    market_prob: float = 0.0
    edge: float = 0.0
    spread: float = 0.0
    required: float = 0.0
    price_cents: int = 0
    contracts: int = 0
    executed: bool = False
    reason: str = ""

    def log_line(self) -> str:
        return (
            f"{self.ticker} mte={self.minutes_to_expiry:.1f} "
            f"side={self.side} model_p={self.model_prob:.3f} mkt_p={self.market_prob:.3f} "
            f"edge={self.edge:+.3f} spread={self.spread:.3f} req={self.required:.3f} "
            f"px={self.price_cents}c n={self.contracts} exec={self.executed} :: {self.reason}"
        )


@dataclass
class OpenPosition:
    spec: MarketSpec
    side: str
    contracts: int
    entry_price_cents: int
    prob_win: float
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# --------------------------------------------------------------------------- #
#  Engine
# --------------------------------------------------------------------------- #
class ExecutionEngine:
    def __init__(
        self,
        config: Config,
        client: KalshiClient,
        orderbook: OrderBookManager,
        feed: MarketDataFeed,
        risk: RiskManager,
    ) -> None:
        self.config = config
        self.client = client
        self.orderbook = orderbook
        self.feed = feed
        self.risk = risk

        self._specs: Dict[str, MarketSpec] = {}
        self._forecasts: Dict[str, Forecast] = {}
        self._captain: CaptainDecision = CaptainDecision.conservative_default()
        self._open: Dict[str, OpenPosition] = {}
        self._settled: set[str] = set()
        # Live diagnostics for the dashboard (why no trade is firing, etc.).
        self._last_eval: Dict[str, str] = {}
        self._markets_in_window: int = 0

    # ----------------------------------------------------------- shared updates
    def update_forecast(self, series: str, forecast: Forecast) -> None:
        self._forecasts[series] = forecast

    def apply_captain(self, decision: CaptainDecision) -> None:
        self._captain = decision
        # The Captain's halt flag feeds the (overridable) risk halt switch.
        self.risk.set_halt(decision.halt_trading, reason="captain halt")

    # ----------------------------------------------------------- market refresh
    async def refresh_markets(self) -> List[str]:
        """Fetch open markets, keep specs, and return tickers to keep subscribed."""
        active: List[str] = []
        for series in self.config.market_series:
            try:
                markets = await self.client.get_markets(series, status="open")
            except Exception as exc:
                log.error("Failed to fetch markets for %s: %s", series, exc)
                continue
            for raw in markets:
                spec = MarketSpec.from_raw(raw, series)
                if not spec.ticker:
                    continue
                self._specs[spec.ticker] = spec
                mte = spec.minutes_to_expiry()
                # Warm the book for anything expiring within ~20 min.
                if mte is not None and 0 < mte <= 20:
                    active.append(spec.ticker)
        self.orderbook.set_tickers(active)
        log.debug("Refreshed markets: %d specs, %d active subscriptions",
                  len(self._specs), len(active))
        return active

    # ------------------------------------------------------------- evaluation
    async def evaluate(self) -> List[TradeDecision]:
        """Apply the timing gate + spread-crossing logic across all markets."""
        decisions: List[TradeDecision] = []
        now = datetime.now(timezone.utc)
        self._last_eval = {}
        in_window = 0

        allowed, why = self.risk.can_trade()
        if not allowed:
            log.debug("Risk gate closed: %s", why)
            self._last_eval["_risk_gate"] = f"trading halted: {why}"
            self._markets_in_window = 0
            return decisions

        for ticker, spec in list(self._specs.items()):
            if ticker in self._open:
                continue  # one entry per market
            mte = spec.minutes_to_expiry(now)
            if mte is None:
                continue
            # --- Timing gate ----------------------------------------------------
            if not (self.config.trade_window_min <= mte <= self.config.trade_window_max):
                continue
            in_window += 1

            decision = self._evaluate_market(spec, mte)
            if decision is None:
                continue
            self._last_eval[ticker] = decision.reason
            decisions.append(decision)
            if decision.side and decision.contracts > 0 and not decision.executed:
                await self._execute(spec, decision)

        self._markets_in_window = in_window
        return decisions

    # ------------------------------------------------------------- diagnostics
    def diagnostics(self) -> dict:
        """Live snapshot of the pipeline for the dashboard's system-status panel."""
        now = datetime.now(timezone.utc)
        # Count markets per series and the soonest expiry in each.
        per_series: Dict[str, dict] = {}
        for spec in self._specs.values():
            d = per_series.setdefault(spec.series, {"count": 0, "next_expiry_min": None})
            d["count"] += 1
            mte = spec.minutes_to_expiry(now)
            if mte is not None and mte > 0:
                cur = d["next_expiry_min"]
                if cur is None or mte < cur:
                    d["next_expiry_min"] = round(mte, 1)

        quoted = 0
        for ticker in self._specs:
            q = self.orderbook.get_quote(ticker)
            if q and q.is_two_sided:
                quoted += 1

        # A sample of the soonest-expiring markets with strike + quote info, so
        # the dashboard can show exactly what the engine is looking at.
        sample = []
        specs_sorted = sorted(
            self._specs.values(),
            key=lambda s: (s.minutes_to_expiry(now) is None, s.minutes_to_expiry(now) or 1e9),
        )
        for spec in specs_sorted[:8]:
            mte = spec.minutes_to_expiry(now)
            q = self.orderbook.get_quote(spec.ticker)
            sample.append({
                "ticker": spec.ticker,
                "mte": round(mte, 1) if mte is not None else None,
                "kind": spec.classify(),
                "in_window": (mte is not None
                              and self.config.trade_window_min <= mte <= self.config.trade_window_max),
                "yes_bid": q.yes_bid if q else None,
                "yes_ask": q.yes_ask if q else None,
            })

        return {
            "kalshi_authenticated": self.client.is_authenticated,
            "orderbook_connected": self.orderbook.is_connected,
            "spot_connected": self.feed.is_connected,
            "captain_source": self._captain.source,
            "captain_regime": self._captain.market_regime,
            "markets_tracked": len(self._specs),
            "markets_in_window": self._markets_in_window,
            "markets_quoted": quoted,
            "forecasts_ready": sorted(self._forecasts.keys()),
            "per_series": per_series,
            "trade_window": [self.config.trade_window_min, self.config.trade_window_max],
            "skip_reasons": dict(list(self._last_eval.items())[:12]),
            "market_sample": sample,
        }

    def _evaluate_market(self, spec: MarketSpec, mte: float) -> Optional[TradeDecision]:
        d = TradeDecision(ticker=spec.ticker, series=spec.series, minutes_to_expiry=mte)

        forecast = self._forecasts.get(spec.series)
        if forecast is None:
            d.reason = "no forecast yet"
            return d

        model_p = spec.model_probability(forecast, int(round(mte)))
        if model_p is None:
            d.reason = f"unknown strike layout ({spec.strike_type})"
            return d
        d.model_prob = model_p

        quote = self.orderbook.get_quote(spec.ticker)
        if quote is None or not quote.is_two_sided:
            d.reason = "no two-sided quote"
            return d

        market_p = quote.implied_prob
        d.market_prob = market_p
        d.edge = model_p - market_p
        d.spread = (quote.spread or 0) / 100.0

        # --- Dynamic buffer: base + Captain threshold + volatility term ---------
        sigma = forecast.sigma_at(int(round(mte)))
        rel_vol = sigma / max(forecast.mean_at(int(round(mte))), 1e-9)
        vol_term = min(0.04, 3.0 * rel_vol)
        d.required = self.config.base_edge_buffer + self._captain.min_edge_threshold + vol_term

        # --- Spread-crossing rule: |edge| must beat spread + dynamic buffer -----
        if abs(d.edge) <= d.spread + d.required:
            d.reason = "edge below spread+buffer"
            return d

        # --- Direction + crossing price ----------------------------------------
        if d.edge > 0:
            d.side = "yes"
            d.price_cents = int(quote.yes_ask)
            prob_win = model_p
            available = quote.yes_ask_size
        else:
            d.side = "no"
            d.price_cents = int(100 - quote.yes_bid)
            prob_win = 1.0 - model_p
            available = quote.yes_bid_size

        if not (0 < d.price_cents < 100):
            d.reason = f"degenerate price {d.price_cents}"
            d.side = None
            return d

        # --- Sizing (Kelly x fractional x captain), capped by liquidity ---------
        sizing = self.risk.size_position(prob_win, d.price_cents, self._captain.kelly_multiplier)
        contracts = min(sizing.contracts, max(0, int(available)))
        d.contracts = contracts
        if contracts <= 0:
            d.reason = (
                f"size=0 ({sizing.reason}; kelly_raw={sizing.kelly_fraction_raw:.3f}, "
                f"avail={available})"
            )
            d.side = None
            return d

        d.reason = (
            f"ENTER {d.side} (kelly_applied={sizing.kelly_fraction_applied:.3f}"
            f"{', max-capped' if sizing.capped_by_max_position else ''})"
        )
        return d

    # ------------------------------------------------------------------ execute
    async def _execute(self, spec: MarketSpec, d: TradeDecision) -> None:
        # Record the position immediately to prevent duplicate entries.
        self._open[spec.ticker] = OpenPosition(
            spec=spec,
            side=d.side,
            contracts=d.contracts,
            entry_price_cents=d.price_cents,
            prob_win=d.model_prob if d.side == "yes" else 1.0 - d.model_prob,
        )

        if not self.config.is_live:
            d.executed = True
            log.info("[PAPER] ORDER %s", d.log_line())
            return

        if not self.client.is_authenticated:
            log.critical("LIVE mode but Kalshi client is not authenticated - skipping order")
            d.reason += " | BLOCKED: no auth"
            return

        try:
            # Marketable limit at the crossing price bounds slippage while filling now.
            result = await self.client.create_order(
                ticker=spec.ticker,
                side=d.side,
                action="buy",
                count=d.contracts,
                order_type="limit",
                price_cents=d.price_cents,
            )
            d.executed = True
            log.info("[LIVE] ORDER ACCEPTED %s | resp=%s", d.log_line(), result.get("order", {}))
        except Exception as exc:  # includes KalshiAuthError
            log.error("[LIVE] order failed for %s: %s", spec.ticker, exc)
            d.reason += f" | ORDER FAILED: {exc}"
            # Roll back the reservation so it can be retried next cycle.
            self._open.pop(spec.ticker, None)

    # --------------------------------------------------------------- settlement
    async def settle(self) -> None:
        if self.config.is_live:
            await self._settle_live()
        else:
            self._settle_paper()

    def _settle_paper(self) -> None:
        now = datetime.now(timezone.utc)
        for ticker, pos in list(self._open.items()):
            mte = pos.spec.minutes_to_expiry(now)
            if mte is None or mte > 0:
                continue  # not expired yet
            symbol = self.config.spot_symbol(pos.spec.series)
            final = self.feed.latest(symbol)
            if final is None:
                continue
            yes_wins = pos.spec.settles_yes(final)
            if yes_wins is None:
                self._open.pop(ticker, None)
                continue
            won = yes_wins if pos.side == "yes" else (not yes_wins)
            c = pos.entry_price_cents / 100.0
            pnl = pos.contracts * (1.0 - c) if won else -pos.contracts * c
            self.risk.record_settlement(
                TradeResult(
                    ticker=ticker,
                    side=pos.side,
                    contracts=pos.contracts,
                    entry_price_cents=pos.entry_price_cents,
                    pnl_dollars=pnl,
                    won=won,
                )
            )
            self._open.pop(ticker, None)

    async def _settle_live(self) -> None:
        """Reconcile realised PnL from Kalshi settlements for our open tickers."""
        if not self.client.is_authenticated or not self._open:
            return
        try:
            settlements = await self.client.get_settlements(limit=100)
        except Exception as exc:
            log.error("Failed to fetch settlements: %s", exc)
            return
        for s in settlements:
            ticker = s.get("ticker")
            pos = self._open.get(ticker)
            if pos is None or ticker in self._settled:
                continue
            result = (s.get("market_result") or "").lower()  # "yes" | "no"
            won = (result == "yes") if pos.side == "yes" else (result == "no")
            c = pos.entry_price_cents / 100.0
            pnl = pos.contracts * (1.0 - c) if won else -pos.contracts * c
            self.risk.record_settlement(
                TradeResult(
                    ticker=ticker,
                    side=pos.side,
                    contracts=pos.contracts,
                    entry_price_cents=pos.entry_price_cents,
                    pnl_dollars=pnl,
                    won=won,
                )
            )
            self._settled.add(ticker)
            self._open.pop(ticker, None)

    # ----------------------------------------------------------- captain context
    def build_captain_context(self) -> dict:
        """Assemble the payload the Captain reasons over."""
        forecasts: Dict[str, dict] = {}
        first_summary: dict = {}
        for series, fc in self._forecasts.items():
            # Summarise at the centre of the trade window.
            minute = int((self.config.trade_window_min + self.config.trade_window_max) / 2)
            summary = fc.summary(minute)
            forecasts[series] = summary
            if not first_summary:
                first_summary = summary

        skew: Dict[str, dict] = {}
        for series in self.config.market_series:
            probs, imbalances = [], []
            for ticker, spec in self._specs.items():
                if spec.series != series:
                    continue
                q = self.orderbook.get_quote(ticker)
                if q and q.is_two_sided:
                    probs.append(q.implied_prob)
                    denom = q.yes_bid_size + q.yes_ask_size
                    if denom > 0:
                        imbalances.append((q.yes_bid_size - q.yes_ask_size) / denom)
            if probs:
                skew[series] = {
                    "avg_implied_prob": round(sum(probs) / len(probs), 4),
                    "avg_bid_ask_imbalance": round(
                        sum(imbalances) / len(imbalances), 4
                    ) if imbalances else 0.0,
                    "markets_quoted": len(probs),
                }

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trading_mode": self.config.trading_mode,
            "forecasts": forecasts,
            "forecast": first_summary,  # convenience for the heuristic fallback
            "orderbook_skew": skew,
            "risk": self.risk.snapshot(),
            "open_positions": len(self._open),
            "previous_captain": self._captain.summary(),
        }
