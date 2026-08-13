"""Diagnostics support for SunSpec."""

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from modbus_connection import ModbusError

from .const import DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return the last poll's outcome and the raw registers behind it."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    report = coordinator.report
    diagnostics: dict[str, Any] = {
        "updated": sorted(report.updated),
        "failed": {name: str(error) for name, error in report.failed.items()},
    }
    try:
        diagnostics["registers"] = await coordinator.api.async_read_raw()
    except ModbusError as err:
        # An inverter is asleep every night; the report is still worth having.
        diagnostics["registers_error"] = str(err)
    return diagnostics
