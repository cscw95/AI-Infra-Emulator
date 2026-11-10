"""GPU fault injection — XID / NVLink / thermal faults per GPU, the source
side of the F10 auto-remediation chain (NVSentinel-style: detect → cordon →
drain → GPU reset → re-validate → replace → RMA).

A fault lives on one GPU of a compute tray (Dell PowerEdge XE9812, 4 Rubin
GPU per tray). Every state change is published on the DSX Exchange bus
(topic `health`, or `thermal` for throttling) with the action an
auto-remediation engine should take, and mirrored into the /emulator/v1/faults
feed. The fault drives the rest of the twin:
  - DCGM telemetry (observability) reports the XID / throttle on that GPU
  - tray health → Redfish Status.Health (warning: reset fixes, critical: not)
  - the Dynamo inference emulator drops / slows workers on the GPU
  - Redfish Actions/GpuReset clears reset-recoverable faults; a fatal fault
    makes the reset Task end in Exception (→ F10 replaces the node)
  - a tray HW replace (new serial, trayops) clears every fault on it

  GET  /emulator/v1/gpu-faults?state=&tray_id=&rack_id=
  GET  /emulator/v1/gpu-faults/{id}
  POST /emulator/v1/gpu-faults/inject     {kind, tray_id, gpu?, xid?, reset_recovers?, delta_c?}
  POST /emulator/v1/gpu-faults/recover    {id? | tray_id?}
  GET  /emulator/v1/gpu-faults/scenarios  POST /emulator/v1/gpu-faults/scenarios/{name}"""
import itertools
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .store import STORE, GPU_PER_TRAY, _iso
from . import exchange

router = APIRouter(prefix="/emulator/v1/gpu-faults", tags=["gpu-faults"])

KINDS = ("xid", "nvlink", "thermal")
MAX_ACTIVE = 512
# XID catalog (NVIDIA XID errors): name, default "a GPU reset recovers it"
XID_CATALOG = {
    13: ("Graphics engine exception", True),
    31: ("GPU memory page fault", True),
    43: ("GPU stopped processing", True),
    45: ("Preemptive cleanup (robust channel)", True),
    48: ("Double-bit ECC error (DBE)", True),
    63: ("ECC page retirement / row-remap pending", True),
    64: ("ECC page retirement / row-remap failure", False),
    74: ("NVLink error", True),
    79: ("GPU has fallen off the bus", False),
    94: ("Contained ECC error", True),
    95: ("Uncontained ECC error", True),
    119: ("GSP RPC timeout", True),
    140: ("Unrecovered ECC error (HBM)", False),
}
NVLINK_XID = 74
THERMAL_DEFAULT_DELTA_C = 20.0
THERMAL_THROTTLE_PERF = 0.7       # perf factor while a GPU thermal-throttles


class _Faults:
    def __init__(self):
        self.seed_gen = -1
        self.faults: Dict[str, dict] = {}
        self._ids = itertools.count(1)

    def sync(self):
        cur = getattr(STORE, "seed_gen", 0)
        if cur != self.seed_gen:
            self.seed_gen = cur
            self.faults = {}
            self._ids = itertools.count(1)


FAULTS = _Faults()


def _gpu_uuid(tray_id: str, gpu: int) -> str:
    return f"GPU-{tray_id}-g{gpu}"       # same uuid as observability (DCGM)


def _expire_replaced():
    """A HW replace (new tray serial) clears the faults of the old board."""
    for f in FAULTS.faults.values():
        if f["state"] != "active":
            continue
        t = STORE.trays.get(f["tray_id"])
        if t is not None and t.serial != f["serial"]:
            _clear(f, "hw_replaced")


def active(tray_id: Optional[str] = None) -> List[dict]:
    with STORE.lock:
        FAULTS.sync()
        _expire_replaced()
        return [f for f in FAULTS.faults.values() if f["state"] == "active"
                and (tray_id is None or f["tray_id"] == tray_id)]


