"""
Centralised configuration.

All tunables are loaded from environment variables (optionally via a local
``.env`` file). A single immutable :class:`Config` object is threaded through
the whole application so behaviour is reproducible and auditable.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    import sys
    print("WARNING: python-dotenv not installed. Environment variables will not be loaded from .env file.", file=sys.stderr)
    print("Run: pip install python-dotenv", file=sys.stderr)


# Mapping of Kalshi series -> spot symbol on each supported exchange.
SPOT_SYMBOLS: Dict[str, Dict[str, str]] = {
    "KXBTC15M": {"coinbase": "BTC-USD", "binance": "BTCUSDT"},
    "KXETH15M": {"coinbase": "ETH-USD", "binance": "ETHUSDT"},
}


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    """Immutable runtime configuration."""

    # --- Safety --------------------------------------------------------------
    trading_mode: str = "paper"  # "paper" | "live"

    # --- Kalshi --------------------------------------------------------------
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = "./secrets/kalshi_private_key.pem"
    kalshi_api_host: str = "https://api.elections.kalshi.com"

    # --- Gemini --------------------------------------------------------------
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    # --- Forecaster ----------------------------------------------------------
    # "auto"     -> use TimesFM only if installed AND its API matches; else baseline
    # "baseline" -> always use the built-in statistical forecaster (no heavy deps)
    # "timesfm"  -> force TimesFM (falls back to baseline if it cannot load)
    forecaster: str = "auto"

    # --- TimesFM -------------------------------------------------------------
    timesfm_backend: str = "cpu"
    timesfm_repo_id: str = "google/timesfm-2.0-500m-pytorch"
    context_len: int = 512  # rolling minutes of spot history
    horizon_len: int = 15  # forecast horizon (minutes)

    # Minimum spot bars before the baseline forecaster trusts measured vol.
    # Below this it uses ``baseline_default_vol`` so the bot can forecast (and
    # trade) immediately, with no price-history warm-up.
    min_history_bars: int = 1
    baseline_default_vol: float = 0.0012  # per-minute log-return stdev (~crypto)

    # --- Spot feed -----------------------------------------------------------
    spot_provider: str = "coinbase"  # "coinbase" | "binance"

    # --- Orderbook (WebSocket primary, public REST polling fallback) ----------
    # The Kalshi WS handshake needs valid signed auth. When that is unavailable
    # (no key, or a 401), the engine polls the PUBLIC REST orderbook instead so
    # paper trading still gets live books. These bound that polling.
    orderbook_rest_interval: float = 2.5  # seconds between REST poll cycles
    orderbook_rest_depth: int = 32        # price levels fetched per market
    orderbook_rest_max_markets: int = 40  # cap markets polled per cycle

    # --- Markets -------------------------------------------------------------
    market_series: List[str] = field(default_factory=lambda: ["KXBTC15M", "KXETH15M"])

    # --- Timing gate (minutes before expiry) ---------------------------------
    trade_window_min: float = 6.0
    trade_window_max: float = 9.0

    # --- Risk / sizing -------------------------------------------------------
    portfolio_value_usd: float = 10_000.0
    fractional_kelly: float = 0.25
    daily_stop_loss_pct: float = 0.05
    max_position_pct: float = 0.10
    base_edge_buffer: float = 0.02

    # --- AI cadence ----------------------------------------------------------
    captain_interval_sec: int = 300  # Captain (Gemini) review every 5 minutes
    forecast_interval_sec: int = 60  # TimesFM refresh every minute

    # --- Logging -------------------------------------------------------------
    log_level: str = "INFO"
    log_file: str = "trading.log"

    # ------------------------------------------------------------------ helpers
    @property
    def is_live(self) -> bool:
        return self.trading_mode.lower() == "live"

    @property
    def kalshi_rest_base(self) -> str:
        return f"{self.kalshi_api_host.rstrip('/')}/trade-api/v2"

    @property
    def kalshi_ws_url(self) -> str:
        host = self.kalshi_api_host.rstrip("/")
        host = host.replace("https://", "wss://").replace("http://", "ws://")
        return f"{host}/trade-api/ws/v2"

    def spot_symbol(self, series: str) -> str:
        """Return the spot ticker for a Kalshi series on the active provider."""
        mapping = SPOT_SYMBOLS.get(series, {})
        return mapping.get(self.spot_provider, "")

    def validate(self) -> List[str]:
        """Return a list of human-readable configuration problems (empty == ok)."""
        problems: List[str] = []
        if self.trading_mode.lower() not in ("paper", "live"):
            problems.append(f"TRADING_MODE must be paper|live, got '{self.trading_mode}'")
        if self.spot_provider not in ("coinbase", "binance"):
            problems.append(f"SPOT_PROVIDER must be coinbase|binance, got '{self.spot_provider}'")
        for series in self.market_series:
            if series not in SPOT_SYMBOLS:
                problems.append(f"No spot symbol mapping for series '{series}'")
        if self.is_live:
            if not self.kalshi_api_key_id:
                problems.append("KALSHI_API_KEY_ID is required in live mode")
            if not os.path.exists(self.kalshi_private_key_path):
                problems.append(
                    f"Kalshi private key not found at '{self.kalshi_private_key_path}'"
                )
        if self.trade_window_min >= self.trade_window_max:
            problems.append("TRADE_WINDOW_MIN must be < TRADE_WINDOW_MAX")
        return problems


def load_config() -> Config:
    """Build a :class:`Config` from the current environment."""
    series_raw = _get("MARKET_SERIES", "KXBTC15M,KXETH15M")
    series = [s.strip().upper() for s in series_raw.split(",") if s.strip()]

    return Config(
        trading_mode=_get("TRADING_MODE", "paper").lower(),
        kalshi_api_key_id=_get("KALSHI_API_KEY_ID"),
        kalshi_private_key_path=_get("KALSHI_PRIVATE_KEY_PATH", "./secrets/kalshi_private_key.pem"),
        kalshi_api_host=_get("KALSHI_API_HOST", "https://api.elections.kalshi.com"),
        gemini_api_key=_get("GEMINI_API_KEY"),
        gemini_model=_get("GEMINI_MODEL", "gemini-2.0-flash"),
        forecaster=_get("FORECASTER", "auto").lower(),
        timesfm_backend=_get("TIMESFM_BACKEND", "cpu").lower(),
        timesfm_repo_id=_get("TIMESFM_REPO_ID", "google/timesfm-2.0-500m-pytorch"),
        min_history_bars=_get_int("MIN_HISTORY_BARS", 1),
        baseline_default_vol=_get_float("BASELINE_DEFAULT_VOL", 0.0012),
        spot_provider=_get("SPOT_PROVIDER", "coinbase").lower(),
        orderbook_rest_interval=_get_float("ORDERBOOK_REST_INTERVAL", 2.5),
        orderbook_rest_depth=_get_int("ORDERBOOK_REST_DEPTH", 32),
        orderbook_rest_max_markets=_get_int("ORDERBOOK_REST_MAX_MARKETS", 40),
        market_series=series,
        # Wider default window (0-13 min) so the bot evaluates markets across
        # most of their life and trades actively in paper mode. Narrow it via
        # .env for a more selective live strategy.
        trade_window_min=_get_float("TRADE_WINDOW_MIN", 0.0),
        trade_window_max=_get_float("TRADE_WINDOW_MAX", 13.0),
        base_edge_buffer=_get_float("BASE_EDGE_BUFFER", 0.02),
        portfolio_value_usd=_get_float("PORTFOLIO_VALUE_USD", 10_000.0),
        fractional_kelly=_get_float("FRACTIONAL_KELLY", 0.25),
        daily_stop_loss_pct=_get_float("DAILY_STOP_LOSS_PCT", 0.05),
        max_position_pct=_get_float("MAX_POSITION_PCT", 0.10),
        captain_interval_sec=_get_int("CAPTAIN_INTERVAL_SEC", 300),
        forecast_interval_sec=_get_int("FORECAST_INTERVAL_SEC", 60),
        log_level=_get("LOG_LEVEL", "INFO").upper(),
        log_file=_get("LOG_FILE", "trading.log"),
    )
