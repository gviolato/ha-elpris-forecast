"""Data update coordinator: fetch, persist, score and model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import FirmData, ForecastData, parse_prognose, parse_timepris
from .const import (
    CONF_FORGETTING_FACTOR,
    CONF_MIN_EFFECTIVE_N,
    CONF_MIN_SCORED_DAYS,
    CONF_PLACE,
    CONF_SMOOTHING_HOURS,
    DB_FILENAME,
    DEFAULT_FORGETTING_FACTOR,
    DEFAULT_MIN_EFFECTIVE_N,
    DEFAULT_MIN_SCORED_DAYS,
    DEFAULT_SMOOTHING_HOURS,
    DOMAIN,
    FIRM_REFRESH_MINUTES,
    FORECAST_REFRESH_MINUTES,
    PROGNOSE_URL,
    TIMEPRIS_URL,
)
from .storage import ActualRow, Store
from .timeline import forecast_rows
from .uncertainty import ErrorModel

_LOGGER = logging.getLogger(__name__)
_TIMEOUT = aiohttp.ClientTimeout(total=30)


@dataclass
class ElprisData:
    """Everything the entities need."""

    firm: FirmData | None
    forecast: ForecastData | None
    model: ErrorModel
    counts: dict[str, int]
    model_updated_at: datetime | None


type ElprisConfigEntry = ConfigEntry[ElprisCoordinator]


class ElprisCoordinator(DataUpdateCoordinator[ElprisData]):
    """Polls elpriser.dk, stores history in SQLite and maintains the error model."""

    config_entry: ElprisConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ElprisConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=FIRM_REFRESH_MINUTES),
        )
        self.place: str = entry.data[CONF_PLACE]
        opts = entry.options
        self.forgetting_factor = float(opts.get(CONF_FORGETTING_FACTOR, DEFAULT_FORGETTING_FACTOR))
        self.min_scored_days = int(opts.get(CONF_MIN_SCORED_DAYS, DEFAULT_MIN_SCORED_DAYS))
        self.min_effective_n = float(opts.get(CONF_MIN_EFFECTIVE_N, DEFAULT_MIN_EFFECTIVE_N))
        self.smoothing_hours = int(opts.get(CONF_SMOOTHING_HOURS, DEFAULT_SMOOTHING_HOURS))
        self.db_path = hass.config.path(DB_FILENAME)
        self._session = async_get_clientsession(hass)
        self._store: Store | None = None
        self._firm: FirmData | None = None
        self._forecast: ForecastData | None = None
        self._forecast_fetched: datetime | None = None
        self._model: ErrorModel | None = None
        self._model_updated_at: datetime | None = None

    async def _async_setup(self) -> None:
        self._store = await self.hass.async_add_executor_job(Store, self.db_path)

    async def _get_json(self, url: str, params: dict[str, str] | None = None) -> Any:
        async with self._session.get(url, params=params, timeout=_TIMEOUT) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def _async_update_data(self) -> ElprisData:
        now = dt_util.utcnow()
        try:
            self._firm = parse_timepris(await self._get_json(TIMEPRIS_URL, {"sted": self.place}))
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as err:
            if self._firm is None:
                raise UpdateFailed(f"Could not fetch firm prices: {err}") from err
            _LOGGER.warning("Firm prices unavailable, keeping previous data: %s", err)

        region = self._firm.region if self._firm else "DK1"
        if self._forecast_fetched is None or now - self._forecast_fetched >= timedelta(
            minutes=FORECAST_REFRESH_MINUTES
        ):
            try:
                self._forecast = parse_prognose(await self._get_json(PROGNOSE_URL), region)
                self._forecast_fetched = now
            except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as err:
                _LOGGER.warning("Forecast unavailable, keeping previous data: %s", err)

        counts = await self.hass.async_add_executor_job(self._persist_and_model, now)
        assert self._model is not None
        return ElprisData(self._firm, self._forecast, self._model, counts, self._model_updated_at)

    def _persist_and_model(self, now: datetime) -> dict[str, int]:
        """Runs in the executor: store forecasts/actuals, back-fill, refit when needed."""
        assert self._store is not None
        store = self._store
        if self._forecast is not None:
            new = store.add_forecasts(self.place, forecast_rows(self._firm, self._forecast), now)
            if new:
                _LOGGER.debug("Stored %d new forecast rows", new)
        scored = 0
        if self._firm is not None:
            scored = store.add_actuals(
                self.place,
                (
                    ActualRow(h.start, h.price, h.spot_ore, h.grid_ore, h.charges_ore)
                    for h in self._firm.hours
                ),
                now,
            )
        if scored or self._model is None:
            if scored:
                _LOGGER.debug("Scored %d forecast rows against published prices", scored)
            self._model = ErrorModel(
                self.forgetting_factor,
                self.min_scored_days,
                self.min_effective_n,
                self.smoothing_hours,
            ).fit(store.residuals(self.place))
            store.save_error_model(self.place, self._model.snapshot_rows(), now)
            self._model_updated_at = now
        return store.counts(self.place)
