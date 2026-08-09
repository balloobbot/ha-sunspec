"""Tests for the SunSpec definition to component compiler."""

import json

import pytest

from custom_components.sunspec.model import MODEL_DEFS_PATH
from custom_components.sunspec.model import SunSpecModelError
from custom_components.sunspec.model import _group_length
from custom_components.sunspec.model import _points_length
from custom_components.sunspec.model import build_component_class
from custom_components.sunspec.model import build_layout
from custom_components.sunspec.model import get_model_def
from custom_components.sunspec.model import model_point_offset
from custom_components.sunspec.model import nested_count_points

HEADER = [
    {"name": "ID", "type": "uint16", "size": 1},
    {"name": "L", "type": "uint16", "size": 1},
]


def a_model(points=(), groups=(), name="test"):
    """Build a minimal model definition around ``points`` and ``groups``."""
    return {
        "name": name,
        "label": "Test",
        "type": "group",
        "points": [*HEADER, *points],
        "groups": list(groups),
    }


def model_length(model_def, counts, instances=2):
    """The header length a device would report for this definition."""
    length = _points_length(model_def) - 2
    for group in model_def.get("groups", []):
        repeats = instances if group.get("count") == 0 else 1
        length += repeats * _group_length(group, counts)
    return length


def test_every_shipped_definition_compiles():
    """Every SunSpec model definition maps onto a component.

    The integration has no register map of its own, so any model a device
    advertises has to compile. This is the whole catalogue pysunspec2 ships.
    """
    definitions = sorted(MODEL_DEFS_PATH.glob("model_*.json"))
    assert len(definitions) > 100

    for path in definitions:
        model_id = int(path.stem.split("_")[1])
        model_def = json.loads(path.read_text())["group"]
        # A count read from the device: two of whatever repeats.
        counts = {name: 2 for name in nested_count_points(model_def)}
        layout = build_layout(
            model_id, model_def, model_length(model_def, counts), counts
        )
        build_component_class(layout)


def test_mixed_scale_factor_block_drops_only_the_odd_point(caplog):
    """A block carrying its own scale factors cannot borrow the model's.

    modbus-connection moves every scale register with the instance once a
    sub-component sets ``scale_in_block``, so a point in such a block that
    references a factor in the model's shared fixed block has no expressible
    address. Model 63001 is the one definition where that happens.
    """
    model_def = get_model_def(63001)
    counts = {name: 2 for name in nested_count_points(model_def)}
    layout = build_layout(63001, model_def, model_length(model_def, counts), counts)
    component_class = build_component_class(layout)
    block = vars(vars(component_class)["g_repeating"].component_class)

    assert layout.group("repeating").scale_in_block
    # The point referencing the model-level factor is gone...
    assert "g_repeating_int32" not in block
    assert "'sunssf_1' is in the model's fixed block" in caplog.text
    # ...and the ones referencing the block's own factors are kept.
    assert "g_repeating_int16_11" in block


def test_length_derived_count():
    """A repeating block is sized from the length in the model header."""
    group = {
        "name": "block",
        "type": "group",
        "count": 0,
        "points": [{"name": "V", "type": "uint16", "size": 1, "units": "V"}],
    }
    model_def = a_model(
        points=[{"name": "N", "type": "uint16", "size": 1}], groups=[group]
    )

    layout = build_layout(1, model_def, 4, {})  # N plus three one-register blocks
    assert layout.group("block").count == 3
    assert layout.group("block").count_offset is None  # folded into the one read


def test_length_that_does_not_divide_is_rejected():
    """A length that leaves a partial instance is a map this cannot express."""
    group = {
        "name": "block",
        "type": "group",
        "count": 0,
        "points": [
            {"name": "V", "type": "uint16", "size": 1},
            {"name": "A", "type": "uint16", "size": 1},
        ],
    }
    model_def = a_model(groups=[group])
    with pytest.raises(SunSpecModelError, match="does not fit"):
        build_layout(1, model_def, 5, {})


def test_group_after_a_runtime_counted_group_is_rejected():
    """Where a group starts cannot depend on a count read at poll time."""
    counted = {
        "name": "first",
        "type": "group",
        "count": "N",
        "points": [{"name": "V", "type": "uint16", "size": 1}],
    }
    following = {
        "name": "second",
        "type": "group",
        "points": [{"name": "W", "type": "uint16", "size": 1}],
    }
    model_def = a_model(
        points=[{"name": "N", "type": "uint16", "size": 1}],
        groups=[counted, following],
    )
    with pytest.raises(SunSpecModelError, match="follows a runtime-counted group"):
        build_layout(1, model_def, 10, {})


def test_unknown_count_point_is_rejected():
    """A group counted by a point the model does not have cannot be placed."""
    group = {
        "name": "block",
        "type": "group",
        "count": "Nope",
        "points": [{"name": "V", "type": "uint16", "size": 1}],
    }
    model_def = a_model(groups=[group])
    with pytest.raises(SunSpecModelError, match="not a model point"):
        build_layout(1, model_def, 4, {})


def test_unsupported_point_type_is_dropped(caplog):
    """A point type with no field type is skipped, not fatal."""
    model_def = a_model(points=[{"name": "X", "type": "nonsense", "size": 1}])
    layout = build_layout(1, model_def, 1, {})
    assert "p_X" not in vars(build_component_class(layout))
    assert "unsupported point type" in caplog.text


def test_points_past_the_model_length_are_dropped():
    """A device serving a shorter model revision is not read past its end."""
    model_def = a_model(
        points=[
            {"name": "A", "type": "uint16", "size": 1},
            {"name": "B", "type": "uint16", "size": 1},
        ]
    )
    layout = build_layout(1, model_def, 1, {})
    assert [point.name for point in layout.points] == ["ID", "L", "A"]


def test_padding_reserves_registers_without_a_point():
    """A pad point takes its registers but is not readable."""
    model_def = a_model(
        points=[
            {"name": "Pad", "type": "pad", "size": 1},
            {"name": "A", "type": "uint16", "size": 1},
        ]
    )
    layout = build_layout(1, model_def, 2, {})
    assert [point.name for point in layout.points] == ["ID", "L", "A"]
    assert layout.point("A").offset == 3


def test_nested_count_points_are_reported():
    """Counts a nested group needs are named so they can be read up front."""
    assert nested_count_points(get_model_def(705)) == {"NPt"}
    assert nested_count_points(get_model_def(103)) == set()


def test_model_point_offset():
    """A model point's offset is measured from the model header."""
    assert model_point_offset(get_model_def(103), "ID") == 0
    assert model_point_offset(get_model_def(103), "A") == 2
    assert model_point_offset(get_model_def(103), "Nope") is None


def test_unknown_model_has_no_definition():
    """A vendor model nobody published is simply not modelled."""
    assert get_model_def(64999) is None
