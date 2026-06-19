"""
Forecasting engine.

Wraps Google's **TimesFM** time-series foundation model to produce, for the next
``horizon_len`` minutes, a point forecast plus 10th/90th percentile quantiles of
the underlying spot price. Those are translated into the probability that a
Kalshi market settles YES (price above / below / between strike(s)).

If ``timesfm`` (or its torch backend / checkpoint) is unavailable, a
:class:`BaselineForecaster` (geometric random walk with drift, calibrated from
recent realised volatility) provides the same interface so the engine stays
operable in paper mode. ``create_predictor`` picks the best available.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from ..utils.config import Config
from ..utils.logger import get_logger

log = get_logger("timesfm")

# 10th/90th percentile of a standard normal: z ~ +/-1.2816.
_Z90 = 1.2815515594


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function (no SciPy dependency)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# --------------------------------------------------------------------------- #
#  Forecast container + probability bridge
# --------------------------------------------------------------------------- #
@dataclass
class Forecast:
    """Per-horizon forecast (index 0 == 1 minute ahead)."""

    point: List[float]
    q10: List[float]
    q90: List[float]
    last_price: float
    source: str  # "timesfm" | "baseline"

    @property
    def horizon(self) -> int:
        return len(self.point)

    # ----------------------------------------------------- per-horizon access
    def _idx(self, minute: int) -> int:
        return max(0, min(self.horizon - 1, int(round(minute)) - 1))

    def mean_at(self, minute: int) -> float:
        return self.point[self._idx(minute)]

    def sigma_at(self, minute: int) -> float:
        """Implied stdev from the 10/90 quantile spread (normal approximation)."""
        i = self._idx(minute)
        sigma = (self.q90[i] - self.q10[i]) / (2.0 * _Z90)
        return max(sigma, 1e-9)

    # --------------------------------------------------- settlement probabilities
    def prob_above(self, minute: int, strike: float) -> float:
        mean, sigma = self.mean_at(minute), self.sigma_at(minute)
        return _clip_prob(_norm_cdf((mean - strike) / sigma))

    def prob_below(self, minute: int, strike: float) -> float:
        return _clip_prob(1.0 - self.prob_above(minute, strike))

    def prob_between(self, minute: int, low: float, high: float) -> float:
        mean, sigma = self.mean_at(minute), self.sigma_at(minute)
        p = _norm_cdf((high - mean) / sigma) - _norm_cdf((low - mean) / sigma)
        return _clip_prob(p)

    def summary(self, minute: int) -> dict:
        return {
            "minute": minute,
            "point": round(self.mean_at(minute), 2),
            "q10": round(self.q10[self._idx(minute)], 2),
            "q90": round(self.q90[self._idx(minute)], 2),
            "sigma": round(self.sigma_at(minute), 4),
            "last_price": round(self.last_price, 2),
            "source": self.source,
        }


def _clip_prob(p: float) -> float:
    return max(0.001, min(0.999, p))


# --------------------------------------------------------------------------- #
#  TimesFM predictor
# --------------------------------------------------------------------------- #
class TimesFMPredictor:
    """Lazy wrapper around the TimesFM foundation model."""

    source = "timesfm"

    def __init__(self, config: Config) -> None:
        self.config = config
        self._model = None  # loaded on first use

    def load(self) -> None:
        if self._model is not None:
            return
        import timesfm  # heavy import deferred until needed

        log.info(
            "Loading TimesFM (%s, backend=%s, context=%d, horizon=%d)...",
            self.config.timesfm_repo_id,
            self.config.timesfm_backend,
            self.config.context_len,
            self.config.horizon_len,
        )
        self._model = timesfm.TimesFm(
            hparams=timesfm.TimesFmHparams(
                backend=self.config.timesfm_backend,
                per_core_batch_size=32,
                horizon_len=self.config.horizon_len,
                context_len=self.config.context_len,
                num_layers=50,
                use_positional_embedding=False,
            ),
            checkpoint=timesfm.TimesFmCheckpoint(
                huggingface_repo_id=self.config.timesfm_repo_id
            ),
        )
        log.info("TimesFM loaded.")

    def forecast(self, series: Sequence[float]) -> Forecast:
        self.load()
        import numpy as np

        inputs = [list(map(float, series))]
        point_forecast, quantile_forecast = self._model.forecast(inputs, freq=[0])

        point = np.asarray(point_forecast)[0]
        quantiles = np.asarray(quantile_forecast)[0]  # shape [horizon, 10]
        # Layout: index 0 = mean, indices 1..9 = deciles 0.1..0.9.
        q10 = quantiles[:, 1]
        q90 = quantiles[:, 9]

        return Forecast(
            point=[float(x) for x in point],
            q10=[float(x) for x in q10],
            q90=[float(x) for x in q90],
            last_price=float(series[-1]),
            source=self.source,
        )


# --------------------------------------------------------------------------- #
#  Baseline fallback (geometric random walk with drift)
# --------------------------------------------------------------------------- #
class BaselineForecaster:
    """Calibrated GBM fallback so the engine runs without the heavy model."""

    source = "baseline"

    def __init__(self, config: Config) -> None:
        self.config = config

    def load(self) -> None:  # parity with TimesFMPredictor
        return

    def forecast(self, series: Sequence[float]) -> Forecast:
        s = [float(x) for x in series if x and x > 0]
        if not s:
            # No price at all yet - return a flat, wide forecast so callers never
            # crash. (In practice the feed always supplies the current price.)
            raise ValueError("no spot price available for forecast")
        last = s[-1]
        horizon = self.config.horizon_len
        default_vol = max(1e-6, float(getattr(self.config, "baseline_default_vol", 0.0012)))

        # Per-minute log-return drift (mu) and volatility (sigma). With little or
        # no history we fall back to a calibrated default vol so the bot can
        # forecast immediately - no price-history warm-up required.
        rets = [math.log(s[i] / s[i - 1]) for i in range(1, len(s))]
        if len(rets) >= 3:
            mu = sum(rets) / len(rets)
            var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
            sigma = max(math.sqrt(var), default_vol * 0.25)
        else:
            # 0-2 returns: no reliable estimate -> assume zero drift, default vol.
            mu, sigma = 0.0, default_vol

        point, q10, q90 = [], [], []
        log_last = math.log(last)
        for h in range(1, horizon + 1):
            mean_log = log_last + mu * h
            sd_log = sigma * math.sqrt(h)
            point.append(math.exp(mean_log))
            q10.append(math.exp(mean_log - _Z90 * sd_log))
            q90.append(math.exp(mean_log + _Z90 * sd_log))

        return Forecast(
            point=point, q10=q10, q90=q90, last_price=last, source=self.source
        )


# --------------------------------------------------------------------------- #
#  Factory
# --------------------------------------------------------------------------- #
# The exact API this wrapper is written against. Newer/older ``timesfm`` releases
# (e.g. the 2.5 line) rename these, which would otherwise blow up at runtime with
# "module 'timesfm' has no attribute 'TimesFm'". We probe up-front and fall back.
_REQUIRED_TIMESFM_ATTRS = ("TimesFm", "TimesFmHparams", "TimesFmCheckpoint")


def _timesfm_api_ok() -> tuple:
    """Return (importable, compatible, detail). Never raises."""
    try:
        import timesfm  # noqa: F401
    except Exception as exc:  # not installed / broken install
        return False, False, str(exc)
    missing = [a for a in _REQUIRED_TIMESFM_ATTRS if not hasattr(timesfm, a)]
    if missing:
        ver = getattr(timesfm, "__version__", "?")
        return True, False, f"installed v{ver} but missing {missing} (incompatible API)"
    return True, True, "compatible"


def create_predictor(config: Config, force_baseline: bool = False):
    """
    Return a forecaster exposing ``forecast(series) -> Forecast``.

    The built-in :class:`BaselineForecaster` is always a safe choice. TimesFM is
    used only when the installed package exposes the exact API this wrapper
    targets, so an incompatible version can never crash the forecast loop.

    Controlled by ``config.forecaster``: ``auto`` (default), ``baseline`` or
    ``timesfm``.
    """
    mode = (getattr(config, "forecaster", "auto") or "auto").lower()

    if force_baseline or mode == "baseline":
        log.info("Forecaster: BaselineForecaster (statistical GBM)%s",
                 " [forced]" if force_baseline else " [FORECASTER=baseline]")
        return BaselineForecaster(config)

    importable, compatible, detail = _timesfm_api_ok()
    if compatible:
        log.info("Forecaster: TimesFM (%s)", detail)
        return TimesFMPredictor(config)

    # Incompatible or unavailable -> baseline. Be loud once so it is obvious in
    # the log why we are not on TimesFM (no per-minute error spam).
    if mode == "timesfm":
        log.error(
            "FORECASTER=timesfm but TimesFM is unusable (%s). Falling back to "
            "BaselineForecaster. Install a compatible build with "
            "'pip install -r requirements-optional.txt' or set FORECASTER=baseline.",
            detail,
        )
    elif importable:
        log.warning(
            "TimesFM %s - using BaselineForecaster instead. "
            "(Set FORECASTER=baseline in .env to silence this.)", detail,
        )
    else:
        log.info("TimesFM not installed - using BaselineForecaster (%s).", detail)
    return BaselineForecaster(config)
