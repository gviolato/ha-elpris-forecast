"""Merge firm prices and the forecast into one hourly timeline with p10/p50/p90."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo

from .api import (
    FirmData,
    ForecastData,
    ForecastHour,
    place_shifted_band,
    place_shifted_price,
)
from .const import TIMELINE_HOURS
from .storage import ForecastRow
from .uncertainty import ErrorModel


def hour_floor(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def forecast_rows(firm: FirmData | None, forecast: ForecastData) -> list[ForecastRow]:
    """Forecast hours as database rows, shifted onto the configured place."""
    profile = firm.fixed_profile_ore if firm else {}
    return [
        ForecastRow(
            target=h.start,
            issued=h.issued_at,
            predicted_price=place_shifted_price(h, profile),
            run_id=h.run_id,
            model_id=h.model_id,
            kind=h.kind,
            source_all_in=_kr(h.all_in_ore),
            source_spot_ex_vat=_kr(h.spot_ex_vat_ore),
            source_p10=_kr(h.p10_ore),
            source_p90=_kr(h.p90_ore),
        )
        for h in forecast.hours
    ]


def _kr(ore: float | None) -> float | None:
    return None if ore is None else round(ore / 100, 4)


def build_timeline(
    now: datetime,
    firm: FirmData | None,
    forecast: ForecastData | None,
    model: ErrorModel | None,
    tz: tzinfo,
    hours: int = TIMELINE_HOURS,
) -> list[dict]:
    """Hourly timeline from the current hour: firm price where published, else the forecast.

    ``horizon_hours`` is hours ahead of the current hour. The band of a forecast hour is looked
    up by the forecast's own horizon (target - issue time), which is what the error model is
    indexed by.
    """
    start = hour_floor(now)
    firm_by_hour = {h.start: h for h in firm.hours} if firm else {}
    fc_by_hour: dict[datetime, ForecastHour] = {h.start: h for h in forecast.hours} if forecast else {}
    profile = firm.fixed_profile_ore if firm else {}
    out: list[dict] = []
    for i in range(hours):
        t = start + timedelta(hours=i)
        local = t.astimezone(tz).isoformat()
        if (fh := firm_by_hour.get(t)) is not None:
            out.append(
                {
                    "datetime": local,
                    "price": fh.price,
                    "is_forecast": False,
                    "horizon_hours": i,
                    "p10": fh.price,
                    "p50": fh.price,
                    "p90": fh.price,
                    "band_source": None,
                }
            )
        elif (fc := fc_by_hour.get(t)) is not None:
            price = place_shifted_price(fc, profile)
            issued_h = int((t - fc.issued_at).total_seconds() // 3600)
            p10, p50, p90 = model.quantiles(price, issued_h) if model else (None, price, None)
            source = "model" if p10 is not None else None
            if source is None and (band := place_shifted_band(fc, profile)) is not None:
                # Until our own error model has enough history for this horizon, fall back to
                # the band the publisher computes for its forecast.
                p10, p50, p90 = band[0], price, band[1]
                source = "publisher"
            out.append(
                {
                    "datetime": local,
                    "price": price,
                    "is_forecast": True,
                    "horizon_hours": i,
                    "p10": p10,
                    "p50": p50,
                    "p90": p90,
                    "band_source": source,
                }
            )
    return out
