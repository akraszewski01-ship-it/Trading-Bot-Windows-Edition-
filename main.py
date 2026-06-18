"""
Entry point for the Kalshi 15-minute crypto trading system (Windows edition).

Wires the spot feed, Kalshi orderbook WebSocket, TimesFM forecaster and the
Gemini "Captain" into the execution engine, then runs a set of cooperative
asyncio loops:

    feed.run()            spot price WebSocket (512-min rolling window)
    orderbook.run()       Kalshi orderbook WebSocket
    market_refresh_loop   discover open markets / manage subscriptions
    forecast_loop         TimesFM forecast per series (every minute)
    captain_loop          Gemini oversight (every 5 minutes)
    trading_loop          timing gate + spread-crossing execution + settlement

Run:
    python main.py              # paper or live per .env (TRADING_MODE)
    python main.py --self-test  # offline smoke test (no network/credentials)
"""

from __future__ import annotations

import asyncio
import sys

from src.ai import Captain, create_predictor
from src.utils import RiskManager, get_logger, load_config, setup_logging

# NOTE: src.engine pulls in httpx / websockets (network deps). It is imported
# lazily inside run() so the offline --self-test works with only the standard
# library + the lightweight AI/utils modules installed.
if True:  # typing aid without importing at module load time
    from typing import TYPE_CHECKING

    if TYPE_CHECKING:  # pragma: no cover
        from src.engine import ExecutionEngine, MarketDataFeed

log = get_logger("main")


# --------------------------------------------------------------------------- #
#  Cooperative loops
# --------------------------------------------------------------------------- #
async def market_refresh_loop(engine: ExecutionEngine, interval: int = 30) -> None:
    while True:
        try:
            await engine.refresh_markets()
        except Exception as exc:  # pragma: no cover - defensive
            log.error("market_refresh_loop: %s", exc)
        await asyncio.sleep(interval)


async def forecast_loop(config, feed: MarketDataFeed, predictor, engine: ExecutionEngine) -> None:
    from src.utils.bot_state import BotState
    minute = int((config.trade_window_min + config.trade_window_max) / 2)
    while True:
        forecasts_snap = {}
        spot_snap = {}
        for series in config.market_series:
            symbol = config.spot_symbol(series)
            latest = feed.latest(symbol)
            if latest:
                spot_snap[series] = round(latest, 2)
            if not feed.ready(symbol):
                continue
            try:
                forecast = await asyncio.to_thread(predictor.forecast, feed.get_window(symbol))
                engine.update_forecast(series, forecast)
                forecasts_snap[series] = forecast.summary(minute)
                log.info("Forecast %s: %s", series, forecast.summary(minute))
            except Exception as exc:
                log.error("forecast_loop %s: %s", series, exc)
        BotState.update({"forecasts": forecasts_snap, "spot": spot_snap})
        await asyncio.sleep(config.forecast_interval_sec)


async def captain_loop(config, captain: Captain, engine: ExecutionEngine) -> None:
    from src.utils.bot_state import BotState
    await asyncio.sleep(15)
    while True:
        try:
            context = engine.build_captain_context()
            decision = await captain.review(context)
            engine.apply_captain(decision)
            BotState.update({
                "captain": {
                    "regime": decision.market_regime,
                    "kelly_mult": decision.kelly_multiplier,
                    "min_edge": decision.min_edge_threshold,
                    "halt": decision.halt_trading,
                    "reasoning": decision.reasoning,
                    "source": decision.source,
                    "ts": decision.ts.isoformat(),
                }
            })
        except Exception as exc:
            log.error("captain_loop: %s", exc)
        await asyncio.sleep(config.captain_interval_sec)


async def stop_watcher(interval: int = 1) -> None:
    """Watch BotState for a dashboard 'Stop' request and exit when set."""
    from src.utils.bot_state import BotState
    while True:
        if BotState.get().get("stop_requested"):
            log.info("Stop requested from dashboard - shutting down...")
            # Clear the flag so a fresh start is not immediately stopped.
            BotState.update({"stop_requested": False, "status": "stopped"})
            return
        await asyncio.sleep(interval)


