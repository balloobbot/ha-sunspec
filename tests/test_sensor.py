"""Test SunSpec sensor."""

from homeassistant.components.sensor import ATTR_OPTIONS
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import ATTR_DEVICE_CLASS
from homeassistant.core import HomeAssistant

from custom_components.sunspec.const import DOMAIN
from custom_components.sunspec.sensor import ICON_DC_AMPS

from . import TEST_INVERTER_MM_SENSOR_POWER_ENTITY_ID
from . import TEST_INVERTER_MM_SENSOR_STATE_ENTITY_ID
from . import TEST_INVERTER_PREFIX_SENSOR_DC_ENTITY_ID
from . import TEST_INVERTER_SENSOR_DC_ENTITY_ID
from . import TEST_INVERTER_SENSOR_ENERGY_ENTITY_ID
from . import TEST_INVERTER_SENSOR_EVENT_ENTITY_ID
from . import TEST_INVERTER_SENSOR_POWER_ENTITY_ID
from . import TEST_INVERTER_SENSOR_STATE_ENTITY_ID
from . import TEST_INVERTER_SENSOR_VAR_ID
from . import setup_mock_sunspec_config_entry
from .const import MOCK_CONFIG_MM
from .const import MOCK_CONFIG_PREFIX


async def test_sensor_overflow_error(
    hass: HomeAssistant, sunspec_client_mock, overflow_error_dca
) -> None:
    """Verify device information includes expected details."""

    await setup_mock_sunspec_config_entry(hass)

    entity_state = hass.states.get(TEST_INVERTER_SENSOR_DC_ENTITY_ID)
    assert entity_state


async def test_sensor_dc(hass: HomeAssistant, sunspec_client_mock) -> None:
    """Verify device information includes expected details."""

    await setup_mock_sunspec_config_entry(hass)

    entity_state = hass.states.get(TEST_INVERTER_SENSOR_DC_ENTITY_ID)
    assert entity_state
    assert entity_state.attributes["icon"] == ICON_DC_AMPS


async def test_sensor_var(hass: HomeAssistant, sunspec_client_mock) -> None:
    """Verify device information includes expected details."""

    await setup_mock_sunspec_config_entry(hass)

    entity_state = hass.states.get(TEST_INVERTER_SENSOR_VAR_ID)
    assert entity_state


async def test_sensor_with_prefix(hass: HomeAssistant, sunspec_client_mock) -> None:
    """Verify device information includes expected details."""

    await setup_mock_sunspec_config_entry(hass, MOCK_CONFIG_PREFIX)

    entity_state = hass.states.get(TEST_INVERTER_PREFIX_SENSOR_DC_ENTITY_ID)
    assert entity_state


async def test_sensor_state(hass: HomeAssistant, sunspec_client_mock) -> None:
    """Verify device information includes expected details."""

    await setup_mock_sunspec_config_entry(hass)

    entity_state = hass.states.get(TEST_INVERTER_SENSOR_STATE_ENTITY_ID)
    assert entity_state
    assert entity_state.state == "MPPT"
    # An enum16 does hold one value at a time, so it stays an ENUM.
    assert entity_state.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.ENUM
    assert "MPPT" in entity_state.attributes[ATTR_OPTIONS]


async def test_sensor_bitfield_with_several_bits_set(
    hass: HomeAssistant, sunspec_client_mock
) -> None:
    """A bitfield names every flag that is set, so it is no kind of enum.

    Declared as one, the joined state was not among the options and Home
    Assistant raised out of the entity write - and out of every refresh with it.
    """

    config_entry = await setup_mock_sunspec_config_entry(hass)

    entity_state = hass.states.get(TEST_INVERTER_SENSOR_EVENT_ENTITY_ID)
    assert entity_state.attributes["raw"] == 3
    assert entity_state.state == "GROUND_FAULT,DC_OVER_VOLT"
    assert ATTR_DEVICE_CLASS not in entity_state.attributes
    assert ATTR_OPTIONS not in entity_state.attributes

    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.last_update_success


async def test_sensor_power(hass: HomeAssistant, sunspec_client_mock) -> None:
    """Verify device information includes expected details."""

    await setup_mock_sunspec_config_entry(hass)

    entity_state = hass.states.get(TEST_INVERTER_SENSOR_POWER_ENTITY_ID)
    assert entity_state
    assert entity_state.state == "800"


async def test_sensor_energy(hass: HomeAssistant, sunspec_client_mock) -> None:
    """Verify device information includes expected details."""

    await setup_mock_sunspec_config_entry(hass)

    entity_state = hass.states.get(TEST_INVERTER_SENSOR_ENERGY_ENTITY_ID)
    assert entity_state
    assert entity_state.state == "100000"


async def test_sensor_state_mm(hass: HomeAssistant, sunspec_client_mock) -> None:
    """Verify device information includes expected details."""

    await setup_mock_sunspec_config_entry(hass, MOCK_CONFIG_MM)

    entity_state = hass.states.get(TEST_INVERTER_MM_SENSOR_STATE_ENTITY_ID)
    assert entity_state
    assert entity_state.state == "OFF"


async def test_sensor_power_mm(hass: HomeAssistant, sunspec_client_mock) -> None:
    """Verify device information includes expected details."""

    await setup_mock_sunspec_config_entry(hass, MOCK_CONFIG_MM)

    entity_state = hass.states.get(TEST_INVERTER_MM_SENSOR_POWER_ENTITY_ID)
    assert entity_state
    assert entity_state.state == "9700"
