"""DPS (Dynamic Power Software) / DSX MaxLPS emulation — nvidia.dcpower.v1 shape.

Power hierarchy: PowerDomain (facility OperatingLimit) > ResourceGroup >
Rack > Tray > GPU. Each site is seeded as one PowerDomain whose resource
groups are its SUs. Allocation modes:
  static  — every rack gets an equal fixed share of the domain limit
  dynamic — rack power steering: racks get their demand (by group priority),
            leftover headroom is redistributed up to each rack's peak
The resulting rack cap is the single power-cap source: mutations (PATCH
domain, profile, flex event, Redfish PowerLimit) push it into the
observability rack control state so DCGM/DCIM telemetry reflects it.

WPPS workload power profiles trade power for performance (perf/W);
DSX Flex demand-response events temporarily lower a domain's limit.

  GET|POST  /dps/v1/power-domains        GET|PATCH /dps/v1/power-domains/{id}
  GET|POST  /dps/v1/resource-groups      GET /dps/v1/profiles
  POST      /dps/v1/racks/{rack}/profile GET /dps/v1/telemetry?domain=
  POST      /dps/v1/simulate             GET|POST /dps/v1/flex-events"""
import itertools
import math
import time
import zlib
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .store import STORE, GPU_PER_TRAY, _iso
from . import exchange
from . import observability as obs

router = APIRouter(prefix="/dps/v1", tags=["dps-maxlps"])

# ── power model (Assumption values, consistent with observability) ────
GPU_TDP_W = obs.GPU_POWER_LIMIT_W          # 1300 W board power at max-p
RACK_OVERHEAD_KW = obs.RACK_OVERHEAD_KW    # NVSwitch/CPU/board/fans at full load
GPU_IDLE_FRAC = 0.09                       # idle GPU power as fraction of TDP
STANDBY_KW = 1.5                           # rack powered off (BMC/DPU rails)
TOKENS_PER_GPU_S = 1000.0                  # relative throughput unit at max-p, util 1.0

PROFILES = {
    "max-p": {"power_frac": 1.00, "perf_frac": 1.00,
              "description": "Maximum performance — full board power (default)"},
    "training": {"power_frac": 0.92, "perf_frac": 0.97,
                 "description": "WPPS training — clocks tuned for sustained GEMM"},
    "balanced": {"power_frac": 0.85, "perf_frac": 0.93,
                 "description": "WPPS balanced — best perf/W for mixed fleets"},
    "inference": {"power_frac": 0.75, "perf_frac": 0.88,
                  "description": "WPPS inference — memory-bound decode, lower clocks"},
    "max-q": {"power_frac": 0.68, "perf_frac": 0.82,
              "description": "Maximum efficiency — lowest power per token"},
}
# workload class → average utilisation and transient peak factor
WORKLOADS = {
    "training": {"util": 0.95, "peak": 1.08},
    "inference": {"util": 0.62, "peak": 1.30},
    "idle": {"util": 0.10, "peak": 1.00},
}
# observability rack workload profile → DPS workload class
_OBS_PROFILE = {"idle": "idle", "steady": "inference",
                "train": "training", "burst": "training"}
SMOOTHING_CLIP = 0.75          # power smoothing removes 75% of transient spikes
FLEX_MAX_DURATION_S = 24 * 3600
# Input bounds (DoS / nonsense guards): 1 GW site ceiling, NVL576 rack ceiling,
# object-count caps for the in-memory store.
MAX_LIMIT_KW = 1_000_000.0
MAX_GPUS_PER_RACK = 576
MAX_DOMAINS = 256
MAX_GROUPS = 1024
MAX_FLEX_ACTIVE = 256


def _bounded_kw(v, name: str, allow_zero: bool = False) -> float:
    """Reject NaN/inf and values outside (0|>=0)..MAX_LIMIT_KW."""
    if v is None or not math.isfinite(v) or v > MAX_LIMIT_KW or \
            (v < 0 if allow_zero else v <= 0):
        raise HTTPException(422, f"{name} must be {'>= 0' if allow_zero else '> 0'} "
                                 f"and <= {MAX_LIMIT_KW:g} kW")
    return float(v)


def rack_peak_kw(n_gpu: int, profile: str = "max-p") -> float:
    return n_gpu * GPU_TDP_W * PROFILES[profile]["power_frac"] / 1000.0 \
        + RACK_OVERHEAD_KW