async def dashboard_loop(port: int = 8080) -> None:
    """Run the monitoring dashboard IN-PROCESS so it shares BotState with the
    trading engine (live data, working Stop button). Falls back quietly if
    FastAPI/uvicorn are not installed."""
    try:
        import uvicorn
        from dashboard.server import app
    except Exception as exc:  # pragma: no cover - optional dependency
        log.warning("Dashboard not started (%s). Install fastapi+uvicorn to enable.", exc)
        return

    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    log.info("Dashboard -> http://localhost:%d", port)
    try:
        await server.serve()
    except asyncio.CancelledError:
        server.should_exit = True
        raise


async def trading_loop(engine: ExecutionEngine, interval: int = 5) -> None:
    from src.utils.bot_state import BotState
    while True:
        try:
            decisions = await engine.evaluate()
            for d in decisions:
                if d.side or d.executed:
                    log.info("DECISION %s", d.log_line())
                    BotState.push_decision({
                        "ticker": d.ticker,
                        "side": d.side,
                        "contracts": d.contracts,
                        "price_cents": d.price_cents,
                        "edge": round(d.edge, 4),
                        "executed": d.executed,
                        "reason": d.reason,
                        "mte": round(d.minutes_to_expiry, 1),
                    })
            await engine.settle()
            snap = engine.risk.snapshot()
            BotState.update({
                "status": "running",
                "portfolio": snap,
                "circuit_breaker": {
                    "halted": snap.get("halted", False),
                    "reason": "",
                },
                "positions": [
                    {
                        "ticker": t,
                        "side": p.side,
                        "contracts": p.contracts,
                        "entry_price_cents": p.entry_price_cents,
                        "prob_win": round(p.prob_win, 3),
                        "opened_at": p.opened_at.isoformat(),
                    }
                    for t, p in engine._open.items()
                ],
                "quotes": {
                    t: {
                        "bid": q.yes_bid,
                        "ask": q.yes_ask,
                        "spread": q.spread,
                        "mid": round(q.mid, 1) if q.mid else None,
                    }
                    for t in list(engine._specs)[:20]
                    if (q := engine.orderbook.get_quote(t)) and q.is_two_sided
                },
            })
        except Exception as exc:
            log.exception("trading_loop: %s", exc)
        await asyncio.sleep(interval)


# --------------------------------------------------------------------------- #
#  Bootstrap
# --------------------------------------------------------------------------- #
async def run() -> None:
    # Network-dependent engine imported here so --self-test stays offline.
    from src.engine import (
        ExecutionEngine,
        KalshiClient,
        MarketDataFeed,
        OrderBookManager,
    )

    config = load_config()
    setup_logging(config.log_file, config.log_level)

    from src.utils.bot_state import BotState
    BotState.update({"status": "starting", "mode": config.trading_mode})

    log.info("=" * 70)
    log.info("Kalshi 15m Crypto Trading System - mode=%s provider=%s series=%s",
             config.trading_mode.upper(), config.spot_provider, config.market_series)
    log.info("=" * 70)

    problems = config.validate()
    for p in problems:
        log.warning("CONFIG: %s", p)
    if config.is_live and problems:
        log.critical("Refusing to start LIVE with configuration problems. Exiting.")
        return

    # --- Components ---------------------------------------------------------
    client = KalshiClient(config)
    orderbook = OrderBookManager(config, client)
    symbols = [config.spot_symbol(s) for s in config.market_series]
    feed = MarketDataFeed(config, symbols)
    predictor = create_predictor(config)
    captain = Captain(config)

    portfolio_value = config.portfolio_value_usd
    if config.is_live and client.is_authenticated:
        try:
            bal = await client.get_balance()
            portfolio_value = float(bal.get("balance", 0)) / 100.0 or portfolio_value
            log.info("Live Kalshi balance: $%.2f", portfolio_value)
        except Exception as exc:
            log.error("Could not fetch balance (%s); using configured value", exc)

    risk = RiskManager(
        portfolio_value=portfolio_value,
        fractional_kelly=config.fractional_kelly,
        daily_stop_loss_pct=config.daily_stop_loss_pct,
        max_position_pct=config.max_position_pct,
    )
    engine = ExecutionEngine(config, client, orderbook, feed, risk)

    log.info("Forecaster=%s | Captain=%s | Portfolio=$%.2f",
             getattr(predictor, "source", "?"),
             "gemini" if captain.enabled else "heuristic",
             portfolio_value)

    # --- Warm up ------------------------------------------------------------
    await feed.seed()
    await engine.refresh_markets()

    # --- Launch loops -------------------------------------------------------
    tasks = [
        asyncio.create_task(feed.run(), name="feed"),
        asyncio.create_task(orderbook.run(), name="orderbook"),
        asyncio.create_task(market_refresh_loop(engine), name="market_refresh"),
        asyncio.create_task(forecast_loop(config, feed, predictor, engine), name="forecast"),
        asyncio.create_task(captain_loop(config, captain, engine), name="captain"),
        asyncio.create_task(trading_loop(engine), name="trading"),
        asyncio.create_task(stop_watcher(), name="stop_watcher"),
        asyncio.create_task(dashboard_loop(), name="dashboard"),
    ]
    log.info("All systems live. %d loops running. Ctrl+C (or dashboard Stop) to stop.", len(tasks))

    try:
        # Run until any task finishes (stop_watcher returns when Stop is pressed)
        # or one crashes.
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        pass
    finally:
        log.info("Shutting down...")
        feed.stop()
        orderbook.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()
        log.info("Shutdown complete.")


