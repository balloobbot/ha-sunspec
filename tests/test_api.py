"""Tests for SunSpec api."""

from modbus_connection import ClientClosedError
from modbus_connection import ModbusConnectionError
from modbus_connection.model.sunspec import SunSpecError
import pytest

from custom_components.sunspec.api import SunSpecApiClient

from .conftest import BASE_ADDRESS
from .conftest import patch_sunspec_device
from .conftest import sunspec_holding_registers

ALL_MODELS = [
    1,
    103,
    160,
    304,
    701,
    702,
    703,
    704,
    705,
    706,
    707,
    708,
    709,
    710,
    711,
    712,
]


async def test_api(hass, sunspec_client_mock):
    """Test API calls."""

    # To test the api submodule, we first create an instance of our API client
    api = SunSpecApiClient(host="test", port=123, unit_id=1)

    models = await api.async_get_models()
    assert models == ALL_MODELS

    device_info = await api.async_get_device_info()

    assert device_info.getValue("Mn") == "SunSpecTest"
    assert device_info.getValue("SN") == "sn-123456789"

    model = await api.async_get_data(701)
    assert model.getValue("W") == 9800
    assert model.getMeta("W")["label"] == "Active Power"

    model = await api.async_get_data(705)
    keys = model.getKeys()
    assert len(keys) == 22

    await api.async_close()


async def test_repeating_groups(hass, sunspec_client_mock):
    """A model's repeating blocks are read as sub-components."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1)

    # Model 160's module count comes from the model header's length.
    mppt = await api.async_get_data(160)
    assert mppt.getValue("module:0:DCA") == 90
    assert mppt.getValue("module:1:DCA") == 92
    assert mppt.getMeta("module:0:DCA")["units"] == "A"

    # Model 705's curve count is read from the NCrv point on every poll.
    curves = await api.async_get_data(705)
    # V_SF is -2, so the raw 1 comes back as 0.01 for every curve.
    assert [curves.getValue(f"Crv:{index}:VRef") for index in range(3)] == [
        0.01,
        0.01,
        0.01,
    ]

    # A point outside the discovered instances is not a point of the model.
    with pytest.raises(KeyError):
        curves.getValue("Crv:0:Nope")
    assert curves.getValue("Crv:9:VRef") is None

    await api.async_close()


async def test_scale_factors(hass):
    """A point is scaled by the factor register it names."""
    registers = sunspec_holding_registers("./tests/test_data/inverter.json")
    # Model 160 keeps its DC scale factors in the shared fixed block; each
    # module's points reference them from inside its own repeating block.
    dca_sf = 40830 + 2  # the model header sits at 40830
    registers[dca_sf] = 0xFFFF  # -1, i.e. deci-amps

    with patch_sunspec_device(registers=registers):
        api = SunSpecApiClient(host="test", port=123, unit_id=1)
        mppt = await api.async_get_data(160)
        assert mppt.getValue("module:0:DCA") == 9.0
        assert mppt.getValue("module:1:DCA") == 9.2
        # A point whose factor is left unimplemented keeps its raw value.
        assert mppt.getValue("module:0:DCV") == 900
        await api.async_close()


async def test_no_sunspec_device(hass):
    """A device without a SunSpec marker is reported as such."""
    with patch_sunspec_device(registers={}):
        api = SunSpecApiClient(host="test", port=123, unit_id=1)
        with pytest.raises(SunSpecError):
            await api.async_get_models()
        await api.async_close()


async def test_alternate_base_address(hass):
    """The marker is looked for at every base address SunSpec allows."""
    registers = {
        address - BASE_ADDRESS: word
        for address, word in sunspec_holding_registers(
            "./tests/test_data/inverter.json"
        ).items()
    }
    with patch_sunspec_device(registers=registers):
        api = SunSpecApiClient(host="test", port=123, unit_id=1)
        assert await api.async_get_models() == ALL_MODELS
        await api.async_close()


async def test_unreachable_device(hass):
    """A device that answers nothing raises out of the read."""
    with patch_sunspec_device(error=ModbusConnectionError("unreachable")):
        api = SunSpecApiClient(host="test", port=123, unit_id=1)
        with pytest.raises(ModbusConnectionError):
            await api.async_get_models()
        await api.async_close()


async def test_missing_model(hass, sunspec_client_mock):
    """Asking for a model the device does not have yields no data."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1)
    assert await api.async_read({802}) == {}
    await api.async_close()


async def test_pooled_read(hass, sunspec_client_mock):
    """Every enabled model is read in one pooled pass."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1)
    data = await api.async_read({1, 103, 160})
    assert set(data) == {1, 103, 160}

    unit = api._unit
    unit.read_events.clear()
    await api.async_read({1, 103, 160})
    # The three models span ~900 registers, so the pooled plan is a handful of
    # block reads rather than one request per model - and far fewer than the
    # per-point reads a naive client would issue.
    assert 1 <= len(unit.read_events) <= 12
    await api.async_close()


async def test_disconnect_between_polls(hass, sunspec_client_mock):
    """The link is dropped between polls and re-established on the next read."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1)
    await api.async_read({1})
    assert api._unit.connected

    await api.async_disconnect()
    assert not api._unit.connected

    await api.async_read({1})
    assert api._unit.connected
    await api.async_close()


async def test_get_models_for_other_device(hass, sunspec_client_mock):
    """The options flow can scan another device without touching this client."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1)
    models = await api.async_get_models({"host": "other", "port": 502, "unit_id": 1})
    assert models == ALL_MODELS
    assert not api._unit.connected
    await api.async_close()


async def test_reconnects_after_link_lost(hass, sunspec_client_mock):
    """A dropped link is re-established by the next read."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1)
    assert await api.async_read({1})

    api._unit._connection.simulate_connection_lost()
    assert not api._unit.connected

    assert await api.async_read({1})
    assert api._unit.connected
    await api.async_close()


async def test_closed_client_refuses_reads(hass, sunspec_client_mock):
    """A closed client does not quietly open another connection."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1)
    await api.async_read({1})
    await api.async_close()

    with pytest.raises(ClientClosedError):
        await api.async_read({103})
