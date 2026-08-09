"""Build modbus-connection components from SunSpec model definitions.

SunSpec devices advertise which models they implement, so this integration has no
register map of its own: every point it exposes is described by the official model
definitions that ship with pysunspec2. modbus-connection's model framework is
declarative and author-time, so this module is the bridge - it compiles a model
definition plus the model's discovered address and length into a
``SunSpecComponent`` subclass at runtime.
"""

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from functools import lru_cache
import json
import logging
from pathlib import Path
import re
from typing import Any

from modbus_connection.model import Component
from modbus_connection.model import repeating_group
from modbus_connection.model.sunspec import SunSpecComponent
from modbus_connection.model.sunspec import acc16
from modbus_connection.model.sunspec import acc32
from modbus_connection.model.sunspec import acc64
from modbus_connection.model.sunspec import bitfield16
from modbus_connection.model.sunspec import bitfield32
from modbus_connection.model.sunspec import bitfield64
from modbus_connection.model.sunspec import enum16
from modbus_connection.model.sunspec import enum32
from modbus_connection.model.sunspec import eui48
from modbus_connection.model.sunspec import float32
from modbus_connection.model.sunspec import float64
from modbus_connection.model.sunspec import int16
from modbus_connection.model.sunspec import int32
from modbus_connection.model.sunspec import int64
from modbus_connection.model.sunspec import ipaddr
from modbus_connection.model.sunspec import ipv6addr
from modbus_connection.model.sunspec import string
from modbus_connection.model.sunspec import sunssf
from modbus_connection.model.sunspec import uint16
from modbus_connection.model.sunspec import uint32
from modbus_connection.model.sunspec import uint64
import sunspec2

_LOGGER: logging.Logger = logging.getLogger(__package__)

MODEL_DEFS_PATH = Path(sunspec2.__file__).parent / "models" / "json"

# The two header registers every model starts with; points are declared after them.
HEADER_LENGTH = 2

# Point types carrying an engineering value that a scale factor may scale.
_SCALED = {
    "int16": int16,
    "uint16": uint16,
    "count": uint16,
    "int32": int32,
    "uint32": uint32,
    "int64": int64,
    "uint64": uint64,
    "acc16": acc16,
    "acc32": acc32,
    "acc64": acc64,
}
_CODED = {
    "enum16": enum16,
    "enum32": enum32,
    "bitfield16": bitfield16,
    "bitfield32": bitfield32,
    "bitfield64": bitfield64,
}
_FLOATS = {"float32": float32, "float64": float64}
_ADDRESSES = {"ipaddr": ipaddr, "ipv6addr": ipv6addr, "eui48": eui48}


class SunSpecModelError(Exception):
    """A model cannot be expressed as a modbus-connection component."""


@lru_cache(maxsize=None)
def get_model_def(model_id: int) -> dict[str, Any] | None:
    """Return the SunSpec definition of a model, or None if it is unknown."""
    path = MODEL_DEFS_PATH / f"model_{model_id}.json"
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)["group"]
    except (OSError, ValueError, KeyError):
        _LOGGER.debug("No SunSpec definition for model %s", model_id)
        return None


def _attr_name(prefix: str, name: str) -> str:
    """Return a component attribute name that cannot shadow the base class.

    Point and group names come from the model definitions, so they are neither
    guaranteed to be identifiers nor guaranteed not to collide with something
    ``Component`` already defines. Values are looked up by SunSpec name through
    the layout, so the attribute name only has to be unique and legal.
    """
    return prefix + re.sub(r"[^0-9A-Za-z_]", "_", name)


@dataclass(frozen=True)
class PointLayout:
    """One point of a model or of one of its groups."""

    name: str
    pdef: dict[str, Any]
    offset: int
    """Register offset from the model header, for instance 0 of its group."""
    attr: str
    raw_attr: str | None = None
    """Attribute of the unscaled twin, for a point read through a scale factor.

    modbus-connection decodes a point whose scale-factor register is
    unimplemented to ``None``, which is indistinguishable from the point itself
    being unimplemented - and SunSpec devices do implement points while leaving
    their scale factor unimplemented. The twin reads the same registers (so it
    costs no extra request) and gives the unscaled value back.
    """

    @property
    def size(self) -> int:
        return int(self.pdef["size"])

    @property
    def type(self) -> str:
        return str(self.pdef["type"])


@dataclass
class GroupLayout:
    """A repeated (or single) sub-block of a model."""

    name: str
    gdef: dict[str, Any]
    attr: str
    stride: int
    """Registers one instance occupies."""
    count: int | None
    """Instances, or None when the count is read from a register each poll."""
    count_offset: int | None
    """Register offset of the count point, when the count is read each poll."""
    points: list[PointLayout] = dataclass_field(default_factory=list)
    scale_in_block: bool = False

    def offset_end(self, count: int) -> int:
        """The register offset just past the last instance's last point."""
        last = max((p.offset + p.size for p in self.points), default=0)
        return last + self.stride * (count - 1)


