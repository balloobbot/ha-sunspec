# Migrating ha-sunspec to modbus-connection

This integration used to talk to devices through **pysunspec2**'s synchronous
Modbus client, driven from Home Assistant's executor. It now talks
**modbus-connection 4.3.0** (tmodbus backend) and keeps pysunspec2 only for the
SunSpec model definitions it ships — the JSON catalogue, never the transport.

What changed, in one paragraph: `custom_components/sunspec/model.py` compiles a
SunSpec model definition plus a discovered model's address and length into a
`SunSpecComponent` subclass at runtime; `api.py` scans the model chain with
`modbus_connection.model.sunspec.scan`, builds one component per discovered
model, and pools them all into a single `ComponentGroup`. `sensor.py` and
`entity.py` were not touched at all — `SunSpecModelWrapper` kept its
`getKeys` / `getValue` / `getMeta` / `getGroupMeta` / `getPoint().pdef` surface.

Two results worth stating up front, because they set the tone for everything
below:

- **All 106 model definitions pysunspec2 ships compile into components**, with
  exactly one point dropped in the entire catalogue (see gap 2).
- Against the test device (models 1, 103, 160, 304, 701×2, 702–712), the new
  code produces **byte-identical keys and values** to the pysunspec2
  implementation — every model, every point, every repeated instance. A poll of
  the default-enabled models went from a `sleep(0.6)` plus a full-model read per
  model to **4 pooled block reads** paced 0.1 s apart.

---

## 1. What weird things does this library do?

**It has no register map.** Every other Modbus integration in this space
hard-codes addresses. ha-sunspec hard-codes nothing: it walks the SunSpec model
chain, looks each discovered model ID up in pysunspec2's JSON catalogue, and
creates a sensor for every point that catalogue describes. A device advertising
a model nobody anticipated still works. This is the single fact that shapes the
whole migration — modbus-connection's model framework is declarative and
author-time, and its SunSpec answer to "many models" is an *offline* code
generator (`python -m modbus_connection.model.sunspec.generate 103 160 …`).
Generated classes cannot cover a model nobody generated, so the definitions have
to be compiled at runtime instead.

**Repeat counts come from three different places.** A SunSpec group's `count`
field in the JSON is:

| `count` | means | mapped to |
| --- | --- | --- |
| absent | a single nested namespace | `repeating_group(1, …)` |
| `0` | *divide the leftover model length by the block size* | `repeating_group(<int>, …)` |
| `"NCrv"` | read the named point at poll time | `repeating_group(uint16(addr), …)` |

The `0` case is the weird one: the count is neither fixed nor read from a count
register — it is **derived from the length in the model header**, which is only
known after the scan. That is how the flagship multiple-MPPT model (160) sizes
its modules, and 44 of the shipped definitions use it.

**The count point is always a point of the *model*, never of the group that
uses it** — even for a group nested two levels down. pysunspec2 resolves it with
a literal `getattr(self.model, count_name)`. This is what makes models 705–712
hard (gap 3).

**A point's validity depends on its value, not its definition.** `isValidPoint`
calls a point real only if the device implemented it *and* it carries a unit (or
is an `enum16` / `bitfield32` — that exact pair, not `enum32` or `bitfield16`).
So the sensor list is derived from the values of the first successful poll, and
a device that leaves a point unimplemented simply has no sensor for it.

**Enums are decoded in the entity layer, not the protocol layer.** The wrapper
hands out raw integers and `sensor.py` maps them through the definition's symbol
table itself — a `bitfield32` becomes a comma-joined list of symbol names,
truncated to 255 characters to fit a Home Assistant state. So the components
deliberately do *not* pass an `IntEnum` to `enum16()`.

**Energy sensors lie about zero.** `SunSpecEnergySensor.native_value` returns
the last known value instead of `0`, because a genuine zero would reset Home
Assistant's `total_increasing` long-term statistics. Inverters report 0 when
asleep.

**Only depth-1 groups become sensors.** The DER curve models (705–712) nest
curve *points* inside curves; those exist in the map, are sized correctly, and
are deliberately never exposed — matching the old behaviour exactly. Their sizes
still have to be computed, because the enclosing curve's stride depends on them.

**Entity unique IDs embed the point key.** `"<entry>_<group>:<index>:<point>-<model>-<index>"`.
The `group:index:point` key format is therefore a compatibility contract, not an
implementation detail — renaming a key orphans a user's entity.

