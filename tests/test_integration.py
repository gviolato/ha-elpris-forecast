"""End-to-end test inside Home Assistant (config flow, coordinator, sensors, SQLite)."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.elpris_forecast.const import DOMAIN, PROGNOSE_URL, TIMEPRIS_URL

from .test_core import _prognose, _timepris


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def feeds(aioclient_mock):
    aioclient_mock.get(TIMEPRIS_URL, params={"sted": "1234"},
                       json=_timepris({"2026-01-05": 20, "2026-01-06": 30}))
    aioclient_mock.get(PROGNOSE_URL, json=_prognose("2026-01-07", "2026-01-05T08:00:00Z"))
    return aioclient_mock


async def test_config_flow(hass: HomeAssistant, feeds) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"place": "nope"})
    assert result["errors"] == {"place": "invalid_place"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"place": "1234"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"place": "1234"}
    assert result["title"] == "Elpris 1234 (Grid A/S)"


async def test_sensors(hass: HomeAssistant, feeds, freezer) -> None:
    freezer.move_to("2026-01-05T10:30:00+00:00")
    await hass.config.async_set_time_zone("Europe/Copenhagen")
    entry = MockConfigEntry(domain=DOMAIN, data={"place": "1234"}, unique_id="1234", title="Elpris")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    price = hass.states.get("sensor.elpris_price")
    assert price is not None
    assert float(price.state) == pytest.approx(1.30)  # spot 100 + grid 20 + charges 10 øre
    attrs = price.attributes
    assert attrs["unit_of_measurement"] == "DKK/kWh"
    assert attrs["current_price"] == pytest.approx(1.30)
    tl = attrs["timeline"]
    assert tl[0]["datetime"] == "2026-01-05T11:00:00+01:00"
    forecast = [e for e in tl if e["is_forecast"]]
    assert len(forecast) == 24 and forecast[0]["band_source"] == "publisher"
    assert attrs["band_sources"] == ["publisher"]

    model = hass.states.get("sensor.elpris_forecast_error_model")
    assert model is not None and model.state == "0"
    assert model.attributes["db_forecast_rows"] == 24
    assert model.attributes["db_actual_rows"] == 48

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_options_agreement(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(TIMEPRIS_URL, params={"sted": "1234", "aftale": "nope-x"}, status=404)
    aioclient_mock.get(TIMEPRIS_URL, params={"sted": "1234", "aftale": "s-p"},
                       json=_timepris({"2026-01-05": 20}, markup=9.7))
    aioclient_mock.get(TIMEPRIS_URL, params={"sted": "1234"}, json=_timepris({"2026-01-05": 20}))
    aioclient_mock.get(PROGNOSE_URL, json=_prognose("2026-01-07", "2026-01-05T08:00:00Z"))
    entry = MockConfigEntry(domain=DOMAIN, data={"place": "1234"}, unique_id="1234", title="Elpris")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    base = {"band_source": "publisher", "forgetting_factor": 0.95, "min_scored_days": 3,
            "min_effective_n": 5, "smoothing_hours": 6}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**base, "agreement": "nope-x"})
    assert result["errors"] == {"agreement": "invalid_agreement"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**base, "agreement": "s-p"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    state = hass.states.get("sensor.elpris_price")
    assert state.attributes["agreement"] == "S – P"
    assert state.attributes["supplier_markup"] == pytest.approx(0.097)
