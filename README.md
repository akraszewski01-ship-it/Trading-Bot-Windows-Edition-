# Trading-Bot — Windows Edition

A production-grade, **Windows 11-compatible** automated trading system for
**Kalshi's 15-minute crypto markets** (`KXBTC15M`, `KXETH15M`).

It pairs a quantitative execution engine with two AI layers:

* a **forecasting engine** built on Google's **TimesFM** time-series foundation
  model, and
* a **"Captain" oversight layer** powered by Google's **Gemini**, which sets the
  risk guardrails the engine must obey.

> ⚠️ **Trading involves substantial risk of loss.** This software is provided for
> research and educational purposes. It ships in **paper mode by default** and
> will not place live orders until you explicitly set `TRADING_MODE=live` with
> valid credentials. Validate extensively on paper first. Not financial advice.

---

## Architecture

```
main.py                      Async orchestrator (Windows ProactorEventLoop)
└── src/
    ├── engine/
    │   ├── kalshi_client.py  RSA-PSS signed Kalshi v2 REST + WS auth
    │   ├── orderbook.py      Real-time orderbook over WebSocket (snapshot+delta)
    │   ├── market_data.py    Coinbase/Binance 512-min rolling spot window
    │   └── execution.py      Timing gate + spread-crossing executor + settlement
    ├── ai/
    │   ├── timesfm_predictor.py  TimesFM forecast → settlement probability
    │   └── captain.py            Gemini oversight with enforced JSON schema
    └── utils/
        ├── config.py        Immutable env-driven configuration
        ├── logger.py        Rotating-file + console audit logging (trading.log)
        └── risk.py          Kelly sizing + hard circuit breakers
```

### Data & control flow

```
 Coinbase/Binance WS ─► MarketDataFeed ─►(512 min)─► TimesFM ─► Forecast (P, q10, q90)
                                                                     │
 Kalshi WS ─► OrderBookManager ─► live spread/mid ──────────┐        │
                                                            ▼        ▼
                                              ExecutionEngine.evaluate()
                                                  │  timing gate (6–9 min)
                                                  │  edge > spread + dynamic buffer
                                                  ▼
                              RiskManager (Kelly × captain_mult, circuit breakers)
                                                  ▼
                                       Kalshi order (paper | live)

 every 5 min:  context (forecast + skew + win/loss) ─► Captain (Gemini) ─► guardrails
```

---

## Key design decisions

| Concern | Implementation |
|---|---|
| **Auth** | Kalshi v2 **RSA-PSS** (SHA-256, digest salt length) signing of `timestamp+METHOD+path`; same scheme authenticates the WebSocket handshake. |
| **Timing gate** | Markets are evaluated **only when 6–9 minutes from expiry** (configurable). |
| **Spread crossing** | A trade fires only when `|edge| > spread + dynamic_buffer`, where `dynamic_buffer = base_buffer + captain.min_edge_threshold + volatility_term`. Orders are sent as **marketable limits** at the crossing price to bound slippage. |
| **Forecast → probability** | TimesFM point + 10th/90th quantiles imply a normal at the expiry horizon; `P(settle YES)` follows from the strike(s) (`above` / `below` / `between`). |
| **Position sizing** | **Kelly criterion** for binary contracts, scaled by a static fractional-Kelly safety factor **and** the Captain's `kelly_multiplier`, capped by liquidity. |
| **Circuit breakers** | Hard, AI-non-overridable: **daily stop-loss 5%**, **max position 10%** of portfolio. |
| **Captain schema** | Gemini configured with `response_mime_type=application/json` + `response_schema`; output additionally parsed, validated and clamped here. |
| **Resilience** | WS auto-reconnect with backoff, orderbook **seq-gap detection** → resubscribe, conservative fallbacks if Gemini/TimesFM are unavailable. |
| **Windows** | `WindowsProactorEventLoopPolicy` set explicitly; all I/O is asyncio-based. |

---

## Setup (Windows 11)

```powershell
# 1. Python 3.11 recommended
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install deps  (PyTorch may install faster from pytorch.org first)
pip install -r requirements.txt

# 3. Configure
copy .env.example .env
notepad .env          # fill in keys; keep TRADING_MODE=paper to start
```

### Credentials

* **Kalshi**: create an API key (Account → API Keys). Save the downloaded
  private key as `secrets/kalshi_private_key.pem` and set `KALSHI_API_KEY_ID`.
* **Gemini**: get a key from Google AI Studio → `GEMINI_API_KEY`.

Both AI layers degrade gracefully: without `GEMINI_API_KEY` the Captain runs a
conservative heuristic; without `timesfm` installed the engine uses a calibrated
statistical fallback forecaster.

---

## Running

```powershell
python main.py --self-test   # offline smoke test (no network/keys needed)
python main.py               # run the system (paper or live per .env)
```

Everything is logged to **`trading.log`** (rotating) and the console: TimesFM
forecasts, Captain reasoning/decisions, trade decisions and settlements.

### Going live

1. Run for an extended period in `paper` mode and review `trading.log`.
2. Confirm the circuit breakers and sizing behave as expected.
3. Set `TRADING_MODE=live` and start with a **small** `PORTFOLIO_VALUE_USD` /
   tight `MAX_POSITION_PCT`. The engine refuses to start live with any config
   validation errors and refuses to send orders if it cannot authenticate.

---

## Configuration reference

All settings come from environment variables (see `.env.example`). Highlights:

| Var | Default | Meaning |
|---|---|---|
| `TRADING_MODE` | `paper` | `paper` simulates; `live` sends real orders |
| `SPOT_PROVIDER` | `coinbase` | `coinbase` (US-friendly) or `binance` |
| `MARKET_SERIES` | `KXBTC15M,KXETH15M` | Kalshi series to trade |
| `FRACTIONAL_KELLY` | `0.25` | base Kelly safety scalar |
| `DAILY_STOP_LOSS_PCT` | `0.05` | daily realised-loss halt |
| `MAX_POSITION_PCT` | `0.10` | per-position notional cap |
| `BASE_EDGE_BUFFER` | `0.02` | min edge added on top of spread |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Captain model |
| `TIMESFM_BACKEND` | `cpu` | `cpu` or `gpu` |

---

## Settlement-source note

Kalshi crypto markets settle on a specific reference index. The spot feed
(Coinbase/Binance) is used here as a **proxy** for forecasting and paper
settlement. For live trading, align the feed with Kalshi's stated settlement
source for best calibration.