def _demand_kw(n_gpu: int, profile: str, util: float) -> float:
    pf = PROFILES[profile]["power_frac"]
    gpu_w = GPU_TDP_W * pf * max(GPU_IDLE_FRAC, util)
    return n_gpu * gpu_w / 1000.0 + RACK_OVERHEAD_KW * (0.55 + 0.45 * util)


# ── state ─────────────────────────────────────────────────────────────
class _Dps:
    def __init__(self):
        self.seed_gen = -1
        self.domains: Dict[str, dict] = {}
        self.groups: Dict[str, dict] = {}
        self.rack_profile: Dict[str, str] = {}
        self.manual_cap: Dict[str, float] = {}     # rack → kW (Redfish/operator)
        self.tray_limit_w: Dict[str, float] = {}   # tray → W (Redfish tray PowerLimit)
        self.flex: Dict[str, dict] = {}
        self._ids = itertools.count(1)

    def sync(self):
        cur = getattr(STORE, "seed_gen", 0)
        if cur == self.seed_gen:
            return
        self.seed_gen = cur
        self.domains, self.groups, self.flex = {}, {}, {}
        self.rack_profile, self.manual_cap, self.tray_limit_w = {}, {}, {}
        sites: Dict[str, dict] = {}
        for r in STORE.racks.values():
            s = sites.setdefault(r.site_id, {"name": r.site, "sus": {}})
            s["sus"].setdefault(r.su_id, []).append(r.rack_id)
        for site_id, s in sites.items():
            did = f"pd-{site_id}"
            racks = [rid for su in s["sus"].values() for rid in su]
            peak = sum(rack_peak_kw(self.n_gpu(rid)) for rid in racks)
            self.domains[did] = {
                "id": did, "name": f"{s['name']} facility feed", "site": site_id,
                "operating_limit_kw": round(peak * 0.80, 1),   # 80% of nameplate
                "mode": "static", "smoothing": False,
                "created_at": _iso(), "updated_at": _iso()}
            for su_id, rids in s["sus"].items():
                gid = f"rg-{su_id}"
                self.groups[gid] = {"id": gid, "domain": did, "name": su_id,
                                    "racks": list(rids), "priority": 50,
                                    "min_kw": 0.0, "max_kw": None}

    def nid(self, prefix):
        return f"{prefix}-{next(self._ids):04d}"

    @staticmethod
    def n_gpu(rack_id: str) -> int:
        r = STORE.racks.get(rack_id)
        return len(r.trays) * GPU_PER_TRAY if r else 0

    def domain_racks(self, did: str) -> List[str]:
        return [rid for g in self.groups.values() if g["domain"] == did
                for rid in g["racks"]]

    def rack_domain(self, rack_id: str) -> Optional[str]:
        for g in self.groups.values():
            if rack_id in g["racks"]:
                return g["domain"]
        return None


DPS = _Dps()


def _domain_or_404(did: str) -> dict:
    DPS.sync()
    d = DPS.domains.get(did)
    if not d:
        raise HTTPException(404, f"power domain {did} not found")
    return d


def _flex_reduce(did: str) -> float:
    now = time.time()
    red = 0.0
    for f in DPS.flex.values():
        if f["domain"] != did:
            continue
        if f["status"] == "active" and now >= f["_end"]:
            f["status"] = "completed"
            exchange.publish("grid", "FlexEventCompleted", "info", did,
                             {"flex_event": f["id"], "reduce_kw": f["reduce_kw"]})
        if f["status"] == "active":
            red += f["reduce_kw"]
    return red


def effective_limit_kw(d: dict) -> float:
    return max(0.0, d["operating_limit_kw"] - _flex_reduce(d["id"]))


def _rack_workload(rack_id: str) -> str:
    ctl = obs.ENGINE.rack_ctrl.get(rack_id)
    if ctl and ctl.get("profile") in _OBS_PROFILE:
        return _OBS_PROFILE[ctl["profile"]]
    h = zlib.crc32(rack_id.encode()) % 10
    return "training" if h < 6 else "inference" if h < 9 else "idle"


