"""Forecast-error model: exponentially forgotten (EWMA) bias and variance per horizon.

For every scored forecast the residual is ``e = predicted - actual`` (kr/kWh). Residuals are
grouped by horizon ``h = floor((target - issued) / 1h)``, clamped to ``1..168``, and consumed in
target-time order with a forgetting factor ``lam`` (weight of an observation decays by ``lam``
for every newer observation in the same horizon)::

    W  <- lam * W + 1                   # sum of weights
    Q  <- lam^2 * Q + 1                 # sum of squared weights  -> effective n = W^2 / Q
    d   = e - mu
    mu <- mu + d / W                    # EWMA bias
    S  <- lam * S + d * (e - mu)        # weighted sum of squares (West's recurrence)
    var = S / (W - Q / W)               # unbiased exponentially weighted variance

With roughly two forecast runs per day each horizon gets ~2 residuals a day, so ``lam = 0.95``
is a half-life of ~13.5 residuals, i.e. about one week.

Because the runs are issued at fixed times of day, a single horizon bucket also corresponds to a
fixed hour of day. To avoid mistaking that diurnal pattern for horizon dependence (and to get
more data per estimate), the published statistics pool the buckets within ``+-smoothing_hours``
with a triangular kernel. The pooled sigma is then made non-decreasing in ``h`` (uncertainty
cannot shrink further ahead).

A horizon is *ready* when its pooling window contains residuals from at least
``min_scored_days`` distinct target days and an effective sample size of ``min_effective_n``.
The 24 residuals of one day are strongly correlated, so counting days is the honest gate.

Quantiles assume a Gaussian error: ``p50 = price - bias``, ``p10/p90 = p50 -+ 1.2816 sigma``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
import math

from .const import MAX_HORIZON_HOURS
from .storage import Residual

Z90 = 1.2815515655446004  # standard normal 90th percentile


@dataclass
class HorizonStats:
    """Running EWMA state of one horizon bucket."""

    n: int = 0
    weight: float = 0.0
    weight_sq: float = 0.0
    mean: float = 0.0
    sum_sq: float = 0.0
    days: set[date] = field(default_factory=set)

    def update(self, residual: float, lam: float) -> None:
        self.n += 1
        self.weight = lam * self.weight + 1.0
        self.weight_sq = lam * lam * self.weight_sq + 1.0
        delta = residual - self.mean
        self.mean += delta / self.weight
        self.sum_sq = lam * self.sum_sq + delta * (residual - self.mean)

    @property
    def effective_n(self) -> float:
        return self.weight**2 / self.weight_sq if self.weight_sq else 0.0

    @property
    def variance(self) -> float:
        denom = self.weight - self.weight_sq / self.weight if self.weight else 0.0
        return max(self.sum_sq / denom, 0.0) if denom > 0 else 0.0


@dataclass(frozen=True)
class Band:
    """Published uncertainty for one horizon."""

    bias: float
    sigma: float
    effective_n: float
    days: int
    ready: bool


def clamp_horizon(hours: int) -> int:
    return min(max(int(hours), 1), MAX_HORIZON_HOURS)


class ErrorModel:
    """EWMA error statistics for horizons 1..168, rebuilt from the stored residual history."""

    def __init__(
        self,
        forgetting_factor: float,
        min_scored_days: int,
        min_effective_n: float,
        smoothing_hours: int,
    ) -> None:
        if not 0 < forgetting_factor <= 1:
            raise ValueError("forgetting_factor must be in (0, 1]")
        self.lam = forgetting_factor
        self.min_scored_days = min_scored_days
        self.min_effective_n = min_effective_n
        self.smoothing_hours = max(int(smoothing_hours), 0)
        self.buckets: dict[int, HorizonStats] = {}
        self.bands: dict[int, Band] = {}
        self.residual_count = 0
        self.scored_days: set[date] = set()

    def fit(self, residuals: Iterable[Residual]) -> ErrorModel:
        """Consume residuals (oldest target first) and compute the published bands."""
        for res in residuals:
            h = clamp_horizon(res.horizon_hours)
            stats = self.buckets.setdefault(h, HorizonStats())
            stats.update(res.residual, self.lam)
            day = res.target.date()
            stats.days.add(day)
            self.scored_days.add(day)
            self.residual_count += 1
        self._pool()
        return self

    def _pool(self) -> None:
        k = self.smoothing_hours
        running_sigma = 0.0
        self.bands = {}
        for h in range(1, MAX_HORIZON_HOURS + 1):
            w_tot = mean_acc = var_acc = 0.0
            eff_acc = 0.0
            days: set[date] = set()
            for j in range(max(1, h - k), min(MAX_HORIZON_HOURS, h + k) + 1):
                stats = self.buckets.get(j)
                if stats is None or stats.weight == 0:
                    continue
                kernel = 1.0 - abs(j - h) / (k + 1)
                w = kernel * stats.weight
                w_tot += w
                mean_acc += w * stats.mean
                # total variance of the pooled mixture: within + between-bucket spread
                var_acc += w * (stats.variance + stats.mean**2)
                eff_acc += kernel * stats.effective_n
                days |= stats.days
            if w_tot == 0:
                continue
            mean = mean_acc / w_tot
            sigma = math.sqrt(max(var_acc / w_tot - mean**2, 0.0))
            ready = len(days) >= self.min_scored_days and eff_acc >= self.min_effective_n
            if ready:
                running_sigma = max(running_sigma, sigma)
                sigma = running_sigma
            self.bands[h] = Band(mean, sigma, eff_acc, len(days), ready)

    def band(self, horizon_hours: int) -> Band | None:
        """The pooled band for a horizon, or ``None`` if it is not ready yet."""
        band = self.bands.get(clamp_horizon(horizon_hours))
        return band if band is not None and band.ready else None

    def quantiles(self, price: float, horizon_hours: int) -> tuple[float | None, float, float | None]:
        """(p10, p50, p90) for a forecast price; p10/p90 are ``None`` until the horizon is ready."""
        band = self.band(horizon_hours)
        if band is None:
            return None, price, None
        p50 = price - band.bias
        return (
            round(p50 - Z90 * band.sigma, 4),
            round(p50, 4),
            round(p50 + Z90 * band.sigma, 4),
        )

    def snapshot_rows(self) -> list[tuple]:
        """Raw per-bucket state for the ``error_model`` table."""
        return [
            (h, s.n, s.weight, s.effective_n, s.mean, s.variance, self.lam)
            for h, s in sorted(self.buckets.items())
        ]

    def summary(self) -> dict:
        """Compact diagnostics: per forecast day (24 h blocks of horizon)."""
        ready = [h for h, b in self.bands.items() if b.ready]
        by_day = []
        for d in range(7):
            hs = [self.bands[h] for h in range(d * 24 + 1, d * 24 + 25) if h in self.bands]
            ok = [b for b in hs if b.ready]
            by_day.append(
                {
                    "horizon": f"{d * 24 + 1}-{d * 24 + 24}h",
                    "ready_hours": len(ok),
                    "bias": round(sum(b.bias for b in ok) / len(ok), 4) if ok else None,
                    "sigma": round(sum(b.sigma for b in ok) / len(ok), 4) if ok else None,
                    "max_scored_days": max((b.days for b in hs), default=0),
                }
            )
        return {
            "residuals": self.residual_count,
            "scored_days": len(self.scored_days),
            "ready_horizons": len(ready),
            "min_ready_horizon": min(ready) if ready else None,
            "max_ready_horizon": max(ready) if ready else None,
            "by_forecast_day": by_day,
        }