def gpu_map() -> Dict[str, dict]:
    """uuid → strongest active fault (one pass, for the telemetry tick)."""
    rank = {"xid": 3, "nvlink": 2, "thermal": 1}
    out: Dict[str, dict] = {}
    for f in active():
        cur = out.get(f["gpu_uuid"])
        if cur is None or rank[f["kind"]] > rank[cur["kind"]]:
            out[f["gpu_uuid"]] = f
    return out


def gpu_state(uuid: str) -> Optional[dict]:
    """Strongest active fault on a GPU (observability / dynamo hook)."""
    hit = [f for f in active() if f["gpu_uuid"] == uuid]
    if not hit:
        return None
    rank = {"xid": 3, "nvlink": 2, "thermal": 1}
    return max(hit, key=lambda f: rank[f["kind"]])


def _tray_health(tray_id: str):
    tray = STORE.trays.get(tray_id)
    if tray is None:
        return
    fs = [f for f in FAULTS.faults.values()
          if f["state"] == "active" and f["tray_id"] == tray_id
          and f["kind"] != "thermal"]
    if any(not f["reset_recovers"] for f in fs):
        tray.health = "critical"
    elif fs:
        tray.health = "warning" if tray.health != "critical" else "critical"
    elif tray.health in ("warning", "critical"):
        tray.health = "ok"


def _action(f: dict) -> str:
    if f["kind"] == "thermal":
        return "monitor"
    return "gpu_reset" if f["reset_recovers"] else "replace"


def _view(f: dict) -> dict:
    return {**{k: v for k, v in f.items() if k != "serial"},
            "recommended_action": _action(f)}


def _publish(f: dict, type_: str, severity: str):
    topic = "thermal" if f["kind"] == "thermal" else "health"
    exchange.publish(topic, type_, severity, f["tray_id"], {
        "fault_id": f["id"], "kind": f["kind"], "xid": f.get("xid"),
        "gpu_index": f["gpu"], "gpu_uuid": f["gpu_uuid"],
        "tray_id": f["tray_id"], "machine_id": f["tray_id"],
        "rack_id": f["rack_id"], "reset_recovers": f["reset_recovers"],
        "recommended_action": _action(f), "state": f["state"],
        "@odata.id": f"/redfish/v1/Systems/{f['tray_id']}"},
        category="thermal" if f["kind"] == "thermal" else "health")


def _feed(f: dict, resolved: bool):
    if resolved:
        for e in reversed(STORE.faults):
            if e.get("fault_id") == f["id"] and not e["resolved"]:
                e["resolved"], e["resolved_at"] = True, _iso()
        return
    STORE.faults.append({
        "tray_id": f["tray_id"], "kind": f"gpu_{f['kind']}",
        "detail": f["detail"], "at": f["at"], "resolved": False,
        "resolved_at": None, "fault_id": f["id"]})


def _clear(f: dict, how: str):
    f["state"], f["cleared_at"], f["cleared_by"] = "cleared", _iso(), how
    _feed(f, True)
    if how != "hw_replaced":          # trayops owns health during a replace
        _tray_health(f["tray_id"])
    _publish(f, "GpuFaultCleared", "info")