def _rack_powered_off(rack_id: str) -> bool:
    ctl = obs.ENGINE.rack_ctrl.get(rack_id)
    if ctl and ctl.get("power") == "off":
        return True
    r = STORE.racks[rack_id]
    return all(STORE.power_state(STORE.trays[t]) == "Off" for t in r.trays)


def rack_demand(rack_id: str) -> dict:
    n_gpu = DPS.n_gpu(rack_id)
    profile = DPS.rack_profile.get(rack_id, "max-p")
    wl = _rack_workload(rack_id)
    if _rack_powered_off(rack_id):
        return {"profile": profile, "workload": "off", "util": 0.0,
                "demand_kw": STANDBY_KW, "peak_kw": rack_peak_kw(n_gpu, profile),
                "spike": 1.0}
    # slow deterministic wander (±4%) so telemetry is alive but test-stable
    phase = zlib.crc32(rack_id.encode()) % 628 / 100.0
    jitter = 1.0 + 0.04 * math.sin(time.time() / 90.0 + phase)
    util = min(1.0, WORKLOADS[wl]["util"] * jitter)
    return {"profile": profile, "workload": wl, "util": round(util, 3),
            "demand_kw": _demand_kw(n_gpu, profile, util),
            "peak_kw": rack_peak_kw(n_gpu, profile),
            "spike": WORKLOADS[wl]["peak"]}


def _manual_cap(rack_id: str) -> Optional[float]:
    """Operator caps set through the observability rack control (no
    cap_source) are adopted as manual caps; an operator uncap of a DPS-owned
    cap clears the manual cap."""
    ctl = obs.ENGINE.rack_ctrl.get(rack_id)
    if ctl:
        src = ctl.get("cap_source")
        if ctl.get("cap_kw") and not src:
            DPS.manual_cap[rack_id] = float(ctl["cap_kw"])
        elif ctl.get("cap_kw") is None and src in ("dps", "manual"):
            DPS.manual_cap.pop(rack_id, None)
            ctl.pop("cap_source", None)
    return DPS.manual_cap.get(rack_id)


def allocate(did: str) -> dict:
    """Compute caps for every rack of a domain. Returns rack_id → record."""
    d = DPS.domains[did]
    limit = effective_limit_kw(d)
    groups = [g for g in DPS.groups.values() if g["domain"] == did]
    recs: Dict[str, dict] = {}
    for g in groups:
        for rid in g["racks"]:
            rec = rack_demand(rid)
            rec["group"] = g["id"]
            rec["manual_cap_kw"] = _manual_cap(rid)
            ceiling = rec["peak_kw"]
            if rec["manual_cap_kw"]:
                ceiling = min(ceiling, rec["manual_cap_kw"])
            rec["ceiling_kw"] = ceiling
            recs[rid] = rec
    n = len(recs)
    if not n:
        return recs
    if d["mode"] == "static":
        share = limit / n
        for rec in recs.values():
            rec["cap_kw"] = min(rec["ceiling_kw"], share)
    else:
        # 1) floor: group min_kw split across its racks (bounded by ceiling)
        cap = {rid: 0.0 for rid in recs}
        for g in groups:
            if g["racks"] and g.get("min_kw"):
                per = g["min_kw"] / len(g["racks"])
                for rid in g["racks"]:
                    cap[rid] = min(recs[rid]["ceiling_kw"], per)
        left = max(0.0, limit - sum(cap.values()))
        # 2) demand, highest priority tier first; proportional inside a tier
        tiers = sorted({g["priority"] for g in groups}, reverse=True)
        for p in tiers:
            want = {}
            for g in groups:
                if g["priority"] != p:
                    continue
                g_room = (g["max_kw"] - sum(cap[r] for r in g["racks"])
                          if g.get("max_kw") else float("inf"))
                g_want = {r: max(0.0, min(recs[r]["demand_kw"],
                                          recs[r]["ceiling_kw"]) - cap[r])
                          for r in g["racks"]}
                tot = sum(g_want.values())
                if tot > g_room > 0:
                    g_want = {r: v * g_room / tot for r, v in g_want.items()}
                elif g_room <= 0:
                    g_want = {r: 0.0 for r in g_want}
                want.update(g_want)
            tot = sum(want.values())
            scale = min(1.0, left / tot) if tot else 0.0
            for r, v in want.items():
                cap[r] += v * scale
            left -= tot * scale
        # 3) leftover headroom steered to racks (burst room up to ceiling)
        for _ in range(3):
            room = {r: recs[r]["ceiling_kw"] - cap[r] for r in recs
                    if recs[r]["ceiling_kw"] - cap[r] > 1e-6}
            g_max = {g["id"]: g["max_kw"] for g in groups if g.get("max_kw")}
            for g in groups:
                if g["id"] in g_max:
                    g_left = g_max[g["id"]] - sum(cap[r] for r in g["racks"])
                    for r in g["racks"]:
                        if r in room and g_left <= 1e-6:
                            room.pop(r)
            tot = sum(room.values())
            if left <= 1e-6 or not tot:
                break
            give = min(left, tot)
            for r, v in room.items():
                cap[r] += v * give / tot
            left -= give
        for rid, rec in recs.items():
            rec["cap_kw"] = cap[rid]
    for rec in recs.values():
        rec["measured_kw"] = min(rec["demand_kw"], rec["cap_kw"])
        clip = SMOOTHING_CLIP if d["smoothing"] else 0.0
        rec["transient_kw"] = min(rec["cap_kw"], rec["measured_kw"] * (
            1 + (rec["spike"] - 1) * (1 - clip)))
        rec["headroom_kw"] = rec["cap_kw"] - rec["measured_kw"]
        rec["throttled"] = rec["demand_kw"] > rec["cap_kw"] + 1e-6
    return recs


