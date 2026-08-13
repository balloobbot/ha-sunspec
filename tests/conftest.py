"""Global fixtures for SunSpec integration."""

from contextlib import contextmanager
import logging
from typing import Any
from unittest.mock import Mock
from unittest.mock import PropertyMock
from unittest.mock import patch

from modbus_connection import ModbusConnectionError
from modbus_connection import ModbusTimeoutError
from modbus_connection.mock import MockModbusConnection
import pytest
import sunspec2.file.client as file_client

from custom_components.sunspec.api import SunSpecModelWrapper

pytest_plugins = "pytest_homeassistant_custom_component"
_LOGGER: logging.Logger = logging.getLogger(__package__)

TEST_DEVICE = "./tests/test_data/inverter.json"
BASE_ADDRESS = 40000
SUNS_MARKER = [0x5375, 0x6E53]
END_MODEL = [0xFFFF, 0]


def sunspec_holding_registers(path: str, base_address: int = BASE_ADDRESS) -> dict:
    """Render a pysunspec2 device dump as the register map a device would serve.

    The integration talks Modbus now, so the fixtures hand it registers rather
    than a stand-in client: the marker, every model's own image back to back, and
    the end marker that terminates the chain.
    """
    device = file_client.FileClientDevice(path)
    device.scan()
    words = list(SUNS_MARKER)
    for model in device.model_list:
        image = model.get_mb()
        words.extend(
            int.from_bytes(image[i : i + 2], "big") for i in range(0, len(image), 2)
        )
    words.extend(END_MODEL)
    return {base_address + offset: word for offset, word in enumerate(words)}


@contextmanager
def patch_sunspec_device(registers=None, error=None, unit_id=1):
    """Serve ``registers`` to every client the integration builds.

    ``error`` makes the device answer nothing, as one that is powered down or
    behind a dead gateway would.
    """
    if registers is None:
        registers = sunspec_holding_registers(TEST_DEVICE)
    connections = []

    class MockSunSpecConnection(MockModbusConnection):
        """A ``ModbusConnection`` serving the fixture's registers in memory."""

        def __init__(self, params, **kwargs):
            super().__init__()
            unit = self.for_unit(unit_id)
            unit.holding.update(registers)
            if error is not None:
                unit.fail_requests(error)
            connections.append(self)

    with patch("custom_components.sunspec.api.ModbusConnection", MockSunSpecConnection):
        yield connections


# This fixture is used to prevent HomeAssistant from attempting to create and dismiss persistent
# notifications. These calls would fail without this fixture since the persistent_notification
# integration is never loaded during a test.
@pytest.fixture(name="skip_notifications", autouse=True)
def skip_notifications_fixture():
    """Skip notification calls."""
    with patch("homeassistant.components.persistent_notification.async_create"), patch(
        "homeassistant.components.persistent_notification.async_dismiss"
    ):
        yield


@pytest.fixture(name="auto_enable_custom_integrations", autouse=True)
def auto_enable_custom_integrations(
    hass: Any, enable_custom_integrations: Any  # noqa: F811
) -> None:
    """Enable custom integrations defined in the test dir."""


# This fixture, when used, will result in calls to async_get_data to return None. To have the call
# return a value, we would add the `return_value=<VALUE_TO_RETURN>` parameter to the patch call.
@pytest.fixture(name="bypass_get_device_info")
def bypass_get_device_info_fixture():
    """Skip calls to get data from API."""
    with patch("custom_components.sunspec.SunSpecApiClient.async_get_device_info"):
        yield


@pytest.fixture
def sunspec_client_mock():
    """Serve the test device's registers to the integration."""
    with patch_sunspec_device() as connections:
        yield connections


# In this fixture, we are forcing calls to async_get_data to raise an Exception. This is useful
# for exception handling.
@pytest.fixture
def sunspec_client_mock_connect_error():
    """Simulate connection error when retrieving data from API."""
    with patch_sunspec_device(error=ModbusConnectionError("no route to host")):
        yield


@pytest.fixture(name="error_on_get_device_info")
def error_get_device_info_fixture():
    """Simulate error when retrieving data from API."""
    with patch(
        "custom_components.sunspec.SunSpecApiClient.async_get_device_info",
        side_effect=Exception,
    ):
        yield


@pytest.fixture(name="timeout_on_get_device_info")
def timeout_get_device_info_fixture():
    """Simulate timeout when retrieving data from API."""
    with patch(
        "custom_components.sunspec.SunSpecApiClient.async_get_device_info",
        side_effect=ModbusTimeoutError,
    ):
        yield


@pytest.fixture(name="device_info_without_serial")
def device_info_without_serial_fixture():
    """Return device info without an SN point."""
    device_info = Mock()

    def get_value(point_name, model_index=0):
        if point_name == "SN":
            raise KeyError(point_name)
        return None

    device_info.getValue.side_effect = get_value
    type(device_info).num_models = PropertyMock(return_value=1)
    yield device_info


# In this fixture, we are forcing calls to async_read to raise an Exception. This is useful
# for exception handling.
@pytest.fixture
def error_on_get_data():
    """Simulate error when retrieving data from API."""
    with patch_sunspec_device(), patch(
        "custom_components.sunspec.SunSpecApiClient.async_read",
        side_effect=Exception,
    ):
        yield


@pytest.fixture
def timeout_error_on_get_data():
    """Simulate timeout error when retrieving data from API."""
    with patch_sunspec_device(), patch(
        "custom_components.sunspec.SunSpecApiClient.async_read",
        side_effect=ModbusTimeoutError,
    ):
        yield


@pytest.fixture
def connect_error_on_get_data():
    """Simulate connection error when retrieving data from API."""
    with patch_sunspec_device(), patch(
        "custom_components.sunspec.SunSpecApiClient.async_read",
        side_effect=ModbusConnectionError,
    ):
        yield


@pytest.fixture
def zero_energy_reading():
    """Simulate a device that answers 0 for its lifetime energy counter."""
    real_get_value = SunSpecModelWrapper.getValue

    def my_side_effect(self, point_name, model_index=0):
        if point_name == "WH":
            return 0
        return real_get_value(self, point_name, model_index)

    with patch.object(
        SunSpecModelWrapper, "getValue", autospec=True, side_effect=my_side_effect
    ):
        yield


@pytest.fixture
def overflow_error_dca():
    """Simulate overflow error for getValue from API."""

    def my_side_effect(*args, **kwargs):
        if args[0] == "DCA":
            raise OverflowError()
        return 1

    with patch(
        "custom_components.sunspec.api.SunSpecModelWrapper.getValue",
        side_effect=my_side_effect,
    ):
        yield
