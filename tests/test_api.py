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
    models, report = await api.async_read({802})
    assert models == {}
    assert report.complete
    await api.async_close()


async def test_one_read_per_model(hass, sunspec_client_mock):
    """A poll reads each model on its own rather than pooling them together."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1)
    models, report = await api.async_read({1, 103, 160})
    assert set(models) == {1, 103, 160}
    assert report.updated == {"1:0", "103:0", "160:0"}
    assert report.complete

    unit = api._unit
    unit.read_events.clear()
    await api.async_read({1, 103, 160})
    # One block each: the three models span 67, 52 and 50 registers, all inside
    # the per-request ceiling.
    assert len(unit.read_events) == 3
    await api.async_close()


async def test_a_read_stays_inside_the_model_it_refreshes(hass, sunspec_client_mock):
    """No block spans two models, so a refused block costs only its own.

    Reads used to be pooled across the whole chain, which took 7 requests for
    this device but let one refusal discard every model's data. Splitting them
    costs one request per model instance, plus one more wherever an instance
    outgrows the per-request register ceiling: 19 for the 17 instances here.
    """
    api = SunSpecApiClient(host="test", port=123, unit_id=1)
    every = set(await api.async_get_models())
    await api.async_read(every)

    unit = api._unit
    unit.read_events.clear()
    await api.async_read(every)

    models = await api._async_scan()
    # Model spans do not overlap, so a read inside exactly one of them is also
    # inside the chain the scan walked.
    spans = [
        (model.address, model.address + model.span - 1)
        for model_id in every
        for model in models.get(model_id)
    ]
    for event in unit.read_events:
        low = event.address
        high = event.address + event.count - 1
        owners = [span for span in spans if span[0] <= low and high <= span[1]]
        assert len(owners) == 1, f"read {low}..{high} is not inside a single model"

    assert len(unit.read_events) == 19
    await api.async_close()


async def test_disconnect_between_polls(hass, sunspec_client_mock):
    """The link is dropped between polls and re-established on the next read."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1)
    await api.async_read({1})
    assert api._connection.connected

    await api.async_disconnect()
    assert not api._connection.connected

    await api.async_read({1})
    assert api._connection.connected
    await api.async_close()


async def test_get_models_for_other_device(hass, sunspec_client_mock):
    """The options flow can scan another device without touching this client."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1)
    models = await api.async_get_models({"host": "other", "port": 502, "unit_id": 1})
    assert models == ALL_MODELS
    assert not api._connection.connected
    await api.async_close()


async def test_reconnects_after_link_lost(hass, sunspec_client_mock):
    """A dropped link is re-established by the next read."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1)
    assert (await api.async_read({1}))[0]

    api._connection.simulate_connection_lost()
    assert not api._connection.connected

    assert (await api.async_read({1}))[0]
    assert api._connection.connected
    await api.async_close()


async def test_closed_client_refuses_reads(hass, sunspec_client_mock):
    """A closed client does not quietly open another connection."""
    api = SunSpecApiClient(host="test", port=123, unit_id=1)
    await api.async_read({1})
    await api.async_close()

    with pytest.raises(ClientClosedError):
        await api.async_read({103})