def inject_fault(kind: str, tray_id: str, gpu: int = 0, xid: Optional[int] = None,
                 reset_recovers: Optional[bool] = None,
                 delta_c: Optional[float] = None) -> dict:
    with STORE.lock:
        FAULTS.sync()
        if kind not in KINDS:
            raise HTTPException(422, f"kind must be one of {list(KINDS)}")
        tray = STORE.trays.get(tray_id)
        if tray is None:
            raise HTTPException(404, f"tray {tray_id} not found")
        if not 0 <= gpu < GPU_PER_TRAY:
            raise HTTPException(422, f"gpu must be 0..{GPU_PER_TRAY - 1}")
        if sum(1 for f in FAULTS.faults.values() if f["state"] == "active") >= MAX_ACTIVE:
            raise HTTPException(409, f"too many active faults (max {MAX_ACTIVE})")
        if kind == "nvlink":
            xid = NVLINK_XID
        if kind == "xid":
            xid = 79 if xid is None else xid
            if xid not in XID_CATALOG:
                raise HTTPException(422, f"xid must be one of {sorted(XID_CATALOG)}")
        name, rec_default = XID_CATALOG.get(xid, ("", True))
        if kind == "thermal":
            d = THERMAL_DEFAULT_DELTA_C if delta_c is None else float(delta_c)
            if not 0 < d <= 60:
                raise HTTPException(422, "delta_c must be in (0, 60]")
            detail = f"GPU{gpu} thermal throttle (+{d:g} °C)"
            rec = False
        else:
            d = None
            rec = rec_default if reset_recovers is None else bool(reset_recovers)
            detail = (f"GPU{gpu} XID {xid} — {name}" if kind == "xid"
                      else f"GPU{gpu} NVLink down (XID {xid})")
        fid = f"gf-{next(FAULTS._ids):04d}"
        f = {"id": fid, "kind": kind, "tray_id": tray_id, "rack_id": tray.rack_id,
             "gpu": gpu, "gpu_uuid": _gpu_uuid(tray_id, gpu), "xid": xid,
             "xid_name": name or None, "delta_c": d, "reset_recovers": rec,
             "detail": detail, "state": "active", "at": _iso(),
             "cleared_at": None, "cleared_by": None, "serial": tray.serial}
        FAULTS.faults[fid] = f
        _tray_health(tray_id)
        _feed(f, False)
        try:
            from . import oob
            oob.sel_add(tray_id, "Warning" if kind == "thermal" else "Critical",
                        detail, "GPU_THERMAL" if kind == "thermal" else f"XID_{xid}",
                        "Temperature" if kind == "thermal" else "Processor")
        except Exception:
            pass
        STORE.event("warning" if kind == "thermal" else "critical",
                    "NeoCloudEmulator.1.0.GpuFault", [tray_id, detail])
        _publish(f, {"xid": "GpuXidError", "nvlink": "NvlinkDown",
                     "thermal": "GpuThermalThrottle"}[kind],
                 "warning" if kind == "thermal" else "critical")
        return _view(f)


def on_gpu_reset(tray_id: str, gpu_ids: List[int]) -> dict:
    """Called by Redfish Actions/GpuReset. Clears reset-recoverable XID /
    NVLink faults on the reset GPUs; reports the ones that survive."""
    with STORE.lock:
        cleared, remaining = [], []
        for f in active(tray_id):
            if f["gpu"] not in gpu_ids or f["kind"] == "thermal":
                continue
            if f["reset_recovers"]:
                _clear(f, "gpu_reset")
                cleared.append(f["id"])
            else:
                remaining.append(f["id"])
        return {"cleared": cleared, "remaining": remaining}


# ── API ───────────────────────────────────────────────────────────────
class InjectBody(BaseModel):
    kind: str
    tray_id: str
    gpu: int = 0
    xid: Optional[int] = None
    reset_recovers: Optional[bool] = None
    delta_c: Optional[float] = None


class RecoverBody(BaseModel):
    id: Optional[str] = None
    tray_id: Optional[str] = None


@router.get("")
def list_faults(state: Optional[str] = None, tray_id: Optional[str] = None,
                rack_id: Optional[str] = None):
    with STORE.lock:
        active()                          # sync + expire replaced boards
        fs = [_view(f) for f in FAULTS.faults.values()
              if (not state or f["state"] == state)
              and (not tray_id or f["tray_id"] == tray_id)
              and (not rack_id or f["rack_id"] == rack_id)]
        return {"count": len(fs), "active": sum(f["state"] == "active" for f in fs),
                "kinds": list(KINDS),
                "xid_catalog": {str(k): {"name": v[0], "reset_recovers": v[1]}
                                for k, v in XID_CATALOG.items()},
                "faults": fs}


@router.post("/inject", status_code=201)
def inject(body: InjectBody):
    out = inject_fault(body.kind, body.tray_id, body.gpu, body.xid,
                       body.reset_recovers, body.delta_c)
    _converge()
    return out


def _converge():
    from . import observability as obs     # lazy: obs imports this module
    obs.ENGINE.tick(force=True)            # DCGM reflects the change at once


