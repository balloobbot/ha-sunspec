"""
Custom integration to integrate SunSpec with Home Assistant.

For more details about this integration, please refer to
https://github.com/cjne/ha-sunspec
"""

import asyncio
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.core_config import Config
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.update_coordinator import UpdateFailed

from .api import SunSpecApiClient
from .api import SunSpecMapShiftError
from .api import UpdateReport
from .const import CONF_ENABLED_MODELS
from .const import CONF_HOST
from .const import CONF_PORT
from .const import CONF_SCAN_INTERVAL
from .const import CONF_UNIT_ID
from .const import DEFAULT_MODELS
from .const import DOMAIN
from .const import PLATFORMS
from .const import STARTUP_MESSAGE

SCAN_INTERVAL = timedelta(seconds=30)

_LOGGER: logging.Logger = logging.getLogger(__package__)


async def async_setup(hass: HomeAssistant, config: Config):
    """Set up this integration using YAML is not supported."""
    return True


async def async_migrate_entry(hass, config_entry: ConfigEntry):
    """Migrate old entry."""
    _LOGGER.debug("Migrating configuration from version %s", config_entry.version)

    if config_entry.version == 1:
        # Migrate from version 1 to version 2
        # Version 1 used 'slave_id', version 2 uses 'unit_id'
        new_data = {**config_entry.data}

        # Migrate slave_id to unit_id if needed
        if "slave_id" in new_data:
            if "unit_id" not in new_data:
                # No unit_id exists, migrate slave_id to unit_id
                new_data["unit_id"] = new_data.pop("slave_id")
                _LOGGER.info(
                    "Migrated 'slave_id' to 'unit_id': %s", new_data["unit_id"]
                )
            else:
                # Both exist, remove slave_id and keep unit_id
                new_data.pop("slave_id")
                _LOGGER.info(
                    "Removed 'slave_id', keeping existing 'unit_id': %s",
                    new_data["unit_id"],
                )

        # Update the config entry with new version and data
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=2)
        _LOGGER.info("Migration to version %s successful", config_entry.version)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up this integration using UI."""
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
        _LOGGER.info(STARTUP_MESSAGE)

    host = entry.data.get(CONF_HOST)
    port = entry.data.get(CONF_PORT)
    unit_id = entry.data.get(CONF_UNIT_ID, 1)

    client = SunSpecApiClient(host, port, unit_id)

    _LOGGER.debug("Setup conifg entry for SunSpec")
    coordinator = SunSpecDataUpdateCoordinator(hass, client=client, entry=entry)
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""

    _LOGGER.debug("Unload entry")
    unloaded = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, platform)
                for platform in PLATFORMS
            ]
        )
    )
    if unloaded:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator.unsub()
        await coordinator.api.async_close()

    return True  # unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)


def get_sunspec_unique_id(
    config_entry_id: str, key: str, model_id: int, model_index: int
) -> str:
    """Create a uniqe id for a SunSpec entity"""
    return f"{config_entry_id}_{key}-{model_id}-{model_index}"


class SunSpecDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(self, hass: HomeAssistant, client: SunSpecApiClient, entry) -> None:
        """Initialize."""
        self.api = client
        self.hass = hass
        self.entry = entry

        _LOGGER.debug("Data: %s", entry.data)
        _LOGGER.debug("Options: %s", entry.options)
        models = entry.options.get(
            CONF_ENABLED_MODELS, entry.data.get(CONF_ENABLED_MODELS, DEFAULT_MODELS)
        )
        scan_interval = timedelta(
            seconds=entry.options.get(
                CONF_SCAN_INTERVAL,
                entry.data.get(CONF_SCAN_INTERVAL, SCAN_INTERVAL.total_seconds()),
            )
        )
        self.option_model_filter = set(map(lambda m: int(m), models))
        self.report = UpdateReport(set(), {})
        self._logged_failures: set[str] = set()
        self.unsub = entry.add_update_listener(async_reload_entry)
        _LOGGER.debug(
            "Setup entry with models %s, scan interval %s. IP: %s Port: %s ID: %s",
            self.option_model_filter,
            scan_interval,
            entry.data.get(CONF_HOST),
            entry.data.get(CONF_PORT),
            entry.data.get(CONF_UNIT_ID),
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=scan_interval,
            config_entry=entry,
        )

    async def _async_update_data(self):
        """Update data via library."""
        _LOGGER.debug("SunSpec Update data coordinator update")
        try:
            data, self.report = await self.api.async_read(self.option_model_filter)
        except SunSpecMapShiftError as exception:
            # The components sit at the addresses the setup scan found. The device
            # rearranged its model chain, so setup has to run again.
            _LOGGER.warning("SunSpec register map changed, reloading: %s", exception)
            self.hass.config_entries.async_schedule_reload(self.entry.entry_id)
            raise UpdateFailed() from exception
        except Exception as exception:
            _LOGGER.warning(exception)
            raise UpdateFailed() from exception
        else:
            self._log_failures()
            return data
        finally:
            # This integration polls infrequently and some inverters accept only
            # one Modbus connection, so the link is not held between updates.
            await self.api.async_disconnect()

    def _log_failures(self) -> None:
        """Report a change in which models are not answering, once per change."""
        failed = set(self.report.failed)
        if failed == self._logged_failures:
            return
        self._logged_failures = failed
        if failed:
            _LOGGER.warning(
                "Keeping the last values for models that did not answer: %s",
                ", ".join(
                    f"{name} ({error})" for name, error in self.report.failed.items()
                ),
            )
        else:
            _LOGGER.info("Every enabled model is answering again")