For completeness, the cursed parts that this migration **deleted**: a
process-global `CLIENT_CACHE` keyed by `host:port:unit`; a raw
`socket.connect_ex()` port probe (with `socket.setdefaulttimeout()`, a
`threading.Lock` and a `time.sleep(0.1)`) run before every connect attempt; a
`reconnect_next()` flag; `time.sleep(0.6)` between model reads and `delay=0.5`
during the scan; and a 120-second socket timeout. Connect-on-demand,
`message_spacing` and the typed error hierarchy replace all of it.

---

## 2. What internals of modbus-connection did I have to touch?

**None.** No private attribute, no subclass of a private class, no
monkeypatching, nothing reached around. That is worth saying plainly, because
this is an unusually demanding consumer.

What it *does* do is use public API in ways the docs don't cover:

- **Component classes are built with `type()` at runtime.** `Component.__init_subclass__`
  collects fields by walking `vars()` over the MRO, so a class built from a
  namespace dict works exactly like a written one — `__set_name__` fires, the
  descriptors bind, `repeating_group` fields land in the right bucket. This is
  the load-bearing assumption of the whole migration and it is documented
  nowhere. Attribute names are machine-generated (`p_<point>`, `g_<group>`)
  because SunSpec point names are not guaranteed to be identifiers and could
  collide with `Component`'s own attributes.

- **Two fields at the same address.** Every point with a scale factor is
  declared twice: once with `scale_register=`, once without (`raw_attr`). The
  planner merges them into one block read, so it costs no extra request — but it
  is not documented that two `ReadItem`s at one address are free. See gap 1 for
  why this is necessary.

- **A raw read outside the model layer.** `_async_read_counts()` calls
  `unit.read_holding_registers()` directly to resolve a nested group's count
  before the layout can be built (gaps 3 and 4).

In the tests: `MockModbusConnection` is subclassed to accept the params/kwargs
the real constructor takes and to preload registers, and `unit.fail_read()` /
`simulate_connection_lost()` drive the failure paths. The register map itself is
rendered from a pysunspec2 device dump into real holding registers, so the tests
exercise real addresses and framing rather than a stand-in client.

---

## 3. What could modbus-connection do better to support this library?

Ordered by how much pain each caused. Two more were found against 4.1.0 and are
**already fixed in 4.3.0** — see the end of this section.

### 1. An unimplemented scale factor should not erase the point.

`sunssf`'s unimplemented sentinel is `0x8000`, which decodes to `-32768`, which
falls outside the `(-10, 10)` spec range that `_scaled()` guards, so the point
decodes to **`None`**. Three states collapse into one:

- the point is unimplemented → `None` (correct)
- the *scale factor* is unimplemented → `None` (wrong)
- the scale factor is garbage → `None` (correct, and the guard is a good idea)

pysunspec2, pysunspec and async-sunspec all treat an unimplemented factor as
scale 1 and hand back the raw value. Real dumps do this constantly: in this
repo's test device, model 103's `A_SF`, `W_SF`, `Hz_SF` … are all unimplemented
while `A`, `W`, `Hz` are populated. Taken literally, migrating cost model 103
**20 of its 23 sensors**.

Working around it needs the raw value, and there is no way to ask a component
for one — hence declaring every scaled point twice. Concretely, either:

- decode an SF holding its *unimplemented sentinel* as "no scaling" rather than
  as an out-of-range exponent (distinct from the garbage-exponent case), or
- expose the pre-scale value, e.g. `component.raw("field")` /
  `NumberField.raw_value`, so one declaration serves both.

The first is what every other SunSpec reader does. The second is more generally
useful — diagnostics want it too.

### 2. `scale_in_block` should be per field, not per component.

It is a class attribute, so a repeating block either carries *all* its scale
factors or *none*. Model 63001's repeating block references two factors inside
itself and one in the model's shared fixed block, and there is no way to express
that: the point is dropped and logged. One point out of the whole catalogue —
but the fix is small (`uint16(0, scale_register=1, scale_in_block=True)`) and it
removes a whole class of "cannot express" from a consumer that must handle
arbitrary maps.

### 3. A `repeating_group`'s count register moves with the instance

There is no opt-out, unlike scale registers. `_count_items` resolves a count at
`count_field.address + base_offset + instance_offset`. For a group nested inside
another repeat, instance *i* reads its count from `count_addr + i * stride`.
Demonstrated on the mock: an outer group of 2 with an inner count at offset 5
reads addresses **5 and 25**, so instance 1 gets a count of 0 and silently has
no sub-instances.