def enforce(did: str) -> Dict[str, dict]:
    """Push the domain's rack caps into the rack control state (single
    power-cap source for DCGM/DCIM telemetry)."""
    obs.ENGINE._ensure_topology()
    recs = allocate(did)
    for rid, rec in recs.items():
        ctl = obs.ENGINE.rack_ctrl.setdefault(rid, obs._default_ctrl())
        ctl["cap_kw"] = round(rec["cap_kw"], 1)
        ctl["cap_source"] = ("manual" if rec["manual_cap_kw"] and
                             rec["cap_kw"] >= rec["manual_cap_kw"] - 0.05
                             else "dps")
    return recs


def rack_power(rack_id: str) -> Optional[dict]:
    """measured/cap for one rack (used by OOB + Redfish)."""
    DPS.sync()
    did = DPS.rack_domain(rack_id)
    if not did:
        return None
    return allocate(did).get(rack_id)


def all_rack_power() -> Dict[str, dict]:
    DPS.sync()
    out = {}
    for did in DPS.domains:
        out.update(allocate(did))
    return out


def tray_power(tray_id: str, rack_rec: Optional[dict] = None) -> dict:
    """Split a rack's measured/cap evenly across its trays, honouring any
    tray-level Redfish PowerLimit."""
    t = STORE.trays[tray_id]
    rec = rack_rec or rack_power(t.rack_id) or {}
    n = len(STORE.racks[t.rack_id].trays) or 1
    demand_w = rec.get("demand_kw", 0.0) * 1000.0 / n
    cap_w = rec.get("cap_kw", rec.get("demand_kw", 0.0)) * 1000.0 / n
    lim = DPS.tray_limit_w.get(tray_id)
    if lim:
        cap_w = min(cap_w, lim)
    if STORE.power_state(t) == "Off":
        demand_w = STANDBY_KW * 1000.0 / n
    measured_w = min(demand_w, cap_w)
    return {"demand_w": round(demand_w, 1), "cap_w": round(cap_w, 1),
            "measured_w": round(measured_w, 1), "tray_limit_w": lim,
            "gpu_cap_w": round(cap_w / max(1, t.gpus) * 0.88, 1),
            "gpu_measured_w": round(measured_w / max(1, t.gpus) * 0.88, 1)}


# ── views ─────────────────────────────────────────────────────────────
def _domain_view(d: dict, recs: Optional[dict] = None) -> dict:
    recs = recs if recs is not None else allocate(d["id"])
    racks = DPS.domain_racks(d["id"])
    eff = effective_limit_kw(d)
    measured = sum(r["measured_kw"] for r in recs.values())
    return {**d, "effective_limit_kw": round(eff, 1),
            "racks": len(racks), "gpus": sum(DPS.n_gpu(r) for r in racks),
            "resource_groups": [g["id"] for g in DPS.groups.values()
                                if g["domain"] == d["id"]],
            "measured_kw": round(measured, 1),
            "allocated_kw": round(sum(r["cap_kw"] for r in recs.values()), 1),
            "headroom_kw": round(eff - measured, 1),
            "throttled_racks": sum(1 for r in recs.values() if r["throttled"]),
            "flex_active": [f["id"] for f in DPS.flex.values()
                            if f["domain"] == d["id"] and f["status"] == "active"]}


