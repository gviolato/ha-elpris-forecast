"""Sensors: the merged price timeline and the error-model diagnostics."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import ATTRIBUTION, DOMAIN
from .coordinator import ElprisConfigEntry, ElprisCoordinator
from .timeline import build_timeline, hour_floor

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ElprisConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([ElprisPriceSensor(coordinator), ElprisErrorModelSensor(coordinator)])


class _Base(CoordinatorEntity[ElprisCoordinator]):
    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: ElprisCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Elpriser.dk",
            model="Price + 7-day forecast",
            entry_type=DeviceEntryType.SERVICE,
        )


class ElprisPriceSensor(_Base, SensorEntity):
    """Current hourly price; the 168 h merged timeline is in the attributes."""

    _attr_native_unit_of_measurement = "DKK/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:transmission-tower"
    # ~25 kB: too large for (and pointless in) the recorder.
    _unrecorded_attributes = frozenset({"timeline"})

    def __init__(self, coordinator: ElprisCoordinator) -> None:
        super().__init__(coordinator, "price")
        self._timeline: list[dict] = []
        self._attrs: dict[str, Any] = {}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Roll the timeline over on the hour, independent of the polling interval.
        self.async_on_remove(
            async_track_time_change(self.hass, self._on_hour, minute=0, second=2)
        )
        self._recompute()

    @callback
    def _on_hour(self, _now: datetime) -> None:
        self._recompute()
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._recompute()
        super()._handle_coordinator_update()

    @callback
    def _recompute(self) -> None:
        data = self.coordinator.data
        if data is None:
            return
        now = dt_util.utcnow()
        tz = dt_util.get_default_time_zone()
        self._timeline = build_timeline(now, data.firm, data.forecast, data.model, tz)
        current = self._timeline[0] if self._timeline else None
        if current is not None and dt_util.parse_datetime(current["datetime"]) != hour_floor(now):
            current = None
        self._attr_native_value = current["price"] if current else None
        place = data.firm.place if data.firm else {}
        issued = sorted({h.issued_at for h in data.forecast.hours}) if data.forecast else []
        self._attrs = {
            "current_price": self._attr_native_value,
            "place": place.get("segment"),
            "region": data.firm.region if data.firm else None,
            "grid_company": place.get("gridCompany"),
            "firm_until": data.firm.hours[-1].start.isoformat() if data.firm and data.firm.hours else None,
            "forecast_issued_at": [i.isoformat() for i in issued],
            "band_sources": sorted(
                {e["band_source"] for e in self._timeline if e.get("band_source")}
            ),
            "timeline": self._timeline,
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._attrs


class ElprisErrorModelSensor(_Base, SensorEntity):
    """Diagnostics of the EWMA error model; state = distinct days scored so far."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "d"
    _attr_icon = "mdi:chart-bell-curve"

    def __init__(self, coordinator: ElprisCoordinator) -> None:
        super().__init__(coordinator, "error_model")

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data
        return len(data.model.scored_days) if data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if data is None:
            return {}
        c = self.coordinator
        return {
            **data.model.summary(),
            "forgetting_factor": c.forgetting_factor,
            "min_scored_days": c.min_scored_days,
            "min_effective_n": c.min_effective_n,
            "smoothing_hours": c.smoothing_hours,
            "db_forecast_rows": data.counts.get("forecasts"),
            "db_scored_rows": data.counts.get("scored"),
            "db_actual_rows": data.counts.get("actuals"),
            "model_updated_at": data.model_updated_at.isoformat() if data.model_updated_at else None,
        }