That is exactly the problem `scale_in_block` solves for scale registers — and
scale registers default to *not* moving, which is the safer default. Counts have
no equivalent knob. Every nested SunSpec group hits this: models 705, 706, 712
count curve points by `NPt`, and 707–710 do it three levels deep, with `NPt`
always in the model's fixed block. **Give counts the same treatment**, ideally
defaulting to "stay put" for symmetry with `scale_register`.

### 4. `stride` has to be a static `int`

So a block containing a runtime-counted group cannot be placed. Model 705's
curve block is `11 + NPt * 2` registers. `NPt` is only known after a
read, so the stride is only known after a read, so the layout cannot be built
before one. This integration resolves those counts with a one-off
`read_holding_registers()` at setup and bakes them in as fixed `int` counts —
losing the per-poll re-read for the nested level, and reaching outside the model
layer to do it.

Either accept a resolved-at-build-time stride through a documented API, or offer
a `async_resolve(unit)` step that reads a layout's count registers once and hands
back the sized component. Every consumer of a nested SunSpec model needs this;
none of them should hand-roll it.

The same root cause makes a group that *follows* a runtime-counted group
impossible to place (`build_layout` rejects it explicitly). No shipped
definition does that today, but it is a structural limit worth naming.

### 5. Let a `ManualComponent` join a `ComponentGroup`.

`ManualComponent` is the documented answer to "the layout comes from config
rather than a typed class", and it was the obvious first choice here — keys are
arbitrary strings, `add()` takes `repeating_group` targets, no attribute-name
mangling needed. It was rejected for one reason: **it cannot be pooled into a
`ComponentGroup`**, and pooling is what turns a poll from one-request-per-model
into 4 block reads. It also does not get `SunSpecComponent`'s header
verification. It already produces the same read items as a `Component`; letting
it into a group would have made this migration considerably smaller.

### 6. Let `scan()` take more than one base address.

`scan(unit, base_address)` takes exactly one address and raises `SunSpecError` if
the marker is not there. Requiring the caller to name it is deliberate — a
library written for one brand knows where that brand puts its map, and probing
addresses it will never use is wasted round trips. A *generic* consumer is the
other case: SunSpec defines three locations (40000, 0, 50000) and pysunspec2's
`base_addr_list` is literally `[40000, 0, 50000]`, so this integration
reimplements the loop and its error aggregation. Accepting a sequence and
returning one `SunSpecModels` for whatever was found would cover both without
changing the default. Filed as
[home-assistant-libs/modbus-connection#147](https://github.com/home-assistant-libs/modbus-connection/issues/147).

### 7. Document the runtime-built component.

`type("Name", (Component,), namespace)` works perfectly and is the only way to
be generic over a catalogue of maps. It is also entirely undocumented and
untested by the library, which makes it feel like something that could break in
a refactor. Either bless it with a `Component.build(name, fields)` helper and a
test, or say in the docs that `__init_subclass__` field collection is a
supported extension point.

### Already fixed in 4.3.0

Both of these bit while the migration was written against 4.1.0, and both are
gone in 4.3.0 — recorded because they show up in the shape of the code's history,
not because they still need doing.

- **`disconnect()`.** 4.1.0 had only `close()`, which is permanent, so dropping
  the link between polls meant rebuilding the connection — which invalidates
  every `ModbusUnit` handle, and units are held by components that cache their
  read plans. The workaround was a delegating `ReconnectingUnit` proxy that
  outlived the connection under it. 4.3.0's `disconnect()` deleted that class
  outright; `async_disconnect()` is now one line.
- **The mock had drifted from the real connection.** In 4.1.0
  `MockModbusConnection` was a separate implementation registered with
  `BaseModbusConnection.register()` rather than a subclass, so it had no
  `_client`, different `connected` semantics, and no `fail_requests()` — a
  fixture wanting "this device answers nothing" had to fake it address by
  address. In 4.3.0 it subclasses the base and has `fail_requests()`, and the
  fixture is three lines shorter.

### Things that went better than expected

- The per-type unimplemented sentinels are exactly right. `acc*` treating `0` as
  "not accumulated", `sunssf` at `0x8000`, the int/uint NaN table — all matched
  pysunspec2's behaviour with no adjustment.
- `scale_register` reading the factor inside the same pooled block is precisely
  what SunSpec needs, and shared factors staying put across a repeating block is
  the right default for model 160.
- `SunSpecComponent`'s header verification plus `SunSpecMapShiftError` is a
  genuinely good idea that the pysunspec2-based code had no equivalent of: a
  firmware update that inserts a model used to silently produce garbage.
- Pooled `ComponentGroup` reads and `message_spacing` between them replaced a
  pile of hand-rolled sleeping with two lines of configuration.
