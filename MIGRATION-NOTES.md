# Migrating ha-sunspec to modbus-connection

This integration used to talk to devices through **pysunspec2**'s synchronous
Modbus client, driven from Home Assistant's executor. It now talks
**modbus-connection 4.6.0** (tmodbus backend) and keeps pysunspec2 only for the
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

- **The readable map is discovered, not declared.** `register_ranges` is normally
  written into a component class by its author. Here the map is a *result of the
  scan*: a SunSpec chain is one contiguous run of registers — each model's header
  says where the next one starts — so `SunSpecModels.chain` and `SunSpecModel.span`
  give the exact extent the device answers, and every component gets it set per
  instance. Without it, the planner keeps each model's reads inside the addresses
  that model claims by itself, and a trailing `pad` point is enough to stop a block
  at a model boundary: reading all 16 models of the test device costs 13 requests
  instead of 7. Declaring only each model's *own* block is worse than both (19) —
  a merge keeps every boundary any component's map draws, so every model boundary
  becomes a cut.

In the tests: `MockModbusConnection` is subclassed to accept the params/kwargs
the real constructor takes and to preload registers, and `unit.fail_read()` /
`simulate_connection_lost()` drive the failure paths. The register map itself is
rendered from a pysunspec2 device dump into real holding registers, so the tests
exercise real addresses and framing rather than a stand-in client.

---

## 3. What could modbus-connection do better to support this library?

Ordered by how much pain each caused, and annotated with whether real hardware
actually needs it — spec completeness on its own is not a reason to change the
library. Gaps 2 and 6 are recorded as **not worth doing**; gap 5 was **closed in
4.4.0** and is kept for the reason the integration still does not take it up; one
more looked like a gap and turned out not to be, and two were found against 4.1.0
and are **already fixed in 4.3.0** — both at the end of this section.

The evidence for "real hardware" throughout is a **FranklinWH aGate** dump
(firmware `V10R01B04D00`, models 1, 502, 701–715) plus `solaredge-modbus-multi`'s
handling of the same cases. Where the only evidence is this repo's
`tests/test_data/inverter.json`, that is a *synthetic* pysunspec2 fixture and is
called out as such.