def _rack_view(rid: str, rec: dict, gpus: bool = False) -> dict:
    out = {"rack_id": rid, "profile": rec["profile"], "workload": rec["workload"],
           "demand_kw": round(rec["demand_kw"], 1),
           "measured_kw": round(rec["measured_kw"], 1),
           "cap_kw": round(rec["cap_kw"], 1),
           "headroom_kw": round(rec["headroom_kw"], 1),
           "transient_peak_kw": round(rec["transient_kw"], 1),
           "nameplate_kw": round(rec["peak_kw"], 1),
           "manual_cap_kw": rec["manual_cap_kw"], "throttled": rec["throttled"]}
    n_gpu = DPS.n_gpu(rid)
    if n_gpu:
        it = max(0.0, rec["measured_kw"] - RACK_OVERHEAD_KW * 0.8)
        out["gpu"] = {"count": n_gpu,
                      "measured_w": round(it * 1000 / n_gpu, 1),
                      "cap_w": round(max(0.0, rec["cap_kw"] - RACK_OVERHEAD_KW * 0.8)
                                     * 1000 / n_gpu, 1)}
    if gpus:
        lst = []
        for tid in STORE.racks[rid].trays:
            tp = tray_power(tid, rec)
            for g in range(STORE.trays[tid].gpus):
                lst.append({"gpu_id": f"{tid}-gpu-{g}", "tray_id": tid,
                            "measured_w": tp["gpu_measured_w"],
                            "cap_w": tp["gpu_cap_w"],
                            "headroom_w": round(tp["gpu_cap_w"]
                                                - tp["gpu_measured_w"], 1)})
        out["gpus"] = lst
    return out


# ── models ────────────────────────────────────────────────────────────
class DomainCreate(BaseModel):
    name: str
    operating_limit_kw: float
    site: Optional[str] = None
    mode: str = "static"
    smoothing: bool = False
    racks: List[str] = []


class DomainPatch(BaseModel):
    name: Optional[str] = None
    operating_limit_kw: Optional[float] = None
    mode: Optional[str] = None
    smoothing: Optional[bool] = None


class GroupCreate(BaseModel):
    domain: str
    name: str
    racks: List[str]
    priority: int = 50
    min_kw: float = 0.0
    max_kw: Optional[float] = None


class ProfileBody(BaseModel):
    profile: str


class SimulateBody(BaseModel):
    domain: Optional[str] = None
    operating_limit_kw: Optional[float] = None
    workload_mix: Dict[str, float] = {}
    profile: str = "max-p"
    smoothing: Optional[bool] = None
    gpus_per_rack: Optional[int] = None


class FlexBody(BaseModel):
    domain: str
    reduce_kw: float
    duration_s: int = 900


def _check_mode(mode):
    if mode not in ("static", "dynamic"):
        raise HTTPException(422, "mode must be static|dynamic")


def _move_racks(racks: List[str], into: dict, same_domain: Optional[str]):
    for rid in racks:
        if rid not in STORE.racks:
            raise HTTPException(404, f"rack {rid} not found")
        if same_domain and DPS.rack_domain(rid) != same_domain:
            raise HTTPException(422, f"rack {rid} is not in domain {same_domain}")
    for g in DPS.groups.values():
        g["racks"] = [r for r in g["racks"] if r not in racks]
    into["racks"] = list(dict.fromkeys(racks))


# ── PowerDomain ───────────────────────────────────────────────────────
@router.get("/power-domains")
def list_domains():
    with STORE.lock:
        DPS.sync()
        return {"items": [_domain_view(d) for d in DPS.domains.values()]}


