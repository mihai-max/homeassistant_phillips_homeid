# Copyright (c) 2025, Renaud Allard <renaud@allard.it>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
"""Climate platform for Philips HomeID airfryers (remote control)."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import PhilipsHomeIDCoordinator
from .entity import PhilipsHomeIDEntity
from .local_api import (
    AIRFRYER_STATUS_COOKING,
    AIRFRYER_STATUS_MAINTAIN,
    AIRFRYER_STATUS_PARASETTING,
    AIRFRYER_STATUS_PAUSED,
    AIRFRYER_STATUS_PRECOOK,
    AIRFRYER_STATUS_SETTING,
    AIRFRYER_STATUS_USER_ACTION,
    PORT_HERMESAC,
    PORT_NUTRIMAX,
    VENUS_STYLE_PORTS,
)
from .select import HERMES_PRESETS, NUTRIMAX_PRESETS, SPECTRE_PRESETS, VENUS_PRESETS
from .sensor import get_device_type

_LOGGER = logging.getLogger(__name__)

# Statuses that map to HVACMode.HEAT (device is active / cooking)
_ACTIVE_STATUSES = frozenset({
    AIRFRYER_STATUS_COOKING,
    AIRFRYER_STATUS_PAUSED,
    AIRFRYER_STATUS_SETTING,
    AIRFRYER_STATUS_PRECOOK,
    AIRFRYER_STATUS_PARASETTING,
    AIRFRYER_STATUS_MAINTAIN,
    AIRFRYER_STATUS_USER_ACTION,
})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up climate entities from config entry."""
    coordinator: PhilipsHomeIDCoordinator = hass.data[DOMAIN][entry.entry_id]

    model_name = coordinator.device_info.model_name or ""
    device_type = get_device_type(model_name)

    if device_type not in ("airfryer", "airfryer_dual", "multicooker"):
        return

    if not coordinator.has_property("status", "airfryer"):
        return

    async_add_entities(
        [PhilipsAirfryerClimate(coordinator, coordinator.device_id)]
    )


class PhilipsAirfryerClimate(PhilipsHomeIDEntity, ClimateEntity):
    """Climate entity for Philips airfryer remote control.

    Exposes the airfryer as an HA climate device so users can set temperature,
    choose a cooking preset, and start/stop cooking from a single card.

    Supported devices:
      - SPECTRE (HD9255, HD9280, HD9285) – local HTTPS API
      - Venus (HD9875, HD9876, HD9880) – local HTTPS API & FUSION MQTT
      - Nutrimax / Hermes multicookers – FUSION MQTT

    HVAC mode mapping:
      OFF  → standby / finish / error
      HEAT → cooking / pause / setting / precook / maintain / user_action
    """

    _attr_translation_key = "airfryer"
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = 40
    _attr_max_temp = 200
    _attr_target_temperature_step = 5.0
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: PhilipsHomeIDCoordinator, device_id: str) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{device_id}_climate"
        presets = self._get_presets()
        self._id_to_name: dict[int, str] = presets
        self._name_to_id: dict[str, int] = {v: k for k, v in presets.items()}
        self._attr_preset_modes = list(presets.values())

    def _get_presets(self) -> dict[int, str]:
        """Return the preset map for this device's architecture."""
        port = self.coordinator.device_info.airfryer_port
        if port == PORT_NUTRIMAX:
            return NUTRIMAX_PRESETS
        if port == PORT_HERMESAC:
            return HERMES_PRESETS
        if port in VENUS_STYLE_PORTS:
            return VENUS_PRESETS
        # Default: SPECTRE (HD9255, HD9280, HD9285)
        return SPECTRE_PRESETS

    # --- State properties ---

    @property
    def hvac_mode(self) -> HVACMode:
        """Return current HVAC mode based on airfryer status."""
        status = self._get_property_value("status", "airfryer")
        if status in _ACTIVE_STATUSES:
            return HVACMode.HEAT
        return HVACMode.OFF

    @property
    def current_temperature(self) -> float | None:
        """Return the measured cavity temperature (Venus devices only).

        SPECTRE devices (HD9255/HD9280/HD9285) do not expose cur_temp over
        the local HTTP API, so this returns None for those models.
        """
        value = self._get_property_value("cur_temp", "airfryer")
        if value is not None:
            try:
                return float(value)
            except (ValueError, TypeError):
                return None
        return None

    @property
    def target_temperature(self) -> float | None:
        """Return the target cooking temperature."""
        value = self._get_property_value("temp", "airfryer")
        if value is not None:
            try:
                temp = float(value)
                # Return None when device is in standby with 0°C placeholder
                return temp if temp >= self._attr_min_temp else None
            except (ValueError, TypeError):
                return None
        return None

    @property
    def preset_mode(self) -> str | None:
        """Return the active cooking preset."""
        value = self._get_property_value("preset", "airfryer")
        if value is not None:
            try:
                return self._id_to_name.get(int(value))
            except (ValueError, TypeError):
                return None
        return None

    @property
    def available(self) -> bool:
        """Return True when the airfryer port has responded."""
        if not super().available:
            return False
        return self._has_property("status", "airfryer")

    # --- Control methods ---

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Start or stop cooking."""
        if hvac_mode == HVACMode.HEAT:
            await self.coordinator.async_airfryer_start()
        elif hvac_mode == HVACMode.OFF:
            await self.coordinator.async_airfryer_stop()

    async def async_turn_on(self) -> None:
        """Start cooking."""
        await self.coordinator.async_airfryer_start()

    async def async_turn_off(self) -> None:
        """Stop cooking and return to standby."""
        await self.coordinator.async_airfryer_stop()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the target cooking temperature.

        Sends only the temperature field; does not change the cooking state.
        While cooking, SPECTRE devices accept the new value directly.
        Venus devices perform an automatic pause-set-resume cycle internally.
        """
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is not None:
            await self.coordinator.async_airfryer_update_settings(temp=int(temp))

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set the cooking preset / programme."""
        preset_id = self._name_to_id.get(preset_mode)
        if preset_id is not None:
            await self.coordinator.async_airfryer_set_settings(preset=preset_id)
