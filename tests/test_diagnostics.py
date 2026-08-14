"""Test the SunSpec diagnostics download."""

from homeassistant.core import HomeAssistant
from modbus_connection import ModbusConnectionError
from modbus_connection import ModbusTimeoutError
from modbus_connection.mock import MockModbusConnection
from modbus_connection.model.sunspec import scan

from custom_components.sunspec.const import DOMAIN
from custom_components.sunspec.diagnostics import async_get_config_entry_diagnostics

from . import setup_mock_sunspec_config_entry
from .conftest import BASE_ADDRESS
from .test_resilience import IN_MODEL_103
from .test_resilience import IN_MODEL_160

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


async def test_a_download_does_not_look_like_a_poll(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """The dump reads the device, but nothing is notified.

    The read is real, so without this every listener would fire off the poll
    cycle and write a state for each entity at a moment the coordinator's report
    knows nothing about.
    """
    config_entry = await setup_mock_sunspec_config_entry(hass)
    coordinator = hass.data[DOMAIN][config_entry.entry_id]

    notified = []
    for components in coordinator.api._components.values():
        for component in components:
            component.add_update_listener(lambda: notified.append(1))

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)

    assert diagnostics["registers"]["holding"]
    assert notified == []


async def test_a_downloaded_snapshot_scans_again(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """A dump only backs a hardware-free test if it replays into the mock.

    The chain markers belong to no component, so no component read covers them -
    and without them a replayed scan cannot find where the chain starts or ends.
    """
    config_entry = await setup_mock_sunspec_config_entry(hass)
    coordinator = hass.data[DOMAIN][config_entry.entry_id]

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)

    unit = MockModbusConnection().for_unit(1)
    unit.load_raw(diagnostics["registers"])

    assert sorted(await scan(unit, BASE_ADDRESS)) == sorted(coordinator.api._models)


async def test_diagnostics_name_the_models_that_did_not_answer(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """The errors are stringified, so the payload serializes."""
    config_entry = await setup_mock_sunspec_config_entry(hass)
    coordinator = hass.data[DOMAIN][config_entry.entry_id]

    # On the second model polled: the first has answered, so the poll goes on.
    coordinator.api._unit.fail_read(IN_MODEL_160, ModbusTimeoutError("slow block"))
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["updated"] == ["103:0"]
    assert "slow block" in diagnostics["failed"]["160:0"]


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