@router.post("/power-domains", status_code=201)
def create_domain(body: DomainCreate):
    with STORE.lock:
        DPS.sync()
        _check_mode(body.mode)
        _bounded_kw(body.operating_limit_kw, "operating_limit_kw")
        if len(DPS.domains) >= MAX_DOMAINS:
            raise HTTPException(503, f"power-domain limit ({MAX_DOMAINS}) reached")
        if len(body.name) > 128:
            raise HTTPException(422, "name longer than 128 characters")
        did = DPS.nid("pd")
        d = {"id": did, "name": body.name, "site": body.site,
             "operating_limit_kw": float(body.operating_limit_kw),
             "mode": body.mode, "smoothing": body.smoothing,
             "created_at": _iso(), "updated_at": _iso()}
        DPS.domains[did] = d
        if body.racks:
            g = {"id": f"rg-{did}-default", "domain": did, "name": "default",
                 "racks": [], "priority": 50, "min_kw": 0.0, "max_kw": None}
            _move_racks(body.racks, g, None)
            DPS.groups[g["id"]] = g
        exchange.publish("power", "PowerDomainCreated", "info", did,
                         {"operating_limit_kw": d["operating_limit_kw"],
                          "mode": d["mode"], "racks": len(body.racks)})
        return _domain_view(d)


@router.get("/power-domains/{did}")
def get_domain(did: str):
    with STORE.lock:
        d = _domain_or_404(did)
        recs = allocate(did)
        out = _domain_view(d, recs)
        out["groups"] = [{**g, "measured_kw": round(sum(
            recs[r]["measured_kw"] for r in g["racks"] if r in recs), 1)}
            for g in DPS.groups.values() if g["domain"] == did]
        return out


@router.patch("/power-domains/{did}")
def patch_domain(did: str, body: DomainPatch):
    with STORE.lock:
        d = _domain_or_404(did)
        if body.mode is not None:
            _check_mode(body.mode)
            d["mode"] = body.mode
        if body.operating_limit_kw is not None:
            _bounded_kw(body.operating_limit_kw, "operating_limit_kw")
            d["operating_limit_kw"] = float(body.operating_limit_kw)
        if body.smoothing is not None:
            d["smoothing"] = body.smoothing
        if body.name is not None:
            d["name"] = body.name
        d["updated_at"] = _iso()
        recs = enforce(did)
        exchange.publish("power", "PowerDomainUpdated", "info", did,
                         {"operating_limit_kw": d["operating_limit_kw"],
                          "mode": d["mode"], "smoothing": d["smoothing"]})
        return _domain_view(d, recs)


# ── ResourceGroup ─────────────────────────────────────────────────────
@router.get("/resource-groups")
def list_groups(domain: Optional[str] = None):
    with STORE.lock:
        DPS.sync()
        gs = [g for g in DPS.groups.values() if not domain or g["domain"] == domain]
        return {"items": gs}


@router.post("/resource-groups", status_code=201)
def create_group(body: GroupCreate):
    with STORE.lock:
        _domain_or_404(body.domain)
        if len(DPS.groups) >= MAX_GROUPS:
            raise HTTPException(503, f"resource-group limit ({MAX_GROUPS}) reached")
        _bounded_kw(body.min_kw, "min_kw", allow_zero=True)
        if body.max_kw is not None:
            _bounded_kw(body.max_kw, "max_kw")
        if not 0 <= body.priority <= 1000:
            raise HTTPException(422, "priority must be 0..1000")
        if body.max_kw is not None and body.max_kw < body.min_kw:
            raise HTTPException(422, "max_kw must be >= min_kw")
        gid = DPS.nid("rg")
        g = {"id": gid, "domain": body.domain, "name": body.name, "racks": [],
             "priority": body.priority, "min_kw": body.min_kw,
             "max_kw": body.max_kw}
        _move_racks(body.racks, g, body.domain)
        DPS.groups[gid] = g
        enforce(body.domain)
        exchange.publish("power", "ResourceGroupCreated", "info", gid,
                         {"domain": body.domain, "racks": g["racks"],
                          "priority": g["priority"]})
        return g


# ── WPPS profiles ─────────────────────────────────────────────────────
@router.get("/profiles")
def profiles():
    return {"items": [{"name": k, **v} for k, v in PROFILES.items()]}


