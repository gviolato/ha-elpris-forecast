"""Config and options flow."""

from __future__ import annotations

import re
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    BAND_MODEL,
    BAND_PUBLISHER,
    CONF_AGREEMENT,
    CONF_BAND_SOURCE,
    CONF_FORGETTING_FACTOR,
    CONF_MIN_EFFECTIVE_N,
    CONF_MIN_SCORED_DAYS,
    CONF_PLACE,
    CONF_SMOOTHING_HOURS,
    DEFAULT_BAND_SOURCE,
    DEFAULT_FORGETTING_FACTOR,
    DEFAULT_MIN_EFFECTIVE_N,
    DEFAULT_MIN_SCORED_DAYS,
    DEFAULT_SMOOTHING_HOURS,
    DOMAIN,
    TIMEPRIS_URL,
)

# Same pattern as the API's `sted` parameter: dk1/dk2, a postcode, or "a" + grid area code.
PLACE_RE = re.compile(r"^([dD][kK][12]|[0-9]{4}|[aA][0-9A-Za-z]{2,8})$")
AGREEMENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,119}$")


async def _check_agreement(hass, place: str, agreement: str) -> str | None:
    """Return an error key if elpriser.dk does not know the agreement for this place."""
    if not AGREEMENT_RE.match(agreement):
        return "invalid_agreement"
    try:
        session = async_get_clientsession(hass)
        async with session.get(
            TIMEPRIS_URL,
            params={"sted": place, "aftale": agreement},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status == 404:
                return "invalid_agreement"
            resp.raise_for_status()
    except (aiohttp.ClientError, TimeoutError):
        return "cannot_connect"
    return None


class ElprisConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the place (postcode) whose prices to track."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            place = user_input[CONF_PLACE].strip().lower()
            if not PLACE_RE.match(place):
                errors[CONF_PLACE] = "invalid_place"
            else:
                await self.async_set_unique_id(place)
                self._abort_if_unique_id_configured()
                try:
                    session = async_get_clientsession(self.hass)
                    async with session.get(
                        TIMEPRIS_URL, params={"sted": place}, timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp:
                        if resp.status in (400, 404):
                            errors[CONF_PLACE] = "invalid_place"
                        else:
                            resp.raise_for_status()
                            payload = await resp.json(content_type=None)
                except (aiohttp.ClientError, TimeoutError, ValueError):
                    errors["base"] = "cannot_connect"
                if not errors:
                    info = payload.get("place") or {}
                    title = f"Elpris {place.upper()} ({info.get('gridCompany', info.get('region', ''))})"
                    return self.async_create_entry(title=title, data={CONF_PLACE: place})
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_PLACE): str}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> ElprisOptionsFlow:
        return ElprisOptionsFlow()


class ElprisOptionsFlow(OptionsFlowWithReload):
    """Tune the error model. Changing it re-fits the model from the full stored history."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            agreement = (user_input.get(CONF_AGREEMENT) or "").strip().lower()
            user_input[CONF_AGREEMENT] = agreement
            if agreement and (
                err := await _check_agreement(
                    self.hass, self.config_entry.data[CONF_PLACE], agreement
                )
            ):
                errors[CONF_AGREEMENT] = err
            else:
                return self.async_create_entry(data=user_input)
        o = user_input or self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_AGREEMENT,
                    description={"suggested_value": o.get(CONF_AGREEMENT, "")},
                ): str,
                vol.Required(
                    CONF_BAND_SOURCE, default=o.get(CONF_BAND_SOURCE, DEFAULT_BAND_SOURCE)
                ): vol.In([BAND_PUBLISHER, BAND_MODEL]),
                vol.Required(
                    CONF_FORGETTING_FACTOR,
                    default=o.get(CONF_FORGETTING_FACTOR, DEFAULT_FORGETTING_FACTOR),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=1.0)),
                vol.Required(
                    CONF_MIN_SCORED_DAYS,
                    default=o.get(CONF_MIN_SCORED_DAYS, DEFAULT_MIN_SCORED_DAYS),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
                vol.Required(
                    CONF_MIN_EFFECTIVE_N,
                    default=o.get(CONF_MIN_EFFECTIVE_N, DEFAULT_MIN_EFFECTIVE_N),
                ): vol.All(vol.Coerce(float), vol.Range(min=1, max=1000)),
                vol.Required(
                    CONF_SMOOTHING_HOURS,
                    default=o.get(CONF_SMOOTHING_HOURS, DEFAULT_SMOOTHING_HOURS),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=24)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
