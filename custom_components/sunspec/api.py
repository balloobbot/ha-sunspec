"""SunSpec Modbus client built on modbus-connection."""

from dataclasses import dataclass
import logging
from typing import Any

from modbus_connection import ModbusConnectionError
from modbus_connection import ModbusError
from modbus_connection import ModbusExceptionError
from modbus_connection import ModbusTcpParams
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
    "UpdateReport",
]


@dataclass(frozen=True)
class UpdateReport:
    """What one poll refreshed, by ``model_id:model_index``.

    A model instance that failed kept its previous values and is named here with
    the error that failed it. A model whose components could not be built at all
    is named by its bare id instead, since it has no instances yet. A dead link
    is never in here - the read raises ``ModbusConnectionError`` instead of
    reporting partial silence.
    """

    updated: set[str]
    failed: dict[str, ModbusError]

    @property
    def complete(self) -> bool:
        """Whether every model asked for refreshed."""
        return not self.failed


def poll_key(model_id: int, model_index: int) -> str:
    """The report key for one model instance."""
    return f"{model_id}:{model_index}"


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


def _chain_range(models: SunSpecModels) -> tuple[int, int]:
    """The addresses the discovered model chain occupies, first to last."""
    chain = models.chain
    last = chain[-1]
    return chain[0].address, last.address + last.span - 1


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
        """Read one model, raising whatever kept it from refreshing.

        Asking for a single model leaves nothing to contain: an empty wrapper is
        no more use to the caller than the error is.
        """
        _LOGGER.debug("Get data for model %s", model_id)
        models, report = await self.async_read({model_id})
        for error in report.failed.values():
            raise error
        return models[model_id]

    async def async_read(self, model_ids: set) -> tuple[dict, UpdateReport]:
        """Read every requested model the device has, one component at a time.

        Each model instance is read on its own, so a block the device is too slow
        to answer costs that model its refresh and nothing else: its points keep
        the values the last poll decoded, and the report names it with the error.
        Listeners fire only once every instance has been tried. A failure of the
        link itself raises ``ModbusConnectionError`` rather than reporting.
        """
        updated: set[str] = set()
        failed: dict[str, ModbusError] = {}
        present = []
        for model_id in sorted(model_ids):
            try:
                built = await self._async_build(model_id)
            except ModbusConnectionError:
                raise
            except ModbusError as err:
                # Building reads a nested group's count points. Contain that too,
                # or a model whose counts never answer blanks the whole device on
                # every poll instead of just itself.
                _LOGGER.debug("Could not build model %s: %s", model_id, err)
                failed[str(model_id)] = err
                continue
            if built:
                present.append(model_id)

        fresh: list[SunSpecComponent] = []
        for model_id in present:
            for model_index, component in enumerate(self._components[model_id]):
                try:
                    await component.async_update(notify=False)
                except ModbusConnectionError:
                    raise
                except ModbusError as err:
                    failed[poll_key(model_id, model_index)] = err
                else:
                    updated.add(poll_key(model_id, model_index))
                    fresh.append(component)
        for component in fresh:
            component.notify()

        models = {model_id: self._wrappers[model_id] for model_id in present}
        return models, UpdateReport(updated, failed)

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
        models = await self._async_scan()
        found = models.get(model_id)
        if not found:
            return False
        model_def = get_model_def(model_id)
        if model_def is None:
            _LOGGER.info("Skipping model %s: no SunSpec definition", model_id)
            return False

        chain_low, chain_high = _chain_range(models)
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
            # A SunSpec chain is one contiguous run of registers - every model's
            # header says where the next one starts - and the scan walked it end
            # to end, so the device answers every address in it. Saying so lets a
            # block read bridge whatever a model leaves unread between its points
            # instead of splitting there; without it the planner may only join
            # addresses this model claims by itself. Readable ranges are declared
            # in the component's own coordinates, which the model's address shifts.
            component.register_ranges = (
                (chain_low - model.address, chain_high - model.address),
            )
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