@router.post("/racks/{rack_id}/profile")
def set_profile(rack_id: str, body: ProfileBody):
    with STORE.lock:
        DPS.sync()
        if rack_id not in STORE.racks:
            raise HTTPException(404, f"rack {rack_id} not found")
        if body.profile not in PROFILES:
            raise HTTPException(422, f"profile must be one of {list(PROFILES)}")
        DPS.rack_profile[rack_id] = body.profile
        did = DPS.rack_domain(rack_id)
        rec = enforce(did).get(rack_id) if did else None
        exchange.publish("power", "RackProfileApplied", "info", rack_id,
                         {"profile": body.profile, "domain": did})
        return {"rack_id": rack_id, "profile": body.profile, "domain": did,
                **({"power": _rack_view(rack_id, rec)} if rec else {})}


# ── telemetry ─────────────────────────────────────────────────────────
@router.get("/telemetry")
def telemetry(domain: Optional[str] = None, rack: Optional[str] = None,
              gpus: bool = False):
    with STORE.lock:
        DPS.sync()
        if domain:
            _domain_or_404(domain)
        if rack:
            if rack not in STORE.racks:
                raise HTTPException(404, f"rack {rack} not found")
            domain = DPS.rack_domain(rack)
        out = []
        for did, d in DPS.domains.items():
            if domain and did != domain:
                continue
            recs = allocate(did)
            groups = []
            for g in DPS.groups.values():
                if g["domain"] != did:
                    continue
                rs = [_rack_view(r, recs[r], gpus or bool(rack))
                      for r in g["racks"] if not rack or r == rack]
                if rack and not rs:
                    continue
                groups.append({
                    "group_id": g["id"], "name": g["name"],
                    "priority": g["priority"], "min_kw": g["min_kw"],
                    "max_kw": g["max_kw"],
                    "measured_kw": round(sum(r["measured_kw"] for r in rs), 1),
                    "cap_kw": round(sum(r["cap_kw"] for r in rs), 1),
                    "headroom_kw": round(sum(r["headroom_kw"] for r in rs), 1),
                    "racks": rs})
            view = _domain_view(d, recs)
            view["transient_peak_kw"] = round(
                sum(r["transient_kw"] for r in recs.values()), 1)
            view["groups"] = groups
            out.append(view)
        return {"at": _iso(), "domains": out}


