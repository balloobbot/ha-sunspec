"""Test the SunSpec diagnostics download."""

from homeassistant.core import HomeAssistant
from modbus_connection import ModbusConnectionError
from modbus_connection import ModbusTimeoutError

from custom_components.sunspec.const import DOMAIN
from custom_components.sunspec.diagnostics import async_get_config_entry_diagnostics

from . import setup_mock_sunspec_config_entry
from .test_resilience import IN_MODEL_103

# The common model sits at the head of the chain, model 103 well past it.
IN_MODEL_1 = 40002


async def test_diagnostics_hold_the_registers_and_the_last_poll(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """An issue report needs the raw map, including what only setup reads.

    Model 1 is not one of the polled models - it is read once, for the device's
    identity - and it is exactly the block that says which device this is.
    """
    config_entry = await setup_mock_sunspec_config_entry(hass)

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)

    assert diagnostics["updated"] == ["103:0", "160:0"]
    assert diagnostics["failed"] == {}
    holding = diagnostics["registers"]["holding"]
    assert IN_MODEL_1 in holding
    assert IN_MODEL_103 in holding


async def test_diagnostics_name_the_models_that_did_not_answer(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """The errors are stringified, so the payload serializes."""
    config_entry = await setup_mock_sunspec_config_entry(hass)
    coordinator = hass.data[DOMAIN][config_entry.entry_id]

    coordinator.api._unit.fail_read(IN_MODEL_103, ModbusTimeoutError("slow block"))
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["updated"] == ["160:0"]
    assert "slow block" in diagnostics["failed"]["103:0"]


async def test_diagnostics_of_a_device_that_will_not_answer(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """A sleeping inverter still has a report worth downloading."""
    config_entry = await setup_mock_sunspec_config_entry(hass)
    coordinator = hass.data[DOMAIN][config_entry.entry_id]

    coordinator.api._unit.fail_requests(ModbusConnectionError("link down"))

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert "link down" in diagnostics["registers_error"]
    assert diagnostics["updated"] == ["103:0", "160:0"]
