"""
Risk & money-management.

Responsibilities
----------------
* **Kelly position sizing** for binary (0/1) Kalshi contracts, scaled by the
  Captain's ``kelly_multiplier`` and a static fractional-Kelly safety scalar.
* **Hard circuit breakers** that cannot be overridden by the AI layer:
    - daily stop-loss (default 5% of the day's starting equity)
    - maximum position size (default 10% of portfolio value)
* **Win/loss bookkeeping** so the Captain can reason about recent performance.

All monetary maths is done in *dollars*; Kalshi contract prices are integer
cents in ``[1, 99]`` where a YES contract costing ``c`` cents pays out 100 cents
if it settles YES.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Optional, Tuple

from .logger import get_logger

log = get_logger("risk")


# --------------------------------------------------------------------------- #
#  Data containers
# --------------------------------------------------------------------------- #
@dataclass
class TradeResult:
    """A settled trade outcome, used for PnL and win-rate tracking."""

    ticker: str
    side: str               # "yes" | "no"
    contracts: int
    entry_price_cents: int
    pnl_dollars: float
    won: bool
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class PositionSizing:
    """Result of a sizing request - fully transparent for the audit log."""

    contracts: int
    dollars: float
    kelly_fraction_raw: float       # uncapped Kelly fraction
    kelly_fraction_applied: float   # after fractional + captain scaling
    capped_by_max_position: bool
    reason: str = "ok"


# --------------------------------------------------------------------------- #
#  Kelly maths
# --------------------------------------------------------------------------- #
def kelly_fraction(prob_win: float, price_cents: float) -> float:
    """
    Optimal Kelly fraction for a binary contract.

    Buying at ``c = price_cents/100`` dollars wins ``(1 - c)`` and loses ``c``.
    Net odds ``b = (1 - c) / c`` ->  ``f* = p - (1 - p) / b``.

    Returns 0.0 when the bet is non-positive-expectancy or the price is
    degenerate.
    """
    p = max(0.0, min(1.0, prob_win))
    c = price_cents / 100.0
    if not (0.0 < c < 1.0):
        return 0.0
    b = (1.0 - c) / c
    f = p - (1.0 - p) / b
    return max(0.0, f)


# --------------------------------------------------------------------------- #
#  Risk manager
# --------------------------------------------------------------------------- #
class RiskManager:
    """Thread-safe risk gatekeeper and position sizer."""

    def __init__(
        self,
        portfolio_value: float,
        fractional_kelly: float = 0.25,
        daily_stop_loss_pct: float = 0.05,
        max_position_pct: float = 0.10,
        win_history: int = 50,
    ) -> None:
        self._lock = threading.Lock()
        self.portfolio_value = float(portfolio_value)

        self.fractional_kelly = float(fractional_kelly)
        self.daily_stop_loss_pct = float(daily_stop_loss_pct)
        self.max_position_pct = float(max_position_pct)

        # Daily PnL tracking (realised, from settlements).
        self._day = self._today()
        self.day_start_equity = float(portfolio_value)
        self.daily_pnl = 0.0

        # Rolling win/loss outcomes (1 = win, 0 = loss).
        self._outcomes: Deque[int] = deque(maxlen=win_history)

        # Hard manual / Captain halt switch.
        self._halted = False
        self._halt_reason = ""

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _roll_day_if_needed(self) -> None:
        today = self._today()
        if today != self._day:
            log.info(
                "New trading day %s - resetting daily PnL (prev day pnl=%.2f)",
                today,
                self.daily_pnl,
            )
            self._day = today
            self.day_start_equity = self.portfolio_value
            self.daily_pnl = 0.0

    # ------------------------------------------------------------- public state
    @property
    def win_rate(self) -> float:
        with self._lock:
            if not self._outcomes:
                return 0.0
            return sum(self._outcomes) / len(self._outcomes)

    @property
    def trades_recorded(self) -> int:
        with self._lock:
            return len(self._outcomes)

    @property
    def is_halted(self) -> bool:
        with self._lock:
            return self._halted

    def set_halt(self, halted: bool, reason: str = "") -> None:
        with self._lock:
            if halted and not self._halted:
                log.warning("TRADING HALTED: %s", reason or "unspecified")
            elif not halted and self._halted:
                log.warning("Trading halt CLEARED")
            self._halted = halted
            self._halt_reason = reason

    def update_portfolio_value(self, value: float) -> None:
        with self._lock:
            self.portfolio_value = float(value)

    # ------------------------------------------------------------- circuit break
    def can_trade(self) -> Tuple[bool, str]:
        """Return ``(allowed, reason)`` after evaluating all circuit breakers."""
        with self._lock:
            self._roll_day_if_needed()
            if self._halted:
                return False, f"halted: {self._halt_reason or 'manual/captain'}"
            loss_limit = -self.daily_stop_loss_pct * self.day_start_equity
            if self.daily_pnl <= loss_limit:
                return (
                    False,
                    f"daily stop-loss hit (pnl={self.daily_pnl:.2f} <= "
                    f"limit={loss_limit:.2f})",
                )
            return True, "ok"

    # ------------------------------------------------------------------- sizing
    def size_position(
        self,
        prob_win: float,
        price_cents: int,
        kelly_multiplier: float,
    ) -> PositionSizing:
        """
        Compute the number of contracts to buy.

        ``kelly_multiplier`` comes from the Captain (0..1). The final fraction is
        ``kelly* x fractional_kelly x kelly_multiplier`` and the resulting dollar
        notional is capped at ``max_position_pct`` of the portfolio.
        """
        with self._lock:
            f_raw = kelly_fraction(prob_win, price_cents)
            mult = max(0.0, min(1.0, kelly_multiplier))
            f_applied = f_raw * self.fractional_kelly * mult

            if f_applied <= 0.0:
                return PositionSizing(
                    contracts=0,
                    dollars=0.0,
                    kelly_fraction_raw=f_raw,
                    kelly_fraction_applied=f_applied,
                    capped_by_max_position=False,
                    reason="non-positive Kelly fraction",
                )

            dollars = f_applied * self.portfolio_value
            max_dollars = self.max_position_pct * self.portfolio_value
            capped = dollars > max_dollars
            dollars = min(dollars, max_dollars)

            cost_per_contract = price_cents / 100.0
            contracts = int(dollars // cost_per_contract) if cost_per_contract > 0 else 0

            return PositionSizing(
                contracts=contracts,
                dollars=contracts * cost_per_contract,
                kelly_fraction_raw=f_raw,
                kelly_fraction_applied=f_applied,
                capped_by_max_position=capped,
                reason="ok" if contracts > 0 else "rounds to 0 contracts",
            )

    # --------------------------------------------------------------- settlement
    def record_settlement(self, result: TradeResult) -> None:
        """Record a settled trade: update realised PnL, equity and win-rate."""
        with self._lock:
            self._roll_day_if_needed()
            self.daily_pnl += result.pnl_dollars
            self.portfolio_value += result.pnl_dollars
            self._outcomes.append(1 if result.won else 0)
            log.info(
                "Settled %s %s x%d @ %dc -> pnl=%.2f (%s) | daily_pnl=%.2f "
                "win_rate=%.0f%% (n=%d)",
                result.ticker,
                result.side,
                result.contracts,
                result.entry_price_cents,
                result.pnl_dollars,
                "WIN" if result.won else "LOSS",
                self.daily_pnl,
                self.win_rate_unlocked() * 100,
                len(self._outcomes),
            )

    def win_rate_unlocked(self) -> float:
        # Internal helper assuming the lock is already held.
        if not self._outcomes:
            return 0.0
        return sum(self._outcomes) / len(self._outcomes)

    # ------------------------------------------------------------------ context
    def snapshot(self) -> dict:
        """Lightweight state dict for the Captain context payload / logging."""
        with self._lock:
            self._roll_day_if_needed()
            return {
                "portfolio_value": round(self.portfolio_value, 2),
                "daily_pnl": round(self.daily_pnl, 2),
                "daily_pnl_pct": round(
                    100 * self.daily_pnl / self.day_start_equity, 3
                )
                if self.day_start_equity
                else 0.0,
                "daily_stop_loss_pct": self.daily_stop_loss_pct * 100,
                "win_rate": round(self.win_rate_unlocked(), 4),
                "trades_recorded": len(self._outcomes),
                "halted": self._halted,
            }
