"""
Entry point for the Kalshi 15-minute crypto trading system (Windows edition).

Wires the spot feed, Kalshi orderbook WebSocket, TimesFM forecaster and the
Gemini "Captain" into the execution engine.  The dashboard runs as a
persistent asyncio task that survives engine stop/restart so the Start button
in the browser can relaunch the trading engine without exiting the process.

Run:
    python main.py              # paper or live per .env (TRADING_MODE)
    python main.py --self-test  # offline smoke test (no network/credentials)
"""

from __future__ import annotations

import asyncio
import sys

from src.ai import Captain, create_predictor
from src.utils import RiskManager, get_logger, load_config, setup_logging

if True:
    from typing import TYPE_CHECKING
    if TYPE_CHECKING:
        from src.engine import ExecutionEngine, MarketDataFeed

log = get_logger("main")


# --------------------------------------------------------------------------- #
#  Cooperative loops
# --------------------------------------------------------------------------- #
async def market_refresh_loop(engine: ExecutionEngine, interval: int = 30) -> None:
    while True:
        try:
            await engine.refresh_markets()
        except Exception as exc:
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
    # Publish the Captain wiring status immediately so the dashboard can show
    # "Gemini live" vs "SDK missing" vs "no key" before the first review runs.
    BotState.update({"captain": {
        "regime": "-", "kelly_mult": 0.0, "min_edge": 0.0, "halt": False,
        "reasoning": "Captain initialising...",
        "source": "gemini" if captain.enabled else "heuristic",
        "status_detail": captain.status_detail,
    }})
    # Fast real connectivity check so the dashboard reflects the TRUE Gemini
    # status (e.g. invalid key) within seconds, not an optimistic "live".
    if captain.enabled:
        gemini_ok = await captain.probe()
        BotState.update({"captain": {
            "regime": "-", "kelly_mult": 0.0, "min_edge": 0.0, "halt": False,
            "reasoning": "Gemini connected." if gemini_ok
                         else "Gemini unreachable - running on the safety heuristic.",
            "source": "gemini" if gemini_ok else "heuristic",
            "status_detail": captain.status_detail,
        }})
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
                    "status_detail": captain.status_detail,
                    "ts": decision.ts.isoformat(),
                }
            })
        except Exception as exc:
            log.error("captain_loop: %s", exc)
        await asyncio.sleep(config.captain_interval_sec)


async def stop_watcher(interval: int = 1) -> None:
    """Return as soon as BotState carries stop_requested=True."""
    from src.utils.bot_state import BotState
    while True:
        if BotState.get().get("stop_requested"):
            log.info("Stop requested from dashboard.")
            BotState.update({"stop_requested": False})
            return
        await asyncio.sleep(interval)


async def dashboard_loop(port: int = 8080) -> None:
    """Run the dashboard IN-PROCESS sharing BotState. Runs for the lifetime of
    the process so the Start button works even when the engine is stopped."""
    try:
        import uvicorn
        from dashboard.server import app
    except Exception as exc:
        log.warning("Dashboard not started (%s). Install fastapi+uvicorn.", exc)
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
                "diagnostics": engine.diagnostics(),
                "circuit_breaker": {"halted": snap.get("halted", False), "reason": ""},
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
#  Engine lifecycle (can be started / stopped multiple times per process)
# --------------------------------------------------------------------------- #
async def run_engine() -> None:
    """Bootstrap and run all trading tasks until stop is requested or a task
    raises. Returns when the engine has stopped cleanly."""
    from src.engine import ExecutionEngine, KalshiClient, MarketDataFeed, OrderBookManager
    from src.utils.bot_state import BotState

    config = load_config()
    BotState.update({"status": "starting", "mode": config.trading_mode})

    log.info("=" * 70)
    log.info("Engine starting | mode=%s provider=%s series=%s",
             config.trading_mode.upper(), config.spot_provider, config.market_series)
    log.info("=" * 70)

    problems = config.validate()
    for p in problems:
        log.warning("CONFIG: %s", p)
    if config.is_live and problems:
        log.critical("Refusing to start LIVE with configuration problems.")
        BotState.update({"status": "stopped"})
        return

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

    await feed.seed()
    await engine.refresh_markets()

    tasks = [
        asyncio.create_task(feed.run(), name="feed"),
        asyncio.create_task(orderbook.run(), name="orderbook"),
        asyncio.create_task(market_refresh_loop(engine), name="market_refresh"),
        asyncio.create_task(forecast_loop(config, feed, predictor, engine), name="forecast"),
        asyncio.create_task(captain_loop(config, captain, engine), name="captain"),
        asyncio.create_task(trading_loop(engine), name="trading"),
        asyncio.create_task(stop_watcher(), name="stop_watcher"),
    ]
    log.info("All systems live. %d loops running.", len(tasks))
    BotState.update({"status": "running"})

    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        pass
    finally:
        log.info("Engine shutting down...")
        feed.stop()
        orderbook.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()
        BotState.update({"status": "stopped"})
        log.info("Engine stopped. Waiting for Start signal from dashboard.")


