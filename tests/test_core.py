"""Tests of the pure modules (parsing, storage, error model, timeline)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math
import random
from zoneinfo import ZoneInfo

import pytest

from custom_components.elpris_forecast.api import (
    parse_prognose,
    parse_timepris,
    place_shifted_band,
    place_shifted_price,
)
from custom_components.elpris_forecast.storage import ActualRow, ForecastRow, Residual, Store
from custom_components.elpris_forecast.timeline import build_timeline
from custom_components.elpris_forecast.uncertainty import Z90, ErrorModel, HorizonStats

TZ = ZoneInfo("Europe/Copenhagen")
UTC = timezone.utc


def _local(day: str, hour: int) -> str:
    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00").replace(tzinfo=TZ).isoformat()


def _timepris(days: dict[str, float]) -> dict:
    """Firm prices: spot 100 øre incl. VAT, fixed part = `fixed` (+ 50 øre 17-20h)."""
    out = {"place": {"segment": "1234", "region": "DK1", "gridCompany": "Grid A/S"}}
    for key, (day, fixed) in zip(("today", "tomorrow"), days.items()):
        hours = []
        for h in range(24):
            grid = fixed + (50 if 17 <= h <= 20 else 0)
            hours.append(
                {
                    "start": _local(day, h),
                    "krPerKwhInclVat": round((100 + grid + 10) / 100, 4),
                    "spotOreInclVat": 100,
                    "gridOreInclVat": grid,
                    "chargesOreInclVat": 10,
                }
            )
        out[key] = {"day": day, "hours": hours}
    return out


def _prognose(day: str, issued: str, spot_ex_vat: float = 80.0, kind: str = "forecast") -> dict:
    hours = [
        {
            "start": _local(day, h),
            "allInOreInclVat": spot_ex_vat * 1.25 + 30,
            "spotOreExVat": spot_ex_vat,
            "p10OreInclVat": spot_ex_vat * 1.25 + 30 - 20,
            "p90OreInclVat": spot_ex_vat * 1.25 + 30 + 40,
        }
        for h in range(24)
    ]
    return {
        "generatedAt": issued,
        "regions": [
            {
                "region": "DK1",
                "runs": [{"runId": 7, "issuedAt": issued, "modelId": "m1"}],
                "days": [
                    {"day": "2026-01-01", "kind": "known", "runId": 7, "hours": hours},
                    {"day": day, "kind": kind, "runId": 7, "issuedAt": issued, "hours": hours},
                ],
            },
            {"region": "DK2", "days": []},
        ],
    }


# --- api ---------------------------------------------------------------------------------


def test_timepris_profile_uses_latest_day() -> None:
    firm = parse_timepris(_timepris({"2026-01-05": 20, "2026-01-06": 30}))
    assert len(firm.hours) == 48
    assert firm.region == "DK1"
    assert firm.fixed_profile_ore[3] == 40  # tomorrow's grid 30 + charges 10
    assert firm.fixed_profile_ore[18] == 90
    assert firm.hours[0].start == datetime(2026, 1, 4, 23, tzinfo=UTC)


def test_prognose_skips_known_days_and_other_regions() -> None:
    fc = parse_prognose(_prognose("2026-01-08", "2026-01-05T08:00:00Z"), "dk1")
    assert len(fc.hours) == 24
    assert {h.kind for h in fc.hours} == {"forecast"}
    assert fc.hours[0].issued_at == datetime(2026, 1, 5, 8, tzinfo=UTC)
    assert parse_prognose(_prognose("2026-01-08", "2026-01-05T08:00:00Z"), "DK2").hours == []


def test_place_shift_replaces_fixed_part() -> None:
    fc = parse_prognose(_prognose("2026-01-08", "2026-01-05T08:00:00Z", spot_ex_vat=80), "DK1")
    h18 = next(h for h in fc.hours if h.local_hour == 18)
    profile = {18: 90.0}
    assert place_shifted_price(h18, profile) == pytest.approx((80 * 1.25 + 90) / 100)
    assert place_shifted_price(h18, {}) == pytest.approx((80 * 1.25 + 30) / 100)  # fallback
    p10, p90 = place_shifted_band(h18, profile)
    assert p10 == pytest.approx((100 + 90 - 20) / 100)
    assert p90 == pytest.approx((100 + 90 + 40) / 100)


# --- storage -----------------------------------------------------------------------------


def test_store_backfills_only_forecasts_issued_before_the_price(tmp_path) -> None:
    store = Store(str(tmp_path / "t.db"))
    target = datetime(2026, 1, 8, 12, tzinfo=UTC)
    early = ForecastRow(target, target - timedelta(hours=50), 1.10)
    late = ForecastRow(target, target - timedelta(hours=2), 1.30)
    t_seen = target - timedelta(hours=10)
    assert store.add_forecasts("p", [early], t_seen - timedelta(hours=30)) == 1
    assert store.add_forecasts("p", [early], t_seen) == 0  # deduplicated
    assert store.add_actuals("p", [ActualRow(target, 1.00)], t_seen) == 1
    # a forecast issued after the price was published must never be scored
    store.add_forecasts("p", [late], t_seen + timedelta(hours=9))
    assert store.add_actuals("p", [ActualRow(target, 1.00)], t_seen + timedelta(hours=9)) == 0
    res = store.residuals("p")
    assert [(r.horizon_hours, round(r.residual, 6)) for r in res] == [(50, 0.1)]
    assert store.counts("p") == {"forecasts": 2, "scored": 1, "actuals": 1}
    assert store.residuals("other") == []


# --- uncertainty -------------------------------------------------------------------------


def test_ewma_matches_direct_weighted_estimates() -> None:
    lam, xs = 0.9, [0.3, -0.1, 0.5, 0.2, -0.4, 0.05]
    stats = HorizonStats()
    for x in xs:
        stats.update(x, lam)
    w = [lam ** (len(xs) - 1 - i) for i in range(len(xs))]
    sw, sw2 = sum(w), sum(v * v for v in w)
    mean = sum(wi * x for wi, x in zip(w, xs)) / sw
    var = sum(wi * (x - mean) ** 2 for wi, x in zip(w, xs)) / (sw - sw2 / sw)
    assert stats.mean == pytest.approx(mean)
    assert stats.variance == pytest.approx(var)
    assert stats.effective_n == pytest.approx(sw**2 / sw2)


def _residuals(days: int, horizons: range, sigma: float, bias: float = 0.0, seed: int = 1):
    rng = random.Random(seed)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Residual(base + timedelta(days=d, hours=h % 24), h, rng.gauss(bias, sigma))
        for d in range(days)
        for h in horizons
    ]


def test_band_needs_distinct_days() -> None:
    model = ErrorModel(0.95, 3, 5, 6).fit(_residuals(2, range(30, 60), 0.2))
    assert model.band(40) is None
    assert model.quantiles(1.0, 40) == (None, 1.0, None)
    model = ErrorModel(0.95, 3, 5, 6).fit(_residuals(3, range(30, 60), 0.2))
    assert model.band(40) is not None
    assert model.band(100) is None  # no data that far out


def test_quantiles_recover_bias_and_sigma() -> None:
    model = ErrorModel(1.0, 3, 5, 6).fit(_residuals(200, range(40, 41), 0.2, bias=0.05))
    p10, p50, p90 = model.quantiles(1.0, 40)
    assert p50 == pytest.approx(0.95, abs=0.02)
    assert (p90 - p10) / (2 * Z90) == pytest.approx(0.2, rel=0.15)


def test_sigma_is_non_decreasing_in_horizon() -> None:
    near = _residuals(10, range(20, 40), 0.3, seed=2)
    far = _residuals(10, range(60, 80), 0.1, seed=3)
    model = ErrorModel(0.95, 3, 5, 3).fit(sorted(near + far, key=lambda r: r.target))
    sigmas = [model.band(h).sigma for h in range(20, 80) if model.band(h)]
    assert all(b >= a for a, b in zip(sigmas, sigmas[1:]))
    assert model.summary()["scored_days"] == 10


# --- timeline ----------------------------------------------------------------------------


def test_timeline_merges_firm_forecast_and_bands() -> None:
    firm = parse_timepris(_timepris({"2026-01-05": 20, "2026-01-06": 30}))
    fc = parse_prognose(_prognose("2026-01-07", "2026-01-05T08:00:00Z"), "DK1")
    now = datetime(2026, 1, 5, 10, 30, tzinfo=UTC)
    empty = ErrorModel(0.95, 3, 5, 6).fit([])
    tl = build_timeline(now, firm, fc, empty, TZ)
    assert len(tl) == 24 * 2 - 11 + 24  # rest of today + tomorrow firm, then forecast day
    first = tl[0]
    assert first["datetime"] == "2026-01-05T11:00:00+01:00"
    assert not first["is_forecast"] and first["p10"] == first["price"] == first["p90"]
    fcs = [e for e in tl if e["is_forecast"]]
    assert len(fcs) == 24 and all(e["band_source"] == "publisher" for e in fcs)
    assert all(e["p10"] < e["p50"] < e["p90"] for e in fcs)
    assert [e["horizon_hours"] for e in tl] == list(range(len(tl)))

    trained = ErrorModel(0.95, 3, 5, 6).fit(_residuals(5, range(1, 169), 0.1))
    fcs = [e for e in build_timeline(now, firm, fc, trained, TZ) if e["is_forecast"]]
    assert all(e["band_source"] == "model" for e in fcs)
    assert all(math.isclose(e["p90"] - e["p50"], e["p50"] - e["p10"], abs_tol=1e-3) for e in fcs)


def test_dates_are_utc_in_db(tmp_path) -> None:
    store = Store(str(tmp_path / "t.db"))
    t = datetime(2026, 3, 29, 1, tzinfo=UTC)  # DST switch night
    store.add_forecasts("p", [ForecastRow(t, t - timedelta(hours=30), 1.0)], t - timedelta(days=1))
    store.add_actuals("p", [ActualRow(t, 0.9)], t - timedelta(hours=12))
    (r,) = store.residuals("p")
    assert r.target == t and r.horizon_hours == 30 and r.target.date() == date(2026, 3, 29)