# --------------------------------------------------------------------------- #
#  Offline self-test (no network, no credentials)
# --------------------------------------------------------------------------- #
def self_test() -> int:
    """Exercise the AI + risk path on synthetic data; verifies wiring offline."""
    import math

    from src.ai.captain import Captain
    from src.ai.timesfm_predictor import create_predictor
    from src.utils.risk import RiskManager

    setup_logging("trading.log", "INFO")
    config = load_config()
    log.info("SELF-TEST: synthetic series -> baseline forecast -> captain -> sizing")

    # Synthetic BTC-like random walk (512 minutes).
    series = [60000.0]
    for i in range(511):
        series.append(series[-1] * (1 + 0.0002 * math.sin(i / 7) + 0.0005 * ((i % 5) - 2) / 2))

    predictor = create_predictor(config, force_baseline=True)
    fc = predictor.forecast(series)
    log.info("Forecast @8min: %s", fc.summary(8))
    p_above = fc.prob_above(8, series[-1])  # P(end above current)
    log.info("P(spot above %.0f in 8min) = %.3f", series[-1], p_above)

    captain = Captain(config)  # heuristic unless GEMINI_API_KEY set
    decision = asyncio.run(
        captain.review(
            {
                "forecast": fc.summary(8),
                "risk": {"win_rate": 0.52, "trades_recorded": 20},
            }
        )
    )
    log.info("Captain: %s", decision.summary())

    risk = RiskManager(10_000, config.fractional_kelly, config.daily_stop_loss_pct,
                       config.max_position_pct)

    # Flat-edge case (market priced at fair value) -> expect ~0 contracts.
    flat = risk.size_position(p_above, price_cents=int(p_above * 100),
                              kelly_multiplier=decision.kelly_multiplier)
    log.info("Sizing @fair(%dc): %d contracts ($%.2f)", int(p_above * 100),
             flat.contracts, flat.dollars)

    # Edge case: model 5c richer than the market -> expect a real position.
    edge_px = max(1, int(p_above * 100) - 5)
    sized = risk.size_position(p_above, price_cents=edge_px,
                               kelly_multiplier=decision.kelly_multiplier)
    log.info("Sizing @edge(%dc): %d contracts ($%.2f), kelly_raw=%.3f applied=%.3f capped=%s",
             edge_px, sized.contracts, sized.dollars, sized.kelly_fraction_raw,
             sized.kelly_fraction_applied, sized.capped_by_max_position)

    # Circuit-breaker check.
    allowed, why = risk.can_trade()
    log.info("Circuit breaker: can_trade=%s (%s)", allowed, why)
    log.info("SELF-TEST OK")
    return 0


# --------------------------------------------------------------------------- #
#  Windows-compatible entry
# --------------------------------------------------------------------------- #
def main() -> int:
    # Windows 11: the Proactor event loop is required for robust async I/O.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    if "--self-test" in sys.argv:
        return self_test()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Interrupted by user - exiting.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
