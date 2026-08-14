"""One failing block must not take the rest of the poll with it.

Models used to be read through a single pooled plan, so the first block a device
was too slow to answer discarded every model's data and left the whole device
unavailable. Each model instance is now read on its own.
"""

from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.core import State
from modbus_connection import IllegalDataAddressError
from modbus_connection import ModbusConnectionError
from modbus_connection import ModbusTimeoutError
from modbus_connection import ServerDeviceBusyError
import pytest
from pytest_homeassistant_custom_component.common import (
    mock_restore_cache_with_extra_data,
)

from custom_components.sunspec.api import SunSpecApiClient
from custom_components.sunspec.const import DOMAIN

from . import TEST_INVERTER_SENSOR_DC_ENTITY_ID
from . import TEST_INVERTER_SENSOR_ENERGY_ENTITY_ID
from . import TEST_INVERTER_SENSOR_POWER_ENTITY_ID
from . import setup_mock_sunspec_config_entry
from .conftest import BASE_ADDRESS
from .conftest import TEST_DEVICE
from .conftest import patch_sunspec_device
from .conftest import sunspec_holding_registers

# Model 103 occupies 40090..40141 and model 160 occupies 40830..40879, so a read
# failure inside one of them cannot touch the other.
IN_MODEL_103 = 40100
IN_MODEL_160 = 40840
# Model 705 sizes its curve group from the count point here, which the build
# reads before the model can be polled at all.
MODEL_705_COUNT = 40595


async def test_a_failed_model_leaves_the_rest_fresh(hass, sunspec_client_mock):
    """A model whose block fails keeps its values; the others still refresh.

    Model 103 is polled first and answers, so 160's timeout is one slow block
    rather than a device that is not there.
    """
    api = SunSpecApiClient(host="test", port=123, unit_id=1)
    models, _ = await api.async_read({103, 160})
    before = models[160].getValue("module:0:DCA")

    unit = api._unit
    unit.holding[40093] = 4321  # the device's AC current changes
    unit.holding[40849] = 77  # so does the first module's DC current
    unit.fail_read(IN_MODEL_160, ModbusTimeoutError("slow inverter block"))
    models, report = await api.async_read({103, 160})

    assert not report.complete
    assert set(report.failed) == {"160:0"}
    assert isinstance(report.failed["160:0"], ModbusTimeoutError)
    assert report.updated == {"103:0"}
    assert models[160].getValue("module:0:DCA") == before != 77
    assert models[103].getValue("AphA") == 4321
    await api.async_close()


async def test_the_first_read_timing_out_gives_up_the_poll(hass, sunspec_client_mock):
    """A poll that has heard nothing at all stops at the first timeout.

    Nothing has answered, so the models after it would each pay a full timeout
    of their own - two minutes apiece here - for the same silence.
    """
    api = SunSpecApiClient(host="test", port=123, unit_id=1)
    await api.async_read({103, 160})

    unit = api._unit
    unit.fail_read(IN_MODEL_103, ModbusTimeoutError("asleep for the night"))
    unit.read_events.clear()
    with pytest.raises(ModbusTimeoutError):
        await api.async_read({103, 160})

    # The one read that timed out; model 160 was never reached.
    assert len(unit.read_events) == 1
    await api.async_close()


async def test_listeners_fire_at_the_end_and_only_for_fresh_models(
    hass, sunspec_client_mock
):
    """Nothing is notified until every model has been tried."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1)
    await api.async_read({103, 160})

    unit = api._unit
    seen = []
    api._components[103][0].add_update_listener(
        lambda: seen.append(len(unit.read_events))
    )
    api._components[160][0].add_update_listener(lambda: seen.append(-1))

    unit.fail_read(IN_MODEL_160, ModbusTimeoutError("slow inverter block"))
    unit.read_events.clear()
    await api.async_read({103, 160})

    # One notification, fired after the last read of the poll; none for 160.
    assert seen == [len(unit.read_events)]
    await api.async_close()


async def test_a_dead_link_raises_instead_of_reporting(hass, sunspec_client_mock):
    """Partial silence is not a partial update - the link itself is gone."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1)
    await api.async_read({103, 160})

    api._unit.fail_requests(ModbusConnectionError("link down"))
    with pytest.raises(ModbusConnectionError):
        await api.async_read({103, 160})
    await api.async_close()