@dataclass
class ModelLayout:
    """A SunSpec model laid out over the registers of a discovered instance."""

    model_id: int
    gdef: dict[str, Any]
    points: list[PointLayout] = dataclass_field(default_factory=list)
    groups: list[GroupLayout] = dataclass_field(default_factory=list)

    def point(self, name: str) -> PointLayout | None:
        for point in self.points:
            if point.name == name:
                return point
        return None

    def group(self, name: str) -> GroupLayout | None:
        for group in self.groups:
            if group.name == name:
                return group
        return None


def _make_field(point: PointLayout, sf_offset: int | None, sf_fixed: int | None) -> Any:
    """Return the modbus-connection field for a point, at its model offset."""
    point_type = point.type
    units = point.pdef.get("units")
    scale = 10.0**sf_fixed if sf_fixed is not None else 1.0

    if point_type == "string":
        return string(point.offset, point.size)
    if point_type == "sunssf":
        return sunssf(point.offset)
    if point_type in _SCALED:
        return _SCALED[point_type](
            point.offset, scale=scale, scale_register=sf_offset, unit=units
        )
    if point_type in _CODED:
        # Symbols are resolved by the sensor platform from the point definition,
        # so the raw integer is what this layer wants.
        return _CODED[point_type](point.offset)
    if point_type in _FLOATS:
        return _FLOATS[point_type](point.offset, unit=units)
    if point_type in _ADDRESSES:
        return _ADDRESSES[point_type](point.offset)
    raise SunSpecModelError(f"unsupported point type {point_type!r}")


def _points(
    raw_points: list[dict[str, Any]], start: int, prefix: str
) -> list[PointLayout]:
    """Lay out a group's own points, in declaration order, from ``start``."""
    layouts = []
    offset = start
    for pdef in raw_points:
        if pdef["type"] != "pad":  # padding reserves registers but has no value
            attr = _attr_name(prefix, pdef["name"])
            layouts.append(
                PointLayout(
                    name=pdef["name"],
                    pdef=pdef,
                    offset=offset,
                    attr=attr,
                    # a named scale factor is a register, so it can be unimplemented
                    raw_attr=f"{attr}_raw" if isinstance(pdef.get("sf"), str) else None,
                )
            )
        offset += int(pdef["size"])
    return layouts


def model_point_offset(model_def: dict[str, Any], name: str) -> int | None:
    """Return a model point's register offset from the model header."""
    point = next(
        (p for p in _points(model_def.get("points", []), 0, "") if p.name == name), None
    )
    return point.offset if point is not None else None


def _points_length(raw_group: dict[str, Any]) -> int:
    """Registers the group's own points occupy."""
    return sum(int(p["size"]) for p in raw_group.get("points", []))


def _group_length(raw_group: dict[str, Any], counts: dict[str, int]) -> int:
    """Registers one instance of a group occupies, nested groups included.

    ``counts`` holds the values of count points already read from the device.
    Raises ``SunSpecModelError`` when a nested count is still unknown.
    """
    total = _points_length(raw_group)
    for sub in raw_group.get("groups", []):
        raw_count = sub.get("count")
        if raw_count is None:
            count = 1
        elif isinstance(raw_count, int):
            # A length-derived count only has a meaning against the model header,
            # which a nested group is not sized by.
            raise SunSpecModelError(
                f"nested group {sub['name']!r} has a length-derived count"
            )
        elif raw_count in counts:
            count = counts[raw_count]
        else:
            raise SunSpecModelError(f"count point {raw_count!r} not read yet")
        total += _group_length(sub, counts) * count
    return total


def nested_count_points(model_def: dict[str, Any]) -> set[str]:
    """Return the count points that must be read before the layout can be built.

    A count point is always a point of the model itself. modbus-connection reads a
    ``repeating_group``'s count relative to the instance it belongs to, so a count
    naming a model point can only be read per poll for a group directly on the
    model. Deeper groups - and any group whose own length depends on one - need
    their count resolved up front instead.
    """
    needed: set[str] = set()

    def walk(raw_group: dict[str, Any], depth: int) -> None:
        for sub in raw_group.get("groups", []):
            raw_count = sub.get("count")
            if depth > 0 and isinstance(raw_count, str):
                needed.add(raw_count)
            walk(sub, depth + 1)

    walk(model_def, 0)
    return needed


def build_layout(
    model_id: int,
    model_def: dict[str, Any],
    model_length: int,
    counts: dict[str, int],
) -> ModelLayout:
    """Lay a model definition out over a discovered model's registers.

    ``model_length`` is the length from the model header - the data registers,
    excluding the header itself. ``counts`` holds the values of the count points
    returned by ``nested_count_points``.

    Raises ``SunSpecModelError`` if the definition cannot be laid out.
    """
    layout = ModelLayout(model_id=model_id, gdef=model_def)
    layout.points = _points(model_def.get("points", []), 0, "p_")

    fixed_length = _points_length(model_def) - HEADER_LENGTH
    offset = _points_length(model_def)
    for raw_group in model_def.get("groups", []):
        if layout.groups and layout.groups[-1].count is None:
            # Where the next group starts depends on how many instances the
            # previous one turns out to have, which is only known per poll.
            raise SunSpecModelError(
                f"group {raw_group['name']!r} follows a runtime-counted group"
            )
        stride = _group_length(raw_group, counts)
        if stride <= 0:
            raise SunSpecModelError(f"group {raw_group['name']!r} is empty")
        group = _build_group(
            raw_group, layout, offset, stride, fixed_length, model_length, counts
        )
        layout.groups.append(group)
        offset += stride * (group.count or 0)

    _trim_to_model_length(layout, model_length)
    return layout