On the aGate dump's provenance, since a lot rests on it: it is committed in
[david2069/franklinwh-modbus](https://github.com/david2069/franklinwh-modbus),
one of the surveyed projects — **that author's own device, not hardware anyone
here has**. The register dumps are machine output (the generating tool is
committed beside them, and a second generated report agrees on IP, model,
firmware and serial), so the model set, the counts and the scale-factor values
are solid. The repo's *hand-written* analysis is weaker: it is a heavily
AI-assisted project and every `tests/results/*.md` its prose cites as evidence is
missing from the repo.

A **second owner** of the same hardware then posted an instrumented report
against their own unit
([issue #5](https://github.com/david2069/franklinwh-modbus/issues/5#issuecomment-4529319648)),
which independently confirms the M701–M715 model set and establishes two things
the prose only asserted (and that project's issue
[#11](https://github.com/david2069/franklinwh-modbus/issues/11) adds a third
point, below): the SunSpec chain sits at **base 0** (pysunspec2 places
M701 at address 70, M704 at 296, M713 at 1033, and a sweep finds 40000+ and
50000+ return illegal-address), and a **write can succeed at protocol level
without applying** — a write to a spec-read-only register answered with a success
echo carrying the *stored* value rather than an exception. That second point is
why the write-side asks in
[#156](https://github.com/home-assistant-libs/modbus-connection/issues/156) grew
a readback-verification item.

The third point: batching contiguous writes into one transaction is a
**correctness** requirement on that hardware, not only a round-trip saving — the
aGate's reserve registers reset to a default when the adjacent mode register is
written, so three sequential single-register writes are not equivalent to one
multi-register write. Which also means a write planner must *not* merge as freely
as `ReadPlan` does: the same device wants its DER control writes ordered with the
enable register last, so grouping has to express the caller's intent rather than
be inferred.

### 1. An unimplemented scale factor should not erase the point.

`sunssf`'s unimplemented sentinel is `0x8000`, which decodes to `-32768`, which
falls outside the `(-10, 10)` spec range that `_scaled()` guards, so the point
decodes to **`None`**. Three states collapse into one:

- the point is unimplemented → `None` (correct)
- the *scale factor* is unimplemented → `None` (wrong)
- the scale factor is garbage → `None` (correct, and the guard is a good idea)

pysunspec2 and pysunspec treat an unimplemented factor as scale 1 and hand back
the raw value, so this integration had to as well: the test device's model 103
leaves `A_SF`, `W_SF`, `Hz_SF` … unimplemented while `A`, `W`, `Hz` are
populated, and taking modbus-connection's decoding literally cost model 103
**20 of its 23 sensors**. Reproducing pysunspec2's behaviour needs the raw value,
and there is no way to ask a component for one — hence declaring every scaled
point twice.

**But the real-world basis for changing the library is weak, and worth stating
plainly.** `tests/test_data/inverter.json` is a *synthetic* pysunspec2 fixture
("SunSpecTest", serial `sn-123456789`), not a device dump. On the one real
multi-model dump in the surveyed corpus — a FranklinWH aGate, firmware
`V10R01B04D00` — **all 55 `sunssf` registers hold valid in-range exponents**;
none is unimplemented. And `solaredge-modbus-multi`, a real and widely used
integration, deliberately returns `None` when a scale factor is unimplemented
*or* out of the -10..10 range — i.e. it agrees with modbus-connection, not with
pysunspec2.

So modbus-connection's decoding is defensible, and the twin-field trick here
buys **behavioural parity with the old integration**, not correctness on any
device anyone has shown. What survives as a genuine gap is narrower: a component
cannot expose a field's pre-scale value at all. `async_read_raw()` re-reads the
device, so it does not answer "what was the raw value behind this decoded field
on the last poll". A `component.raw("field")` would serve diagnostics as well as
this workaround.

### 2. `scale_in_block` should be per field, not per component — *no real-world case*

It is a class attribute, so a repeating block carries *all* its scale factors or
none. The one definition that mixes them is model **63001**, whose repeating
block references two factors inside itself and one in the model's fixed block;
the offending point is dropped and logged.

Recording this as **not worth doing**. 63001 and 63002 are SunSpec's own *test*
models, and no real device in the surveyed corpus implements either. Model 133 is
the only shipped definition that keeps scale factors inside a repeating block for
real, and it references *only* its own — which `scale_in_block=True` already
handles correctly. So the cost of the limitation is one point in a test model.

### 3. A `repeating_group`'s count register moves with the instance

Filed, together with gap 4 and the write-side gaps below, as
[home-assistant-libs/modbus-connection#156](https://github.com/home-assistant-libs/modbus-connection/issues/156)
— which frames the whole set against the SunSpec Modbus IEEE 1547-2018 profile:
7 of that profile's 16 models (705–710, 712) cannot be expressed today, and they
are the entire curve-based control half of it.

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

**This is shipping hardware, not spec completeness.** A FranklinWH aGate
(firmware `V10R01B04D00`) implements models 1, 502 and 701–715, with the nested
counts populated:

| Model | | Counts read from the device |
| --- | --- | --- |
| 705 | DERVoltVar | `NCrv` = 3, `NPt` = 4 |
| 706 | DERVoltWatt | `NCrv` = 2, `NPt` = 2 |
| 707–710 | DERTrip LV/HV/LF/HF | `NCrvSet` = 2, `NPt` = 5 |
| 711 | DERFreqDroop | `NCtl` = 2 |
| 712 | DERWattVar | `NCrv` = 2, `NPt` = 6 |
| 714 | DERMeasureDC | `NPrt` = 1 |

Non-zero at both levels, so the nested groups genuinely exist and are genuinely
sized at runtime. Any battery or inverter certified to IEEE 1547-2018 carries
this model set.

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

Same real hardware as gap 3: the aGate's model 705 reports `NPt` = 4, so its
curve block is 19 registers, and nothing can be placed after it until that read
has happened.

The same root cause makes a group that *follows* a runtime-counted group
impossible to place (`build_layout` rejects it explicitly). No shipped
definition does that today, but it is a structural limit worth naming.

### 5. Let a `ManualComponent` join a `ComponentGroup` — *done in 4.4.0*

`ManualComponent` is the documented answer to "the layout comes from config
rather than a typed class", and it was the obvious first choice here — keys are
arbitrary strings, `add()` takes `repeating_group` targets, no attribute-name
mangling needed. It was rejected for one reason: **it could not be pooled into a
`ComponentGroup`**, and pooling is what turns a poll from one-request-per-model
into 4 block reads. It already produced the same read items as a `Component`;
letting it into a group would have made this migration considerably smaller.

The stale-plan objection raised here — `add()` / `remove()` invalidate the
component's own cached plan, but a `ComponentGroup` caches a plan built from its
members' read items and had no way to hear about that — is what 4.4.0 answered:
a member holds a reference to the group and invalidation propagates to it, so a
mutation re-plans the pooled read rather than silently reading against a stale
one.

**The integration still does not switch, and the reason is the one thing that
list left out**: a `ManualComponent` is not a `SunSpecComponent`, so it does not
verify the model header it read against the model the scan discovered. That check
and its `SunSpecMapShiftError` are what turn a firmware update that inserts a
model into a config-entry reload instead of silently wrong sensors — see the last
section. Trading it for a smaller compiler is a bad trade for a device library
whose addresses all come from a scan. The `type()`-built subclass in gap 6 stays.

### 6. The runtime-built component stays unsupported — *by decision*

`type("Name", (Component,), namespace)` works perfectly and is the only way to be
generic over a catalogue of maps: `__init_subclass__` collects fields by walking
`vars()`, `__set_name__` fires, descriptors bind, `repeating_group` fields land in
the right bucket. It is also entirely undocumented and untested by the library.

Asked whether it should be blessed with a `Component.build()` helper, the answer
was no — it is a hack the library does not want to endorse, and this integration
is already doing something unusual by manufacturing its map on demand. Recorded
here as the risk it is: the load-bearing assumption of this whole migration rests
on behaviour with no test protecting it, so a refactor of field collection could
break ha-sunspec without breaking anything in modbus-connection's own suite. The
mitigation is on this side — `tests/test_model.py::test_every_shipped_definition_compiles`
compiles all 106 model definitions, and would fail loudly if the mechanism ever
stopped working.

### Investigated and dropped: multiple base addresses

`scan(unit, base_address)` takes exactly one address, so this integration
reimplements pysunspec2's `base_addr_list = [40000, 0, 50000]` loop itself. That
looked like a gap, and it was filed as
[home-assistant-libs/modbus-connection#147](https://github.com/home-assistant-libs/modbus-connection/issues/147)
— then closed as not planned, because the survey behind it went the other way.

Across ~65 surveyed libraries, **no manufacturer exposes SunSpec chains at two
base addresses at once**: pysunspec and pysunspec2 `break` on the first marker,
async-sunspec does not probe at all, and ABB/FIMER makes the base address a
single user-configured value — default `0`, validated over the whole 0–65535
range, because "it may vary". So there is nothing to merge into one result, and
the candidate list is not reliably the three spec addresses anyway, which is
exactly why naming the address is the caller's job. The real multiplicity in
SunSpec devices is on other axes — unit IDs, and repeated model IDs within one
chain, both already covered.

What survives is small: a generic consumer still decides for itself which
exception means "try the next address" (`SunSpecError`, `ModbusExceptionError`)
versus "this device is unreachable, stop now" (`ModbusConnectionError`). A
handful of lines, in a rare kind of consumer — not worth API surface.

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
