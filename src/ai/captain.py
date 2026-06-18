"""
The "Captain" - an oversight layer powered by Google's Gemini.

Every few minutes the trading loop hands the Captain a context payload (TimesFM
forecast summary, Kalshi orderbook skew, recent win/loss and PnL). The Captain
returns a strictly-schema'd directive:

    {
      "market_regime":      "trend" | "range" | "choppy",
      "kelly_multiplier":   0.0 .. 1.0,
      "min_edge_threshold": float (probability units),
      "halt_trading":       bool,
      "reasoning":          str        # for the audit log
    }

The schema is enforced two ways: Gemini is configured with a ``response_schema``
+ ``application/json`` MIME type, *and* the returned payload is parsed,
validated and clamped here. If Gemini is unavailable or errors, a conservative
heuristic keeps the system safe and operational.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..utils.config import Config
from ..utils.logger import get_logger

log = get_logger("captain")

_REGIMES = ("trend", "range", "choppy")
_MAX_EDGE_THRESHOLD = 0.50  # 50 cents - sanity clamp

_SYSTEM_PROMPT = """\
You are "The Captain", the risk-oversight authority for an automated trading
system operating on Kalshi 15-minute crypto markets (KXBTC15M, KXETH15M).

You do NOT place trades. You set the guardrails the execution engine must obey
for the next few minutes, based on the context you are given:

- A TimesFM price forecast (point + 10th/90th percentile) for the next 15 min.
- Kalshi orderbook skew (market-implied probabilities, bid/ask imbalance).
- Recent realised win-rate and intraday PnL.

Your job is to judge the *market regime* and decide how aggressive the engine
may be. Be conservative: capital preservation outranks profit.

Output ONLY JSON matching the provided schema. Field guidance:
- market_regime: "trend" (directional, momentum reliable),
  "range" (mean-reverting, bounded), or "choppy" (noisy/unreliable).
- kelly_multiplier: 0.0 stand down .. 1.0 full configured Kelly. Lower it when
  the regime is choppy, volatility is spiking, or recent win-rate is poor.
- min_edge_threshold: minimum statistical edge (in probability units, e.g. 0.05
  = 5 cents) required before crossing the spread. Raise it when uncertain.