def _build_group(
    raw_group: dict[str, Any],
    layout: ModelLayout,
    offset: int,
    stride: int,
    fixed_length: int,
    model_length: int,
    counts: dict[str, int],
) -> GroupLayout:
    """Resolve one top-level group's count and lay its points out."""
    name = raw_group["name"]
    raw_count = raw_group.get("count")
    count: int | None
    count_offset: int | None = None

    if raw_count is None:
        count = 1
    elif isinstance(raw_count, int) and raw_count > 0:
        count = raw_count
    elif isinstance(raw_count, int):
        # count 0: the model header says how many instances follow.
        repeating = model_length - fixed_length
        if repeating < 0 or repeating % stride:
            raise SunSpecModelError(
                f"model {layout.model_id} length {model_length} does not fit "
                f"{stride}-register instances of {name!r}"
            )
        count = repeating // stride
    elif raw_count in counts:
        count = counts[raw_count]
    else:
        count_point = layout.point(raw_count)
        if count_point is None:
            raise SunSpecModelError(f"count point {raw_count!r} is not a model point")
        count = None
        count_offset = count_point.offset

    points = _points(raw_group.get("points", []), offset, f"g_{name}_")
    own = {point.name for point in points}
    scale_in_block = any(point.pdef.get("sf") in own for point in points)
    return GroupLayout(
        name=name,
        gdef=raw_group,
        attr=_attr_name("g_", name),
        stride=stride,
        count=count,
        count_offset=count_offset,
        points=points,
        scale_in_block=scale_in_block,
    )


def _trim_to_model_length(layout: ModelLayout, model_length: int) -> None:
    """Drop points the device's model is too short to hold.

    A device may implement a shorter revision of a model than the definition
    describes; reading past its length would read into the next model.
    """
    end = HEADER_LENGTH + model_length
    layout.points = [p for p in layout.points if p.offset + p.size <= end]
    for group in layout.groups:
        if group.count is not None:
            last = group.offset_end(group.count)
            if last > end:
                raise SunSpecModelError(
                    f"group {group.name!r} does not fit model length {model_length}"
                )


def _scale_offsets(
    point: PointLayout, layout: ModelLayout, group: GroupLayout | None
) -> tuple[int | None, int | None]:
    """Return the (scale register offset, fixed scale exponent) of a point.

    Raises ``SunSpecModelError`` when the scale factor cannot be addressed.
    """
    sf = point.pdef.get("sf")
    if sf is None:
        return None, None
    if isinstance(sf, int):
        return None, sf

    if group is not None:
        for candidate in group.points:
            if candidate.name == sf:
                return candidate.offset, None
        if group.scale_in_block:
            # scale_in_block is per sub-component and moves every scale register
            # with its instance, so a block carrying its own scale factors cannot
            # also reference one in the model's shared fixed block.
            raise SunSpecModelError(
                f"{sf!r} is in the model's fixed block, but {group.name!r} "
                f"carries its own scale factors"
            )

    model_point = layout.point(sf)
    if model_point is None:
        raise SunSpecModelError(f"scale factor {sf!r} is not a point of the model")
    return model_point.offset, None


def _class_namespace(
    points: list[PointLayout], layout: ModelLayout, group: GroupLayout | None
) -> dict[str, Any]:
    """Build the field attributes for a component class, skipping bad points."""
    namespace: dict[str, Any] = {}
    for point in points:
        try:
            sf_offset, sf_fixed = _scale_offsets(point, layout, group)
            namespace[point.attr] = _make_field(point, sf_offset, sf_fixed)
            if point.raw_attr is not None:
                namespace[point.raw_attr] = _make_field(point, None, None)
        except SunSpecModelError as err:
            _LOGGER.warning(
                "Skipping SunSpec point %s of model %s: %s",
                point.name,
                layout.model_id,
                err,
            )
    return namespace


def build_component_class(layout: ModelLayout) -> type[SunSpecComponent]:
    """Compile a laid-out model into a ``SunSpecComponent`` subclass."""
    namespace = _class_namespace(layout.points, layout, None)
    for group in layout.groups:
        sub_namespace = _class_namespace(group.points, layout, group)
        sub_namespace["scale_in_block"] = group.scale_in_block
        sub_class = type(
            f"SunSpecModel{layout.model_id}_{group.attr}",
            (Component,),
            sub_namespace,
        )
        count: Any = group.count
        if count is None:
            assert group.count_offset is not None
            count = uint16(group.count_offset)
        namespace[group.attr] = repeating_group(count, sub_class, stride=group.stride)
    return type(f"SunSpecModel{layout.model_id}", (SunSpecComponent,), namespace)
