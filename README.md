# DSX OS NICo Emulator — Vera Rubin NVL72 Digital Twin

> **분리 이력**: 이 프로젝트는 [NeoCloud OS Control-Plane(nocp)](https://github.com/cscw95/NeoCloud-Control-Plane)
> 내부의 NICo 에뮬레이션 기능을 **독립 신규 기능으로 분리**한 저장소다.
> nocp은 `NOCP_NICO_URL`로 이 서비스(:9000)의 `/nico-bridge`에 접속해 실연동한다.

Standalone emulator of the **NVIDIA Infra Controller (NICo)** control-plane surface
and a **Vera Rubin NVL72 rack digital twin**, with DPU-enforced tenant isolation.
Built from the design analysis (`NICo–Vera Rubin NVL72 Emulator Code Analysis`) and
the DPU isolation design (`NeoCloud Vera Rubin NVL72 DPU Isolation Emulator Design`).

It is an **independent service** (not part of NOCP) and integrates with the existing
**NeoCloud OS (NOCP)** control-plane over REST.

## Quick Start

```bash
bash run.sh        # http://127.0.0.1:9000  (dashboard at /, OpenAPI at /docs)
```

Uses the NOCP virtualenv by default (`~/nocp/.venv`); set `PYTHON=...` to override.

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
| **NOCP bridge** | `/nico-bridge/...` | The exact contract NOCP's `NicoHttpAdapter` speaks |

## Built-in fault scenarios (design §12)

```bash
curl -X POST http://127.0.0.1:9000/emulator/v1/scenarios/inter-tenant-isolation/run -d '{}'
```

| name | validates |
|------|-----------|
| `inter-tenant-isolation` | tenant-A→tenant-B traffic 100% dropped (INTER_TENANT_DENY) |
| `mac-spoof-quarantine` | spoofed source MAC dropped + function quarantined |
| `policy-rollback-lkg` | flow-programming failure rolls back to last-known-good generation |
| `arm-os-fail-closed` | DPU Arm OS crash → tenant functions disabled, tray degraded (fail-closed) |
| `ipsec-sa-expiry` | IPsec rekey failure raises auth-failure counter |

Each scenario drives the real isolation engine and returns `{passed, steps, assertions, telemetry_delta}`.

## Integration with NeoCloud OS (NOCP)

The emulator exposes `/nico-bridge`, which implements the same REST contract NOCP's
`NicoHttpAdapter` speaks (`/hosts`, `/instances`, `/jobs` with NicoHost/NicoJob shapes).
Point NOCP's compute adapter at the emulator:

```bash
# in the nocp repo
NOCP_NICO_URL=http://127.0.0.1:9000/nico-bridge ./run.sh
```

NOCP's provisioning lifecycle (reserve → provision → allocate → cordon → sanitize)
then drives the emulator's twin and DPU isolation engine. An end-to-end proof using
NOCP's *actual* adapter code:

```bash
# with the emulator running on :9000
cd ~/nocp && PYTHONPATH=. .venv/bin/python scripts/integrate_emulator.py   # 7 PASS
```

> Scope note: the twin models **one** VR NVL72 rack (the design's "device/site digital
> twin"). To back NOCP's whole multi-rack fleet, scale the twin constants in
> `app/store.py` or run one emulator instance per rack behind a router.

## Tests

```bash
~/nocp/.venv/bin/python -m pytest tests/ -q      # 19 passed
```

## Layout

```
app/store.py         in-memory twin + DPU/policy/flow registries (thread-safe)
app/models.py        Pydantic request/response models
app/dpu.py           DPU isolation API + isolation engine
app/redfish.py       Redfish BMC emulator
app/provisioning.py  DHCP / PXE / DNS + boot state machine
app/fabric.py        NVLink / InfiniBand / Ethernet
app/scenarios.py     fault scenario engine (5 built-in)
app/bridge.py        NOCP NicoHttpAdapter-compatible bridge
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
  delivery.