- halt_trading: true to stop ALL new entries (e.g. extreme/abnormal conditions).
Always include a brief reasoning string.
"""


@dataclass
class CaptainDecision:
    """A validated Captain directive."""

    market_regime: str
    kelly_multiplier: float
    min_edge_threshold: float
    halt_trading: bool
    reasoning: str = ""
    source: str = "gemini"
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def conservative_default(cls, reasoning: str = "default guardrails") -> "CaptainDecision":
        return cls(
            market_regime="choppy",
            kelly_multiplier=0.25,
            min_edge_threshold=0.05,
            halt_trading=False,
            reasoning=reasoning,
            source="default",
        )

    def summary(self) -> str:
        return (
            f"regime={self.market_regime} kelly_mult={self.kelly_multiplier:.2f} "
            f"min_edge={self.min_edge_threshold:.3f} halt={self.halt_trading} "
            f"[{self.source}] :: {self.reasoning}"
        )


class Captain:
    """Gemini-backed oversight with schema enforcement and safe fallbacks."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.enabled = bool(config.gemini_api_key)
        self.last_decision = CaptainDecision.conservative_default()
        self._model = None

        if self.enabled:
            try:
                self._init_model()
                log.info("Captain online (Gemini model=%s)", config.gemini_model)
            except Exception as exc:
                log.error("Captain init failed (%s) - heuristic fallback", exc)
                self.enabled = False
        else:
            log.warning("GEMINI_API_KEY not set - Captain runs in heuristic mode")

    # --------------------------------------------------------------- model init
    def _init_model(self) -> None:
        import google.generativeai as genai

        genai.configure(api_key=self.config.gemini_api_key)
        self._model = genai.GenerativeModel(
            self.config.gemini_model,
            system_instruction=_SYSTEM_PROMPT,
            generation_config=self._build_generation_config(genai),
        )

    @staticmethod
    def _build_generation_config(genai) -> Any:
        """JSON-mode config with a response_schema when the SDK supports it."""
        base = {"response_mime_type": "application/json", "temperature": 0.2}
        try:
            protos = genai.protos
            schema = protos.Schema(
                type=protos.Type.OBJECT,
                properties={
                    "market_regime": protos.Schema(
                        type=protos.Type.STRING, enum=list(_REGIMES)
                    ),
                    "kelly_multiplier": protos.Schema(type=protos.Type.NUMBER),
                    "min_edge_threshold": protos.Schema(type=protos.Type.NUMBER),
                    "halt_trading": protos.Schema(type=protos.Type.BOOLEAN),
                    "reasoning": protos.Schema(type=protos.Type.STRING),
                },
                required=[
                    "market_regime",
                    "kelly_multiplier",
                    "min_edge_threshold",
                    "halt_trading",
                ],
            )
            return genai.GenerationConfig(response_schema=schema, **base)
        except Exception as exc:  # pragma: no cover - SDK version differences
            log.warning("Captain: response_schema unsupported (%s); JSON-mode only", exc)
            return genai.GenerationConfig(**base)

    # ------------------------------------------------------------------- review
    async def review(self, context: Dict[str, Any]) -> CaptainDecision:
        """Run a Captain review; never raises (always returns a usable decision)."""
        if not self.enabled or self._model is None:
            decision = self._heuristic(context)
            self.last_decision = decision
            log.info("Captain (heuristic) %s", decision.summary())
            return decision

        prompt = (
            "Context payload (JSON):\n"
            + json.dumps(context, indent=2, default=str)
            + "\n\nReturn ONLY the JSON directive."
        )
        try:
            resp = await asyncio.to_thread(self._model.generate_content, prompt)
            data = json.loads(resp.text)
            decision = self._parse(data)
            self.last_decision = decision
            log.info("Captain %s", decision.summary())
            return decision
        except Exception as exc:
            log.error("Captain review failed (%s) - reusing last decision", exc)
            return self.last_decision

    # -------------------------------------------------------------- validation
    def _parse(self, data: Dict[str, Any]) -> CaptainDecision:
        regime = str(data.get("market_regime", "choppy")).lower()
        if regime not in _REGIMES:
            regime = "choppy"
        return CaptainDecision(
            market_regime=regime,
            kelly_multiplier=_clamp(data.get("kelly_multiplier", 0.25), 0.0, 1.0),
            min_edge_threshold=_clamp(
                data.get("min_edge_threshold", 0.05), 0.0, _MAX_EDGE_THRESHOLD
            ),
            halt_trading=bool(data.get("halt_trading", False)),
            reasoning=str(data.get("reasoning", ""))[:1000],
            source="gemini",
        )

    # --------------------------------------------------------------- heuristic
    def _heuristic(self, context: Dict[str, Any]) -> CaptainDecision:
        """Rule-based stand-in when Gemini is unavailable."""
        win_rate = float(context.get("risk", {}).get("win_rate", 0.0) or 0.0)
        trades = int(context.get("risk", {}).get("trades_recorded", 0) or 0)

        # Use forecast dispersion as a crude regime/vol proxy.
        fc = context.get("forecast", {}) or {}
        rel_vol = 0.0
        try:
            rel_vol = float(fc.get("sigma", 0.0)) / max(float(fc.get("last_price", 1.0)), 1.0)
        except (TypeError, ValueError):
            rel_vol = 0.0

        if rel_vol > 0.004:
            regime, kelly_mult, min_edge = "choppy", 0.2, 0.06
        elif rel_vol < 0.0015:
            regime, kelly_mult, min_edge = "range", 0.4, 0.04
        else:
            regime, kelly_mult, min_edge = "trend", 0.35, 0.05

        # Temper aggression if recent performance is weak (with enough samples).
        if trades >= 10 and win_rate < 0.45:
            kelly_mult *= 0.5
            min_edge += 0.02

        return CaptainDecision(
            market_regime=regime,
            kelly_multiplier=round(kelly_mult, 3),
            min_edge_threshold=round(min_edge, 3),
            halt_trading=False,
            reasoning=f"heuristic: rel_vol={rel_vol:.4f}, win_rate={win_rate:.2f}, n={trades}",
            source="heuristic",
        )


def _clamp(value: Any, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return lo
