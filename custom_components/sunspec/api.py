"""SunSpec Modbus client built on modbus-connection."""

from dataclasses import dataclass
import logging
from typing import Any

from modbus_connection import ModbusError
from modbus_connection import ModbusExceptionError
from modbus_connection import ModbusTcpParams
from modbus_connection.model import ComponentGroup
from modbus_connection.model.sunspec import SunSpecComponent
from modbus_connection.model.sunspec import SunSpecError
from modbus_connection.model.sunspec import SunSpecMapShiftError
from modbus_connection.model.sunspec import SunSpecModel
from modbus_connection.model.sunspec import SunSpecModels
from modbus_connection.model.sunspec import scan
from modbus_connection.tmodbus import ModbusConnection

from .model import ModelLayout
from .model import PointLayout
from .model import SunSpecModelError
from .model import build_component_class
from .model import build_layout
from .model import get_model_def
from .model import model_point_offset
from .model import nested_count_points

# The old client used a 120s socket timeout; SunSpec inverters can be slow to
# answer a block read, especially just after waking up.
TIMEOUT = 120
# SunSpec devices are commonly behind a small embedded TCP stack that dislikes
# back-to-back requests. The client this replaced slept 0.6s between whole-model
# reads; a poll now issues far fewer requests, so pace them individually instead.
MESSAGE_SPACING = 0.1
# Where a device may place the "SunS" marker, in the order SunSpec suggests.
BASE_ADDRESSES = (40000, 0, 50000)

_LOGGER: logging.Logger = logging.getLogger(__package__)

# The point types the sensor platform renders as an enumeration even though they
# carry no unit. Kept exactly as the pysunspec2-based client had it.
CODED_TYPES = ("enum16", "bitfield32")

__all__ = [
    "SunSpecApiClient",
    "SunSpecMapShiftError",
    "SunSpecModelWrapper",
]


@dataclass(frozen=True)
class SunSpecPoint:
    """One decoded point, with the definition it was decoded from."""

    pdef: dict[str, Any]
    value: Any
    """The point's own value, before its scale factor is applied."""

    scaled: Any = None
    """The value with its scale factor applied, or None if there is none."""

    @property
    def cvalue(self) -> Any:
        """The computed value: scaled where the device implements the factor.

        A device may implement a point but leave its scale factor unimplemented.
        SunSpec readers take the unscaled value in that case, and so must this
        one, or every point behind a missing factor would vanish.
        """
        return self.value if self.scaled is None else self.scaled


class SunSpecModelWrapper:
    """Expose every point of one model ID, over each place it was discovered."""

    def __init__(self, instances: list[tuple[ModelLayout, SunSpecComponent]]) -> None:
        self._instances = instances
        self.num_models = len(instances)

    @property
    def _layout(self) -> ModelLayout:
        return self._instances[0][0]

    def isValidPoint(self, point_name):
        point = self.getPoint(point_name)
        if point.value is None:
            return False
        if point.pdef["type"] in CODED_TYPES:
            return True
        if point.pdef.get("units", None) is None:
            return False
        return True

    def getKeys(self):
        layout, component = self._instances[0]
        keys = list(filter(self.isValidPoint, (p.name for p in layout.points)))
        for group in layout.groups:
            for index in range(len(getattr(component, group.attr))):
                group_keys = (f"{group.name}:{index}:{p.name}" for p in group.points)
                keys.extend(filter(self.isValidPoint, group_keys))
        return keys

    def getValue(self, point_name, model_index=0):
        return self.getPoint(point_name, model_index).cvalue

    def getMeta(self, point_name):
        return self.getPoint(point_name).pdef

    def getGroupMeta(self):
        return self._layout.gdef

    def getPoint(self, point_name, model_index=0) -> SunSpecPoint:
        """Return the point at ``point_name``, raising KeyError if undefined.

        A point of a repeated group is addressed as ``group:index:point``, the
        key format the sensor platform builds its unique IDs from.
        """
        layout, component = self._instances[model_index]
        path = point_name.split(":")
        if len(path) == 1:
            point = layout.point(point_name)
            if point is None:
                raise KeyError(point_name)
            return _read_point(point, component)

        group = layout.group(path[0])
        point = _group_point(group, path[2]) if group is not None else None
        if group is None or point is None:
            raise KeyError(point_name)
        instances = getattr(component, group.attr)
        index = int(path[1])
        if index >= len(instances):
            return SunSpecPoint(point.pdef, None)
        return _read_point(point, instances[index])


def _group_point(group, name) -> PointLayout | None:
    return next((p for p in group.points if p.name == name), None)


def _read_point(point: PointLayout, component) -> SunSpecPoint:
    """Read a point's decoded value off the component holding it."""
    scaled = getattr(component, point.attr, None)
    if point.raw_attr is None:
        return SunSpecPoint(point.pdef, scaled)
    return SunSpecPoint(point.pdef, getattr(component, point.raw_attr, None), scaled)