@router.post("/recover")
def recover(body: Optional[RecoverBody] = None):
    body = body or RecoverBody()
    with STORE.lock:
        hit = [f for f in active()
               if (not body.id or f["id"] == body.id)
               and (not body.tray_id or f["tray_id"] == body.tray_id)]
        if body.id and not hit:
            raise HTTPException(404, f"no active fault {body.id}")
        for f in hit:
            _clear(f, "operator")
    _converge()
    return {"cleared": [f["id"] for f in hit]}


# ── scenarios (what F10 auto-remediation should do with each) ─────────
SCENARIOS = {
    "xid-recoverable": {
        "description": "XID 63 row-remap pending on GPU0 — a GPU reset recovers it",
        "faults": [{"kind": "xid", "xid": 63, "gpu": 0}],
        "expected": ["cordon", "drain", "gpu_reset", "revalidate", "uncordon"],
        "expected_outcome": "recovered"},
    "xid-fatal": {
        "description": "XID 79 GPU fallen off the bus — reset fails, node is replaced + RMA",
        "faults": [{"kind": "xid", "xid": 79, "gpu": 1}],
        "expected": ["cordon", "drain", "gpu_reset(fails)", "replace", "rma"],
        "expected_outcome": "replaced"},
    "nvlink-down": {
        "description": "NVLink error (XID 74) on GPU2 — decode TP degraded, reset recovers",
        "faults": [{"kind": "nvlink", "gpu": 2}],
        "expected": ["cordon", "drain", "gpu_reset", "revalidate", "uncordon"],
        "expected_outcome": "recovered"},
    "thermal-throttle": {
        "description": "GPU3 +20 °C thermal throttle — perf drops, monitor only",
        "faults": [{"kind": "thermal", "gpu": 3, "delta_c": 20.0}],
        "expected": ["monitor"],
        "expected_outcome": "monitor"},
    "same-rack-pair": {
        "description": "XID 48 on two trays of one rack — tests max_parallel_per_rack",
        "faults": [{"kind": "xid", "xid": 48, "gpu": 0},
                   {"kind": "xid", "xid": 48, "gpu": 0, "next_tray": True}],
        "expected": ["run 1 starts", "run 2 waits (max_parallel_per_rack=1)"],
        "expected_outcome": "recovered"},
}


class ScenarioBody(BaseModel):
    tray_id: Optional[str] = None


def _default_tray() -> str:
    """A tray that hosts a Dynamo decode worker (impact visible), else the
    first tray of the twin."""
    try:
        from . import dynamo
        t = dynamo.decode_trays()
        if t:
            return t[0]
    except Exception:
        pass
    return next(iter(STORE.trays))


@router.get("/scenarios")
def scenarios():
    return {"scenarios": [{"name": k, **v} for k, v in SCENARIOS.items()]}


@router.post("/scenarios/{name}", status_code=201)
def run_scenario(name: str, body: Optional[ScenarioBody] = None):
    sc = SCENARIOS.get(name)
    if sc is None:
        raise HTTPException(404, f"unknown scenario {name}; one of {list(SCENARIOS)}")
    with STORE.lock:
        tray_id = (body.tray_id if body and body.tray_id else _default_tray())
        if tray_id not in STORE.trays:
            raise HTTPException(404, f"tray {tray_id} not found")
        rack = STORE.racks[STORE.trays[tray_id].rack_id]
        out = []
        for spec in sc["faults"]:
            tid = tray_id
            if spec.get("next_tray"):
                i = rack.trays.index(tray_id)
                tid = rack.trays[(i + 1) % len(rack.trays)]
            out.append(inject_fault(spec["kind"], tid, spec.get("gpu", 0),
                                    spec.get("xid"), None, spec.get("delta_c")))
    _converge()
    return {"scenario": name, "description": sc["description"],
            "expected": sc["expected"], "expected_outcome": sc["expected_outcome"],
            "faults": out}


@router.get("/{fault_id}")
def get_fault(fault_id: str):
    with STORE.lock:
        active()
        f = FAULTS.faults.get(fault_id)
        if f is None:
            raise HTTPException(404, f"fault {fault_id} not found")
        return _view(f)