async def test_a_healthy_poll_is_complete(hass, sunspec_client_mock):
    """Every model asked for is named in the report when nothing fails."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1)
    models, report = await api.async_read({1, 103, 160})

    assert report.complete
    assert report.failed == {}
    assert report.updated == {"1:0", "103:0", "160:0"}
    assert set(models) == {1, 103, 160}
    await api.async_close()


async def test_a_model_that_cannot_be_built_is_contained(hass, sunspec_client_mock):
    """A count point that will not answer costs its own model only.

    Sizing a nested group is I/O too, and it happens before the model can be
    polled - so without containment a model whose counts never answer would
    blank the whole device on every poll. Model 103 is built and read before
    705 is built at all, so the device has answered by the time its count point
    does not.
    """
    api = SunSpecApiClient(host="test", port=123, unit_id=1)
    api._unit.fail_read(MODEL_705_COUNT, ModbusTimeoutError("no answer"))

    models, report = await api.async_read({103, 705})
    assert set(report.failed) == {"705"}
    assert report.updated == {"103:0"}
    assert set(models) == {103}

    # The failure is not latched: the model is built and polled once it answers.
    api._unit.fail_read(MODEL_705_COUNT, None)
    models, report = await api.async_read({103, 705})
    assert report.complete
    assert report.updated == {"103:0", "705:0"}
    assert models[705].getValue("Crv:0:VRef") == 0.01
    await api.async_close()


async def test_a_count_point_timing_out_first_gives_up_the_poll(
    hass, sunspec_client_mock
):
    """The build reads too, so its timeout answers to the same test.

    Model 705 leads this poll, and sizing its curve group is the first thing
    asked of the device: nothing has answered, so 706 is not walked either.
    """
    api = SunSpecApiClient(host="test", port=123, unit_id=1)
    api._unit.fail_read(MODEL_705_COUNT, ModbusTimeoutError("no answer"))

    with pytest.raises(ModbusTimeoutError):
        await api.async_read({705, 706})

    assert 706 not in api._components
    await api.async_close()


async def test_a_count_point_refused_first_is_still_contained(
    hass, sunspec_client_mock
):
    """A device that refuses is a device that is there, so the poll goes on."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1)
    api._unit.fail_read(MODEL_705_COUNT, ServerDeviceBusyError())

    models, report = await api.async_read({705, 706})

    assert set(report.failed) == {"705"}
    assert report.updated == {"706:0"}
    assert set(models) == {706}
    await api.async_close()