class SunSpecApiClient:
    """Read the models a SunSpec device advertises."""

    def __init__(self, host: str, port: int, unit_id: int) -> None:
        """Set up a connection to a SunSpec device; no I/O happens here."""
        _LOGGER.debug("New SunspecApi Client for %s:%s unit %s", host, port, unit_id)
        self._host = host
        self._port = port
        self._unit_id = unit_id
        self._connection = ModbusConnection(
            ModbusTcpParams(host=host, port=port),
            timeout=TIMEOUT,
            message_spacing=MESSAGE_SPACING,
        )
        self._unit = self._connection.for_unit(unit_id)
        self._models: SunSpecModels | None = None
        self._wrappers: dict[int, SunSpecModelWrapper] = {}
        self._components: dict[int, list[SunSpecComponent]] = {}
        self._groups: dict[frozenset[int], ComponentGroup] = {}

    async def async_get_models(self, config=None) -> list:
        """Return the sorted IDs of every model the device advertises."""
        if config is not None:
            # The options flow can point the integration at another device; probe
            # that one over its own connection rather than this client's.
            other = SunSpecApiClient(
                config["host"], config["port"], config.get("unit_id", 1)
            )
            try:
                return await other.async_get_models()
            finally:
                await other.async_close()
        return sorted(await self._async_scan())

    async def async_get_device_info(self) -> SunSpecModelWrapper:
        """Read the common model, which carries the device's identity."""
        return await self.async_get_data(1)

    async def async_get_data(self, model_id) -> SunSpecModelWrapper:
        """Read one model."""
        _LOGGER.debug("Get data for model %s", model_id)
        return (await self.async_read({model_id}))[model_id]

    async def async_read(self, model_ids: set) -> dict:
        """Read every requested model the device has, in one pooled update."""
        present = {
            model_id for model_id in model_ids if await self._async_build(model_id)
        }
        if not present:
            return {}
        key = frozenset(present)
        group = self._groups.get(key)
        if group is None:
            group = self._groups[key] = ComponentGroup(
                self._unit,
                [component for m in present for component in self._components[m]],
            )
        await group.async_update()
        return {model_id: self._wrappers[model_id] for model_id in present}

    async def async_disconnect(self) -> None:
        """Drop the link; the next read establishes a new one.

        Best effort: the link is dropped either way, so a failure tearing the old
        one down is nothing a caller can act on.
        """
        try:
            await self._connection.disconnect()
        except ModbusError as err:
            _LOGGER.debug("Error disconnecting from %s: %s", self._host, err)

    async def async_close(self) -> None:
        """Close the connection permanently."""
        await self._connection.close()

    # -- discovery -------------------------------------------------------------

    async def _async_scan(self) -> SunSpecModels:
        """Scan the model chain, remembering where each model sits."""
        if self._models is not None:
            return self._models
        errors = []
        for base_address in BASE_ADDRESSES:
            try:
                self._models = await scan(self._unit, base_address)
            except (SunSpecError, ModbusExceptionError) as err:
                # Not a SunSpec map here, or the device refuses the address;
                # both mean "try the next base address".
                errors.append(f"{base_address}: {err}")
                continue
            _LOGGER.debug(
                "Found SunSpec models at base address %s: %s",
                base_address,
                sorted(self._models),
            )
            return self._models
        raise SunSpecError(
            f"No SunSpec device found at {self._host}:{self._port} "
            f"unit id {self._unit_id} ({'; '.join(errors)})"
        )

    async def _async_build(self, model_id: int) -> bool:
        """Build the components for a model ID; False if the device lacks it."""
        if model_id in self._components:
            return True
        found = (await self._async_scan()).get(model_id)
        if not found:
            return False
        model_def = get_model_def(model_id)
        if model_def is None:
            _LOGGER.info("Skipping model %s: no SunSpec definition", model_id)
            return False

        instances = []
        for model in found:
            try:
                layout = build_layout(
                    model_id,
                    model_def,
                    model.length,
                    await self._async_read_counts(model, model_def),
                )
            except SunSpecModelError as err:
                _LOGGER.warning(
                    "Skipping model %s at %s: %s", model_id, model.address, err
                )
                continue
            component = build_component_class(layout)(self._unit, model)
            instances.append((layout, component))

        if not instances:
            return False
        self._components[model_id] = [component for _, component in instances]
        self._wrappers[model_id] = SunSpecModelWrapper(instances)
        return True

    async def _async_read_counts(
        self, model: SunSpecModel, model_def: dict
    ) -> dict[str, int]:
        """Read the count points a nested group's size depends on.

        modbus-connection reads a ``repeating_group``'s count relative to the
        instance holding it, so a nested group counted by a model point has to be
        sized once here instead of on every poll.
        """
        counts = {}
        for name in nested_count_points(model_def):
            offset = model_point_offset(model_def, name)
            if offset is None:
                continue
            # Point offsets are measured from the model header, where the
            # component's own base offset sits.
            address = model.address + offset
            (value,) = await self._unit.read_holding_registers(address, 1)
            counts[name] = value
            _LOGGER.debug("Model %s count point %s = %s", model.model_id, name, value)
        return counts