# ── simulate (static vs dynamic) ──────────────────────────────────────
@router.post("/simulate")
def simulate(body: SimulateBody):
    with STORE.lock:
        DPS.sync()
        d = _domain_or_404(body.domain) if body.domain else None
        limit = body.operating_limit_kw or (d["operating_limit_kw"] if d else None)
        if not limit or limit <= 0:
            raise HTTPException(422, "operating_limit_kw (or a domain) is required")
        _bounded_kw(limit, "operating_limit_kw")
        if body.gpus_per_rack is not None and \
                not 1 <= body.gpus_per_rack <= MAX_GPUS_PER_RACK:
            raise HTTPException(422, f"gpus_per_rack must be 1..{MAX_GPUS_PER_RACK}")
        if len(body.workload_mix or {}) > 16 or any(
                not math.isfinite(float(v)) for v in (body.workload_mix or {}).values()):
            raise HTTPException(422, "workload_mix must hold finite weights")
        if body.profile not in PROFILES:
            raise HTTPException(422, f"profile must be one of {list(PROFILES)}")
        mix = {k: float(v) for k, v in (body.workload_mix or
               {"training": 0.6, "inference": 0.3, "idle": 0.1}).items()}
        bad = [k for k in mix if k not in WORKLOADS]
        if bad or any(v < 0 for v in mix.values()) or not sum(mix.values()):
            raise HTTPException(422, f"workload_mix keys must be {list(WORKLOADS)} "
                                     "with non-negative weights")
        tot = sum(mix.values())
        mix = {k: v / tot for k, v in mix.items()}
        smoothing = body.smoothing if body.smoothing is not None else \
            (d["smoothing"] if d else True)
        if body.gpus_per_rack:
            n_gpu = body.gpus_per_rack
        elif d and DPS.domain_racks(d["id"]):
            n_gpu = DPS.n_gpu(DPS.domain_racks(d["id"])[0])
        else:
            n_gpu = 72
        prof = PROFILES[body.profile]
        avg_kw = sum(w * _demand_kw(n_gpu, body.profile, WORKLOADS[k]["util"])
                     for k, w in mix.items())
        avg_util = sum(w * WORKLOADS[k]["util"] for k, w in mix.items())
        tok_gpu = TOKENS_PER_GPU_S * prof["perf_frac"] * avg_util

        def _case(rack_budget_kw, perf_factor):
            racks = int(limit // rack_budget_kw)
            draw = racks * avg_kw
            tokens = racks * n_gpu * tok_gpu * perf_factor
            return {"rack_budget_kw": round(rack_budget_kw, 1),
                    "racks_supported": racks, "gpus_supported": racks * n_gpu,
                    "expected_draw_kw": round(draw, 1),
                    "headroom_kw": round(limit - draw, 1),
                    "tokens_per_s": round(tokens),
                    "tokens_per_mw": round(tokens / (limit / 1000.0))}

        static = _case(rack_peak_kw(n_gpu, body.profile), 1.0)
        # dynamic: provision to the mix's expected draw + transient margin
        spike = sum(w * WORKLOADS[k]["peak"] for k, w in mix.items())
        margin = (spike - 1) * ((1 - SMOOTHING_CLIP) if smoothing else 1.0) + 0.05
        dyn_budget = min(rack_peak_kw(n_gpu, body.profile), avg_kw * (1 + margin))
        dynamic = _case(dyn_budget, 0.99)     # rare steering-induced capping

        def _pct(a, b):
            return round((a - b) / b * 100.0, 1) if b else None

        return {
            "inputs": {"domain": body.domain, "operating_limit_kw": limit,
                       "workload_mix": {k: round(v, 3) for k, v in mix.items()},
                       "profile": body.profile, "smoothing": smoothing,
                       "gpus_per_rack": n_gpu,
                       "racks_in_domain": len(DPS.domain_racks(d["id"])) if d else None},
            "static": static, "dynamic": dynamic,
            "delta": {"racks": dynamic["racks_supported"] - static["racks_supported"],
                      "gpus_pct": _pct(dynamic["gpus_supported"],
                                       static["gpus_supported"]),
                      "tokens_per_mw_pct": _pct(dynamic["tokens_per_mw"],
                                                static["tokens_per_mw"])}}


# ── DSX Flex demand response ──────────────────────────────────────────
def _flex_view(f: dict) -> dict:
    return {k: v for k, v in f.items() if not k.startswith("_")}


@router.get("/flex-events")
def list_flex(domain: Optional[str] = None):
    with STORE.lock:
        DPS.sync()
        for did in DPS.domains:
            _flex_reduce(did)                 # lazily expire finished events
        return {"items": [_flex_view(f) for f in DPS.flex.values()
                          if not domain or f["domain"] == domain]}


@router.post("/flex-events", status_code=201)
def create_flex(body: FlexBody):
    with STORE.lock:
        d = _domain_or_404(body.domain)
        if not math.isfinite(body.reduce_kw) or body.reduce_kw <= 0 or \
                body.reduce_kw >= effective_limit_kw(d):
            raise HTTPException(422, "reduce_kw must be > 0 and below the "
                                     "domain's effective limit")
        if not 0 < body.duration_s <= FLEX_MAX_DURATION_S:
            raise HTTPException(422, f"duration_s must be 1..{FLEX_MAX_DURATION_S}")
        if len(DPS.flex) >= MAX_FLEX_ACTIVE:
            _flex_reduce(body.domain)            # expire finished events first
            if len(DPS.flex) >= MAX_FLEX_ACTIVE:
                raise HTTPException(503, f"flex-event limit ({MAX_FLEX_ACTIVE}) reached")
        now = time.time()
        fid = DPS.nid("flex")
        f = {"id": fid, "domain": body.domain, "reduce_kw": float(body.reduce_kw),
             "duration_s": body.duration_s, "status": "active",
             "start": _iso(), "end": time.strftime(
                 "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + body.duration_s)),
             "_end": now + body.duration_s}
        DPS.flex[fid] = f
        recs = enforce(body.domain)
        exchange.publish("grid", "FlexEventStarted", "warning", body.domain,
                         {"flex_event": fid, "reduce_kw": f["reduce_kw"],
                          "duration_s": body.duration_s,
                          "effective_limit_kw": round(effective_limit_kw(d), 1)})
        return {**_flex_view(f), "domain_after": _domain_view(d, recs)}
