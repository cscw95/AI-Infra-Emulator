# AI Infra Emulator — Vera Rubin NVL72 Physical Digital Twin 

> **Split history**: this project is a repository that **split the AI Infra
> emulation functionality out of [NeoCloud OS Control-Plane (nocp)](https://github.com/cscw95/NeoCloud-Control-Plane)
> into a standalone new component**. The NICo Emulator (:9000) drives this
> service (:9100) over REST, and nocp queries the physical topology via `AI_INFRA_URL`.

Standalone emulator of the **NVIDIA Infra Controller (NICo)** control-plane surface
and a **Vera Rubin NVL72 rack digital twin**, with DPU-enforced tenant isolation.
Built from the design analysis (`NICo–Vera Rubin NVL72 Emulator Code Analysis`) and
the DPU isolation design (`NeoCloud Vera Rubin NVL72 DPU Isolation Emulator Design`).

It is an **independent service** (not part of NOCP) and integrates with the existing
**NeoCloud OS (NOCP)** control-plane over REST.

## Quick Start

```bash
bash run.sh        # http://127.0.0.1:9100  (dashboard at /, OpenAPI at /docs)
```

Uses `./.venv` when present, otherwise `python3`; set `PYTHON=...` to override.

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## What it emulates

Per the design docs, the twin is a single **Vera Rubin NVL72 rack**
(18 compute trays × 4 Rubin GPU = 72 GPU, one BlueField DPU per tray, 9 NVLink
switch trays, power shelves, CDU).

| Domain | Surface | Highlights |
|--------|---------|-----------|
| **DPU isolation** | `/emulator/v1/dpus/...` | VF/SF/representor model, tenant attachments, **default-deny** engine, policy generation + Last-Known-Good rollback, telemetry counters, fault injection |
| **Redfish BMC** | `/redfish/v1/...` | Systems/Chassis/Managers/UpdateService/EventService + DPU BMC; PowerState state machine (Off→PoweringOn→On); boot override |
| **Provisioning** | `/emulator/v1/provision`, `/dhcp`, `/pxe`, `/dns` | DHCP lease, iPXE script, boot state machine (PXE→DHCP→iPXE→OS→HostReady) |
| **Fabric** | `/emulator/v1/fabric/...` | NVLink domain, InfiniBand P_Key partitions, Ethernet VXLAN segments, switch inventory |
| **Scenarios** | `/emulator/v1/scenarios/...` | 5 built-in fault scenarios (design §12) |
| **Metrics** | `/metrics` | Prometheus text exposition of DPU counters |
| **OOB (DSX)** | `/redfish/v1/Chassis/{id}/PowerSubsystem`, `ThermalSubsystem`, `LogServices/SEL`, `/emulator/v1/oob` | power shelves/PSUs, leak, inlet, SEL, AC power cycle, GPU reset, EventService push |
| **MaxLPS / DPS** | `/dps/v1/...` | PowerDomain/ResourceGroup, static vs dynamic allocation, WPPS profiles, simulate, DSX Flex |
| **DSX Exchange** | `/exchange/v1/...` | event bus (SSE) — power, thermal, leak, grid, health |

## Hardware vendor: Dell

The whole AI Infra hardware supply vendor is **Dell** (Dell AI Factory). Rack,
trays, power shelves, CDU, BMC, switches and storage carry Dell identity; the
silicon inside (Rubin GPU, Vera CPU, ConnectX, BlueField, NVLink) stays NVIDIA.
All strings live in `app/dell.py`; sources and verification status are in
`../RESEARCH-DELL.md`.

- Redfish: compute-tray System/Chassis and rack Chassis report `Manufacturer: "Dell Inc."`,
  `Model`, `SKU` (7-char service tag), `SerialNumber`, `PartNumber` and an `Oem.Dell`
  block (`DellSystem`, `DelliDRACCard`). `Oem.Nvidia` stays for NICo/NOCP.
  Tray Managers are **iDRAC10**; per-BMC Dell ids are served as aliases at
  `/redfish/v1/Dell/{tray_id}/Managers/iDRAC.Embedded.1`, `.../Systems/System.Embedded.1`,
  `.../Chassis/System.Embedded.1`. DPU systems/BMCs stay NVIDIA.
- Storage: canonical API is **Dell PowerScale** at `/powerscale/v1/*`. `/vast/v1/*`
  and its field names (`cboxes`, `dboxes`, `drr`, `vast-<site>` names) still work and
  answer with `Deprecation: true` + `Link: rel="successor-version"`.
- `GET /versions` adds `"hw_vendor": "Dell"`; `GET /emulator/v1/inventory/bom` and
  `/emulator/v1/inventory/bom/{rack_id}` return the bill of materials.
- Power model unchanged (227 kW MaxP / 187 kW MaxQ, 4 × 110 kW shelves). Dell's only
  public VR figure is "racks of 235 kW" (spoken, May 2026); the 110 kW shelf is a
  model rating, not a published Dell part (Dell lists the IR7000 PS33, 6 × 5.5 kW).

### Bill of materials (per rack / per site)

| Item | Dell part | Qty | Status |
|---|---|---|---|
| Rack | Dell IR9000 ORv3 rack | 1 | unverified pairing |
| Compute tray | PowerEdge XE9812 compute tray (4 Rubin GPU + 2 Vera CPU) | 18 | verified |
| NVLink switch tray | PowerEdge XE9812 NVLink switch tray | 9 | name unverified |
| Power shelf / PSU | Dell ORv3 power shelf 110 kW / PSU 18.3 kW | 4 / 24 | model rating, unverified |
| BMC | iDRAC10 (`iDRAC.Embedded.1`) | 18 | verified |
| Rack controller | Dell Integrated Rack Controller (IRC) | 1 | verified |
| CDU (per SU) | Dell PowerCool CDU C7000 | site | verified |
| Ethernet leaf/spine | Dell PowerSwitch SN5610 (Spectrum-4, 64 × 800GbE) | site | verified |
| InfiniBand | NVIDIA Quantum-X800 Q3400, Dell supplied | site | Dell resale verified for Q3300-LD only |
| Storage | Dell PowerScale F910 (OneFS) | site | verified |
| Management | Dell OpenManage Enterprise | site | verified |
| GPU / DPU (silicon) | NVIDIA Rubin ×72 / BlueField ×18 | — | NVIDIA |

## DSX OS baseline (v0.2.0)

This emulator tracks the **NVIDIA DSX OS (2026.05)** layering: it is the physical /
OOB plane under Provisioning (NICo v2.3 Redfish, power shelves, RMS-style AC power
cycle) and Fleet Health (SEL, leak, thermal), and it hosts the **DSX MaxLPS** power
layer (DPS `nvidia.dcpower.v1` shape) plus a **DSX Exchange** style event bus.

### Version matrix

Served at `GET /versions` and `GET /emulator/v1/versions` (same payload in all four
NeoCloud repos):

| dsx_os | nico | nvke | dps | mission_control | doca | gpu_operator | k8s | platform |
|---|---|---|---|---|---|---|---|---|
| 2026.05 | 2.3.0 | 0.23.1 | 0.10 | 2.5 | 3.x | AICR recipe | 1.33, 1.34 | Vera Rubin NVL72 |

### New endpoints

Chassis `{id}` is a compute tray (`su-1-rack-00-tray-00`) or a rack (`su-1-rack-00`).
Trays share the rack's power-shelf busbar (4 shelves × 110 kW, 6 PSUs each = 440 kW; see `app/power_model.py`).

| Method | Path | Purpose |
|---|---|---|
| GET | `/redfish/v1/Chassis/{id}/PowerSubsystem` | capacity, allocation (cap) vs requested, PSU redundancy |
| GET | `/redfish/v1/Chassis/{id}/PowerSubsystem/PowerSupplies[/{psu}]` | power-shelf PSUs (`PS{shelf}-PSU{n}`), output W, health |
| GET | `/redfish/v1/Chassis/{id}/ThermalSubsystem` | inlet/GPU temperatures + `LeakDetection` (`OK\|Warning\|Critical`) |
| GET | `/redfish/v1/Chassis/{id}/ThermalSubsystem/LeakDetection` | leak detector groups (cold plates, rack manifold, CDU branch) |
| GET | `/redfish/v1/Chassis/{id}/Controls[/PowerLimit]` | current power limit, effective (DPS) limit, reading |
| PATCH | `/redfish/v1/Chassis/{id}/Controls/PowerLimit` | `{"SetPoint": W}` tray or rack cap; `{"ControlMode":"Automatic"}` clears |
| POST | `/redfish/v1/Chassis/{id}/Actions/Chassis.Reset` | `{"ResetType":"PowerCycle"}` AC power cycle (works with BMC down, restores it) |
| GET | `/redfish/v1/Systems/{id}/LogServices[/SEL]` | log services |
| GET | `/redfish/v1/Systems/{id}/LogServices/SEL/Entries[/{entry}]` | SEL entries (`?severity=Critical`) |
| POST | `/redfish/v1/Systems/{id}/LogServices/SEL/Actions/LogService.ClearLog` | clear SEL |
| POST | `/redfish/v1/Systems/{id}/Actions/GpuReset` | OOB GPU reset `{"GpuIds":[0,1]}` (needs BMC + host On) |
| POST | `/redfish/v1/EventService/Subscriptions` | `{"Destination": url}` → events POSTed (Redfish `Event` shape). Default categories `leak, thermal, psu, sel`; override with `{"Oem":{"Nvidia":{"Categories":[...]}}}` (also `power, grid, health`). Delivery stats in `Oem.Nvidia` |
| GET, POST | `/dps/v1/power-domains` | PowerDomain list/create (`operating_limit_kw`, `mode: static\|dynamic`, `smoothing`, `racks`) |
| GET, PATCH | `/dps/v1/power-domains/{id}` | detail with groups / change limit, mode, smoothing (re-enforces rack caps) |
| GET, POST | `/dps/v1/resource-groups` | groups under a domain (`racks`, `priority`, `min_kw`, `max_kw`); `?domain=` |
| GET | `/dps/v1/profiles` | WPPS profiles `max-p, max-q, training, inference, balanced` |
| POST | `/dps/v1/racks/{rack}/profile` | `{"profile": ...}` apply a WPPS profile to a rack |
| GET | `/dps/v1/telemetry?domain=&rack=&gpus=` | domain/group/rack/GPU measured, cap, headroom (`rack=` or `gpus=true` adds per-GPU) |
| POST | `/dps/v1/simulate` | `{"domain","operating_limit_kw","workload_mix":{training,inference,idle},"profile","smoothing"}` → static vs dynamic `racks_supported, gpus_supported, tokens_per_mw, headroom_kw` |
| GET, POST | `/dps/v1/flex-events` | DSX Flex demand response `{"domain","reduce_kw","duration_s"}` lowers the effective limit for the window |
| GET | `/emulator/v1/oob/summary?site=` | per rack `{bmc_reachable, bmc_total, psu_ok, psu_total, leak, max_inlet_c, sel_critical, power_kw, cap_kw}` + totals |
| GET | `/emulator/v1/oob/racks/{rack}` | per-tray OOB detail + PSUs |
| POST | `/emulator/v1/oob/inject` / `/recover` | fault injection: `leak, psu_fail, bmc_unreachable, thermal, sel_critical` |
| GET | `/exchange/v1/topics` | topics `power, thermal, leak, grid, health` with publish counts |
| GET | `/exchange/v1/events` | SSE stream (`?topic=a,b&since=<id>&follow=&timeout_s=`); `?stream=false` returns JSON |
| GET | `/versions`, `/emulator/v1/versions` | version matrix |

### Power model and the single cap source

- Hierarchy: PowerDomain (facility OperatingLimit) > ResourceGroup > Rack > Tray > GPU.
  Each NOCP ComputeBlock (floor) is seeded as one domain `pd-{block_id}`
  (`pd-aif-stt-gasan-f1` 6 MW, `pd-aif-stt-gasan-f2` 3 MW, `pd-aif-igis-ansan-f1` 12.9 MW,
  `pd-aif-igis-ansan-f2` 10.4 MW) with OperatingLimit = the block budget, one resource
  group per SU (`rg-su-N`), mode `static`. (Replaces the per-site `pd-gasan`/`pd-ansan`.)
- Rack power (`app/power_model.py`, NCP VR NVL72 reference design): MaxP 227 kW, MaxQ 187 kW.
  Rubin GPU 2300 W at Max-P → GPU 165.6 kW + overhead 61.4 kW (9 NVSwitch trays × 2.4 kW +
  18 tray hosts × 2.21 kW). MaxQ GPU = (187 − 61.4)/72 = 1744 W. WPPS profiles are rack
  fractions of MaxP (max-q 187/227, training 0.92, balanced 0.87, inference 0.75).
  Redfish rack PowerLimit AllowableMax = 227 000 W, tray = 227 000/18 = 12 611 W; a SetPoint
  outside the range is a 400 (no silent clamp). Power shelves 4 × 110 kW (6 × 18.3 kW PSU).
- `static`: every rack gets an equal share. `dynamic` (rack power steering): racks get
  their demand by group priority, then leftover headroom is redistributed up to each
  rack's nameplate. `smoothing` clips 75% of transient spikes.
- Rack caps computed by DPS are pushed into the observability rack control state on every
  DPS change, so DCGM/DCIM telemetry (`/emulator/v1/obs/*`) shows the same cap. Redfish
  rack `PowerLimit` and the operator `power_cap` action are kept as manual caps that
  bound the DPS allocation. NOCP should write caps here (AI-Infra is the source of truth).
- Numbers are emulator assumptions (Rubin board power 1.3 kW, rack overhead 24 kW), not
  vendor figures.

### Events

Every OOB/DPS change is published on the Exchange bus; the Redfish EventService
dispatcher listens to the bus and POSTs matching events to subscribers (thread pool,
3 s timeout; set `AI_INFRA_PUSH_SYNC=1` for inline delivery). CDU faults from
`/emulator/v1/obs/dlc/cdus/{id}/inject` are mirrored to the `leak` / `thermal` topics.

## Built-in fault scenarios (design §12)

```bash
curl -X POST http://127.0.0.1:9100/emulator/v1/scenarios/inter-tenant-isolation/run -d '{}'
```

| name | validates |
|------|-----------|
| `inter-tenant-isolation` | tenant-A→tenant-B traffic 100% dropped (INTER_TENANT_DENY) |
| `mac-spoof-quarantine` | spoofed source MAC dropped + function quarantined |
| `policy-rollback-lkg` | flow-programming failure rolls back to last-known-good generation |
| `arm-os-fail-closed` | DPU Arm OS crash → tenant functions disabled, tray degraded (fail-closed) |
| `ipsec-sa-expiry` | IPsec rekey failure raises auth-failure counter |

Each scenario drives the real isolation engine and returns `{passed, steps, assertions, telemetry_delta}`.

## Integration with the stack

This service owns the **physical plane** (rack twin, DPU isolation, fabric,
telemetry). It does not talk to NOCP directly — the [NICo Emulator](https://github.com/cscw95/NICo-Emulator)
(:9000) drives it over REST (`AI_INFRA_URL=http://127.0.0.1:9100`), and NOCP's
compute adapter points at the NICo Emulator's `/nico-bridge`:

```bash
bash run.sh                                              # ① this service (:9100)
cd ~/nico-emulator && bash run.sh                        # ② NICo Emulator (:9000)
cd ~/nocp && NOCP_NICO_URL=http://127.0.0.1:9000/nico-bridge ./run.sh   # ③ NOCP (:8000)
```

NOCP's provisioning lifecycle (reserve → provision → allocate → cordon → sanitize)
then drives this twin and the DPU isolation engine end-to-end. An end-to-end proof using
NOCP's *actual* adapter code:

```bash
# with the emulator running on :9100
cd ~/nocp && PYTHONPATH=. .venv/bin/python scripts/integrate_emulator.py   # 7 PASS
```

> Scope note: the twin models **one** VR NVL72 rack (the design's "device/site digital
> twin"). To back NOCP's whole multi-rack fleet, scale the twin constants in
> `app/store.py` or run one emulator instance per rack behind a router.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q      # 126 passed
```

## Layout

```
app/store.py         in-memory twin + DPU/policy/flow registries (thread-safe)
app/models.py        Pydantic request/response models
app/dpu.py           DPU isolation API + isolation engine
app/redfish.py       Redfish BMC emulator
app/provisioning.py  DHCP / PXE / DNS + boot state machine
app/fabric.py        NVLink / InfiniBand / Ethernet
app/exchange.py      DSX Exchange event bus (SSE, listeners)
app/dps.py           DPS / MaxLPS power domains, allocation, simulate, Flex
app/oob.py           OOB state (BMC, PSU, leak, inlet, SEL) + /emulator/v1/oob
app/redfish_oob.py   Redfish PowerSubsystem/ThermalSubsystem/SEL/PowerLimit/resets
app/dell.py          Dell hardware identity, BOM, iDRAC alias routes
app/main.py          FastAPI app (CORS for :8000/:8090) + dashboard + /metrics
static/index.html    single-page dashboard
tests/               pytest suite
```

## Design notes / roadmap

- Language: Python/FastAPI was chosen for single-host runnability and contract parity
  with NOCP. The design docs suggest a Rust/Go stateful protocol layer for a production
  build; this emulator implements the **behavioral contract** (state machines, counters,
  fault semantics) that such a layer would expose.
- Not yet modeled (design backlog): real DOCA Flow offload, OVS/OVN datapath, SPDM
  measured-boot attestation depth, per-rack fleet scale-out, Redfish EventService push
  delivery (now implemented, see DSX OS baseline).

## Security hardening (opt-in, `app/security.py`)

All controls default **off** (dev behaviour unchanged). Same env names in NICo-Emulator.

| Env | Effect |
|---|---|
| `EMU_API_KEY` | POST/PUT/PATCH/DELETE need `X-API-Key: <key>` or `Authorization: Bearer <key>` (401 otherwise) |
| `EMU_REDFISH_USER` + `EMU_REDFISH_PASSWORD` | HTTP Basic on every `/redfish/v1/*` except the service root, like a real BMC |
| `EMU_CORS_ORIGINS` | comma list of browser origins (default: localhost :8000/:8090/:9000) |
| `EMU_OUTBOUND_ALLOWLIST` | hosts / CIDRs / `.suffix` that EventService `Destination` may target (default `localhost,127.0.0.1,::1`). http/https only; link-local and cloud-metadata IPs are always refused; re-checked at delivery time |
| `EMU_MAX_SUBSCRIPTIONS` (64), `EMU_SSE_MAX_CLIENTS` (64), `EMU_SSE_MAX_S` (3600) | EventService / SSE caps |
| `EMU_MAX_BODY_BYTES` (1 MiB), `EMU_MAX_PAGE` (10000) | request body and `limit`/`top`/`pageSize` caps |

Input bounds: DPS kW values finite and ≤ 1 GW, `gpus_per_rack` 1..576, PowerLimit `SetPoint` ≤ 50 kW (tray) / 1 MW (rack).
