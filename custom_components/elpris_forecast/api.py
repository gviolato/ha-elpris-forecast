"""Parsing of the elpriser.dk feeds into plain data objects.

Pure functions only (no Home Assistant imports) so they can be unit tested and audited.

* ``timepris.json?sted=<place>`` - published (firm) hourly prices for today and tomorrow,
  all-in for the requested place: spot + grid tariff + Energinet tariffs + electricity tax, incl. VAT.
* ``prognose.json`` - 7-day hourly forecast of the same all-in price, but computed for each price
  region's *representative* grid area. :func:`place_shifted_price` moves it onto the requested place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .const import DANISH_VAT


def parse_ts(value: str) -> datetime:
    """Parse an ISO timestamp (``Z`` or offset) into an aware UTC datetime."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


@dataclass(frozen=True)
class FirmHour:
    """A published hourly price at the configured place (kr/kWh incl. VAT)."""

    start: datetime  # UTC
    local_hour: int  # hour of day in Danish time (tariff profiles are local-time based)
    price: float
    spot_ore: float | None
    grid_ore: float | None
    charges_ore: float | None

    @property
    def fixed_ore(self) -> float | None:
        """Non-spot part of the price (grid tariff + charges), øre/kWh incl. VAT."""
        if self.grid_ore is None or self.charges_ore is None:
            return None
        return self.grid_ore + self.charges_ore


@dataclass
class FirmData:
    """Parsed ``timepris.json``."""

    place: dict
    region: str
    hours: list[FirmHour]
    generated_at: datetime | None
    fixed_profile_ore: dict[int, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ForecastHour:
    """One forecast hour, as published for the region's representative grid area."""

    start: datetime  # UTC
    local_hour: int
    all_in_ore: float  # representative area, øre/kWh incl. VAT
    spot_ex_vat_ore: float | None
    p10_ore: float | None
    p90_ore: float | None
    run_id: int | None
    model_id: str | None
    issued_at: datetime
    kind: str


@dataclass
class ForecastData:
    """Parsed ``prognose.json`` for one region."""

    region: str
    hours: list[ForecastHour]
    generated_at: datetime | None


def _hour_of(start: str) -> int:
    return datetime.fromisoformat(start).hour


def parse_timepris(payload: dict) -> FirmData:
    """Parse the firm hourly prices for today and tomorrow."""
    hours: list[FirmHour] = []
    profile: dict[int, float] = {}
    for key in ("today", "tomorrow"):
        day = payload.get(key)
        if not day:
            continue
        for item in day.get("hours") or []:
            price = item.get("krPerKwhInclVat")
            if price is None:
                continue
            hour = FirmHour(
                start=parse_ts(item["start"]),
                local_hour=_hour_of(item["start"]),
                price=float(price),
                spot_ore=item.get("spotOreInclVat"),
                grid_ore=item.get("gridOreInclVat"),
                charges_ore=item.get("chargesOreInclVat"),
            )
            hours.append(hour)
            # Later days overwrite earlier ones: the newest published tariffs win.
            if hour.fixed_ore is not None:
                profile[hour.local_hour] = hour.fixed_ore
    hours.sort(key=lambda h: h.start)
    place = payload.get("place") or {}
    generated = payload.get("generatedAt")
    return FirmData(
        place=place,
        region=str(place.get("region") or "DK1").upper(),
        hours=hours,
        generated_at=parse_ts(generated) if generated else None,
        fixed_profile_ore=profile,
    )


def parse_prognose(payload: dict, region: str) -> ForecastData:
    """Parse the forecast days (everything not yet published as firm) for ``region``."""
    region = region.upper()
    block = next(
        (r for r in payload.get("regions") or [] if str(r.get("region")).upper() == region),
        None,
    )
    generated = payload.get("generatedAt")
    result = ForecastData(
        region=region, hours=[], generated_at=parse_ts(generated) if generated else None
    )
    if block is None:
        return result
    runs = {r.get("runId"): r for r in block.get("runs") or []}
    for day in block.get("days") or []:
        kind = day.get("kind") or "forecast"
        if kind == "known":
            continue  # published prices come from timepris for the configured place
        issued = day.get("issuedAt") or runs.get(day.get("runId"), {}).get("issuedAt")
        if not issued:
            continue
        issued_at = parse_ts(issued)
        model_id = day.get("modelId") or runs.get(day.get("runId"), {}).get("modelId")
        for item in day.get("hours") or []:
            all_in = item.get("allInOreInclVat")
            if all_in is None:
                continue
            result.hours.append(
                ForecastHour(
                    start=parse_ts(item["start"]),
                    local_hour=_hour_of(item["start"]),
                    all_in_ore=float(all_in),
                    spot_ex_vat_ore=item.get("spotOreExVat"),
                    p10_ore=item.get("p10OreInclVat"),
                    p90_ore=item.get("p90OreInclVat"),
                    run_id=day.get("runId"),
                    model_id=model_id,
                    issued_at=issued_at,
                    kind=kind,
                )
            )
    result.hours.sort(key=lambda h: h.start)
    return result


def place_shifted_price(hour: ForecastHour, fixed_profile_ore: dict[int, float]) -> float:
    """Forecast all-in price at the configured place, kr/kWh incl. VAT.

    all-in = spot_ex_vat * VAT + fixed(hour of day), where *fixed* is grid tariff + Energinet
    tariffs + electricity tax (incl. VAT). The forecast's spot part is kept and the fixed part is
    replaced by the place's latest published per-hour profile. Falls back to the representative
    area's all-in price when either piece is missing.
    """
    fixed = fixed_profile_ore.get(hour.local_hour)
    if hour.spot_ex_vat_ore is None or fixed is None:
        return round(hour.all_in_ore / 100, 4)
    return round((hour.spot_ex_vat_ore * DANISH_VAT + fixed) / 100, 4)


def place_shifted_band(
    hour: ForecastHour, fixed_profile_ore: dict[int, float]
) -> tuple[float, float] | None:
    """The publisher's own p10/p90 moved onto the configured place, kr/kWh (or ``None``).

    The band only concerns the spot part, so it is shifted by the same tariff difference as the
    central value.
    """
    if hour.p10_ore is None or hour.p90_ore is None:
        return None
    shift = place_shifted_price(hour, fixed_profile_ore) - hour.all_in_ore / 100
    return round(hour.p10_ore / 100 + shift, 4), round(hour.p90_ore / 100 + shift, 4)