async def test_reading_a_single_model_raises_its_error(hass, sunspec_client_mock):
    """There is nothing to contain when only one model was asked for."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1)
    await api.async_read({103})

    api._unit.fail_read(IN_MODEL_103, ModbusTimeoutError("slow inverter block"))
    with pytest.raises(ModbusTimeoutError):
        await api.async_get_data(103)
    await api.async_close()


async def test_only_the_failed_models_sensors_go_unavailable(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """The device keeps reporting; the model that did not answer does not.

    Model 103 is polled first and answers, so the timeout on 160 is a block this
    device is slow with rather than a device that has gone quiet.
    """
    config_entry = await setup_mock_sunspec_config_entry(hass)
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    assert hass.states.get(TEST_INVERTER_SENSOR_DC_ENTITY_ID).state != STATE_UNAVAILABLE

    coordinator.api._unit.fail_read(
        IN_MODEL_160, ModbusTimeoutError("slow inverter block")
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success
    assert set(coordinator.report.failed) == {"160:0"}
    assert hass.states.get(TEST_INVERTER_SENSOR_DC_ENTITY_ID).state == STATE_UNAVAILABLE
    power = hass.states.get(TEST_INVERTER_SENSOR_POWER_ENTITY_ID)
    assert power.state not in (STATE_UNAVAILABLE, None)

    # And they come back on the poll that answers again.
    coordinator.api._unit.fail_read(IN_MODEL_160, None)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.report.complete
    assert hass.states.get(TEST_INVERTER_SENSOR_DC_ENTITY_ID).state != STATE_UNAVAILABLE


async def test_an_accumulator_outlives_its_models_failure(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """Lifetime energy holds its last reading; the instantaneous points do not.

    Both points sit in model 103, so the same failed poll reaches both. Dropping
    the total would tear a hole in long term statistics and the energy dashboard
    every time the inverter went quiet. The device refuses the block rather than
    timing out on it: a refusal is an answer, so the poll carries on either way.
    """
    config_entry = await setup_mock_sunspec_config_entry(hass)
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    energy_before = hass.states.get(TEST_INVERTER_SENSOR_ENERGY_ENTITY_ID)
    assert energy_before.attributes["state_class"] == SensorStateClass.TOTAL_INCREASING

    coordinator.api._unit.fail_read(IN_MODEL_103, ServerDeviceBusyError())
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert set(coordinator.report.failed) == {"103:0"}
    assert (
        hass.states.get(TEST_INVERTER_SENSOR_POWER_ENTITY_ID).state == STATE_UNAVAILABLE
    )
    energy = hass.states.get(TEST_INVERTER_SENSOR_ENERGY_ENTITY_ID)
    assert energy.state == energy_before.state != STATE_UNAVAILABLE


async def test_a_device_answering_nothing_fails_with_a_reason(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """The failure names an error, since that is all the user gets to see.

    Home Assistant logs the message at error level and keeps the traceback for
    debug, so "no model answered" on its own would say nothing about why.
    """
    config_entry = await setup_mock_sunspec_config_entry(hass)
    coordinator = hass.data[DOMAIN][config_entry.entry_id]

    for address in (IN_MODEL_103, IN_MODEL_160):
        coordinator.api._unit.fail_read(address, ServerDeviceBusyError())
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    error = coordinator.last_exception
    assert str(coordinator.report.failed["103:0"]) in str(error)
    # Every failure is kept, not just the one that got named.
    assert len(error.__cause__.exceptions) == 2


async def test_a_newly_failed_model_is_logged_once(
    hass: HomeAssistant, sunspec_client_mock, caplog
) -> None:
    """A model that keeps failing has already been reported."""
    config_entry = await setup_mock_sunspec_config_entry(hass)
    coordinator = hass.data[DOMAIN][config_entry.entry_id]

    coordinator.api._unit.fail_read(IN_MODEL_103, ServerDeviceBusyError())
    caplog.clear()
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert caplog.text.count("Failed to fetch 103:0") == 1


async def test_a_total_keeps_its_value_across_a_restart(
    hass: HomeAssistant, sunspec_client_mock, zero_energy_reading
) -> None:
    """A counter picks up where it left off rather than starting over.

    An inverter that is asleep reports no energy at all, so without the restored
    value the counter would come back from a restart empty and long term
    statistics would treat the next reading as a fresh start.
    """
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(TEST_INVERTER_SENSOR_ENERGY_ENTITY_ID, "1234"),
                {"native_value": 1234, "native_unit_of_measurement": "Wh"},
            ),
        ),
    )

    await setup_mock_sunspec_config_entry(hass)

    assert hass.states.get(TEST_INVERTER_SENSOR_ENERGY_ENTITY_ID).state == "1234"


async def test_a_busy_device_is_not_read_as_a_missing_map(hass):
    """A refusal that is not about the address leaves the scan undecided.

    Answering "no SunSpec device found" to a device that was merely busy would
    send the config flow and every later scan down the wrong path.
    """
    with patch_sunspec_device(error=ServerDeviceBusyError()):
        api = SunSpecApiClient(host="test", port=123, unit_id=1)
        with pytest.raises(ServerDeviceBusyError):
            await api.async_get_models()
        await api.async_close()


async def test_an_unserved_address_moves_on_to_the_next(hass):
    """A device that refuses the address is simply not mapped there."""
    registers = {
        address - BASE_ADDRESS: word
        for address, word in sunspec_holding_registers(TEST_DEVICE).items()
    }
    with patch_sunspec_device(registers=registers) as connections:
        api = SunSpecApiClient(host="test", port=123, unit_id=1)
        connections[0].for_unit(1).fail_read(
            BASE_ADDRESS, IllegalDataAddressError(), register_type="holding"
        )
        # 40000 is refused, so the scan settles on the map at 0.
        assert await api.async_get_models()
        await api.async_close()


async def test_a_dead_device_keeps_only_its_accumulators(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """Containment is per model; a device answering nothing is still a failure.

    Every instantaneous reading goes unavailable with the link. The totals do
    not: an inverter that powers down for the night would otherwise gap its own
    statistics until morning, which is the whole reason they are exempt.
    """
    config_entry = await setup_mock_sunspec_config_entry(hass)
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    energy_before = hass.states.get(TEST_INVERTER_SENSOR_ENERGY_ENTITY_ID).state

    coordinator.api._unit.fail_requests(ModbusConnectionError("link down"))
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    for entity_id in (
        TEST_INVERTER_SENSOR_POWER_ENTITY_ID,
        TEST_INVERTER_SENSOR_DC_ENTITY_ID,
    ):
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    energy = hass.states.get(TEST_INVERTER_SENSOR_ENERGY_ENTITY_ID)
    assert energy.state == energy_before != STATE_UNAVAILABLE