# --------------------------------------------------------------------------- #
#  Top-level run loop — dashboard survives engine stop/restart
# --------------------------------------------------------------------------- #
async def run() -> None:
    from src.utils.bot_state import BotState

    config = load_config()
    setup_logging(config.log_file, config.log_level)
    BotState.update({"status": "starting", "mode": config.trading_mode})

    # Dashboard runs for the full process lifetime so the browser Start button
    # can signal a restart even when the trading engine is stopped.
    dashboard_task = asyncio.create_task(dashboard_loop(), name="dashboard")

    try:
        while True:
            await run_engine()

            # Wait for a start_requested signal from the dashboard, or for the
            # dashboard itself to exit (user closed the window / Ctrl+C).
            log.info("Idle. Press Start in the dashboard to restart the engine.")
            while True:
                if dashboard_task.done():
                    return  # dashboard died — exit the process
                state = BotState.get()
                if state.get("start_requested"):
                    BotState.update({"start_requested": False})
                    log.info("Start signal received — relaunching engine.")
                    break
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        dashboard_task.cancel()
        await asyncio.gather(dashboard_task, return_exceptions=True)
        log.info("Process exiting.")


# --------------------------------------------------------------------------- #
#  Offline self-test
# --------------------------------------------------------------------------- #
def self_test() -> int:
    import math
    from src.ai.captain import Captain
    from src.ai.timesfm_predictor import create_predictor
    from src.utils.risk import RiskManager

    setup_logging("trading.log", "INFO")
    config = load_config()
    log.info("SELF-TEST: synthetic series -> baseline forecast -> captain -> sizing")

    series = [60000.0]
    for i in range(511):
        series.append(series[-1] * (1 + 0.0002 * math.sin(i / 7) + 0.0005 * ((i % 5) - 2) / 2))

    predictor = create_predictor(config, force_baseline=True)
    fc = predictor.forecast(series)
    log.info("Forecast @8min: %s", fc.summary(8))
    p_above = fc.prob_above(8, series[-1])
    log.info("P(spot above %.0f in 8min) = %.3f", series[-1], p_above)

    captain = Captain(config)
    decision = asyncio.run(captain.review({"forecast": fc.summary(8), "risk": {"win_rate": 0.52, "trades_recorded": 20}}))
    log.info("Captain: %s", decision.summary())

    risk = RiskManager(10_000, config.fractional_kelly, config.daily_stop_loss_pct, config.max_position_pct)
    flat = risk.size_position(p_above, price_cents=int(p_above * 100), kelly_multiplier=decision.kelly_multiplier)
    log.info("Sizing @fair(%dc): %d contracts ($%.2f)", int(p_above * 100), flat.contracts, flat.dollars)
    edge_px = max(1, int(p_above * 100) - 5)
    sized = risk.size_position(p_above, price_cents=edge_px, kelly_multiplier=decision.kelly_multiplier)
    log.info("Sizing @edge(%dc): %d contracts ($%.2f)", edge_px, sized.contracts, sized.dollars)
    allowed, why = risk.can_trade()
    log.info("Circuit breaker: can_trade=%s (%s)", allowed, why)
    log.info("SELF-TEST OK")
    return 0


# --------------------------------------------------------------------------- #
#  Entry
# --------------------------------------------------------------------------- #
def main() -> int:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    if "--self-test" in sys.argv:
        return self_test()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Interrupted by user.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
