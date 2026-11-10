"""F26 AI-Infra `GET /metrics` — DCGM GPU · rack power (DPS) · CDU · DPU.

Exposition per `app/prom.py` (Prometheus text 0.0.4; OpenMetrics 1.0 via Accept).

GPU series use the dcgm-exporter names and label set of
NVIDIA/dcgm-exporter@4e663a526c46 `etc/default-counters.csv` (vendored in
`ref/upstream/dcgm-exporter/`; Apache-2.0) and its collector label order
(`internal/pkg/collector/base_collector.go`: gpu, UUID, pci_bus_id, device,
modelName, Hostname) plus NeoCloud topology labels rack/su/site. The CSV declares
`DCGM_FI_DEV_PCIE_REPLAY_TOTAL` and `DCGM_FI_DEV_GPU_ENERGY_JOULES_TOTAL` as
counters, but `promtool check metrics` rejects the upper-case `_TOTAL` suffix for
a counter; the names are kept and the type is exposed as `untyped`.

A real fleet runs one dcgm-exporter per node (:9400/metrics). Here:
  GET /metrics                       rack · domain · CDU · DPU + fleet GPU summary;
                                     per-GPU DCGM series with ?rack= / ?su= / ?site=,
                                     or the whole fleet with ?collect=dcgm
  GET /dcgm-exporter/{host}/metrics  one node (compute tray) = 4 GPUs, as the
                                     per-node exporter would answer (upstream label
                                     spelling UUID/modelName/Hostname, which promtool's
                                     camelCase lint flags exactly as it does upstream)

Rack power comes from the DPS allocator (app/dps.py: measured_kw = Redfish
PowerSubsystem reading), numbers from app/power_model.py (227/187 kW).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from . import __version__, dps, observability as obs, power_model as pm
from .prom import CONTENT_TYPE_OM, CONTENT_TYPE_TEXT, Registry, sanitize, wants_openmetrics
from .store import STORE

router = APIRouter(tags=["observability"])

GPU_MODEL_NAME = "NVIDIA Rubin"
GIB = 1024 ** 3

# (name, type, help, getter(gpu record) → value | None)
DCGM_FIELDS = (
    ("DCGM_FI_DEV_SM_CLOCK_HERTZ", "gauge", "SM clock frequency in hertz.",
     lambda g: None if g["sm_clock_mhz"] is None else g["sm_clock_mhz"] * 1e6),
    ("DCGM_FI_DEV_MEMORY_TEMP_CELSIUS", "gauge", "Memory temperature (in C).",
     lambda g: g["mem_temp_c"]),
    ("DCGM_FI_DEV_GPU_TEMP_CELSIUS", "gauge", "GPU temperature (in C).", lambda g: g["temp_c"]),
    ("DCGM_FI_DEV_BOARD_POWER_WATTS", "gauge", "Power draw (in W).", lambda g: g["power_w"]),
    ("DCGM_FI_DEV_PCIE_REPLAY_TOTAL", "untyped", "Total number of PCIe retries.",
     lambda g: g["pcie_replay"]),
    ("DCGM_FI_DEV_GPU_UTIL_RATIO", "gauge", "GPU utilization as a ratio.",
     lambda g: None if g["util_pct"] is None else round(g["util_pct"] / 100.0, 4)),
    ("DCGM_FI_DEV_XID_ERROR", "gauge", "Value of the last XID error encountered.",
     lambda g: _last_xid(g)),
    ("DCGM_FI_DEV_FB_CAPACITY_BYTES", "gauge", "Total framebuffer capacity in bytes.",
     lambda g: int(g["mem_total_gb"] * GIB)),
    ("DCGM_FI_DEV_FB_USED_BYTES", "gauge", "Used framebuffer memory in bytes.",
     lambda g: None if g["mem_used_gb"] is None else int(g["mem_used_gb"] * GIB)),
    ("DCGM_FI_DEV_FB_FREE_BYTES", "gauge", "Free framebuffer memory in bytes.",
     lambda g: None if g["mem_used_gb"] is None
     else int((g["mem_total_gb"] - g["mem_used_gb"]) * GIB)),
    ("DCGM_FI_DEV_NVLINK_THROUGHPUT_BYTES_PER_SECOND", "gauge",
     "Total NVLink throughput across all lanes in bytes per second.",
     lambda g: None if g["nvlink_tx_gbps"] is None
     else int((g["nvlink_tx_gbps"] + g["nvlink_rx_gbps"]) * 1e9 / 8)),
    ("DCGM_FI_DEV_ROW_REMAP_FAILED", "gauge", "Whether remapping of rows has failed",
     lambda g: 1 if g["ecc_uncorr"] and g["health"] == "critical" else 0),
    ("DCGM_FI_PROF_GR_ENGINE_UTIL_RATIO", "gauge", "Ratio of time the graphics engine is active.",
     lambda g: None if g["sm_util_pct"] is None else round(g["sm_util_pct"] / 100.0, 4)),
)


def _last_xid(g: dict) -> int:
    x = g.get("xid_recent") or []
    if not x:
        return 0
    last = x[-1]
    return int(last.get("xid", 0) if isinstance(last, dict) else last)


# promtool lint rejects camelCase label names; /metrics relabels the dcgm-exporter
# labels to snake_case (what a `labelmap` relabel rule would do). The per-node
# /dcgm-exporter/{host}/metrics keeps the upstream spelling.
SNAKE = {"UUID": "uuid", "modelName": "model_name", "Hostname": "hostname"}


def _dcgm_labels(g: dict, upstream: bool = False) -> dict:
    lab = {"gpu": str(g["idx"]), "UUID": g["gpu_uuid"],
           "pci_bus_id": f"00000000:{0x18 + 0x10 * int(g['idx']):02X}:00.0",
           "device": f"nvidia{g['idx']}", "modelName": GPU_MODEL_NAME,
           "Hostname": g["tray_id"], "rack": g["rack_id"], "su": g["su_id"],
           "site": g["site"]}
    return lab if upstream else {SNAKE.get(k, k): v for k, v in lab.items()}


def add_dcgm(reg: Registry, gpus: list[dict], upstream: bool = False) -> None:
    for name, mtype, help_, get in DCGM_FIELDS:
        f = reg.family(name, mtype, help_)
        for g in gpus:
            v = get(g)
            if v is not None:
                f.add(v, **_dcgm_labels(g, upstream))


def _gpu_summary(reg: Registry, gpus: list[dict]) -> None:
    f = reg.gauge("neocloud_gpus", "GPUs by site and state (active|idle|throttled|faulted|off).")
    c: dict[tuple, int] = {}
    pw: dict[str, float] = {}
    for g in gpus:
        c[(g["site"], g["state"])] = c.get((g["site"], g["state"]), 0) + 1
        pw[g["rack_id"]] = pw.get(g["rack_id"], 0.0) + float(g["power_w"] or 0)
    for (site, st), n in sorted(c.items()):
        f.add(n, site=site, state=st)
    p = reg.gauge("neocloud_rack_gpu_power_watts", "Sum of DCGM board power of a rack's GPUs.")
    for rid, w in sorted(pw.items()):
        p.add(round(w, 1), rack=rid)


def _racks(reg: Registry) -> None:
    recs = dps.all_rack_power()
    meas = reg.gauge("neocloud_rack_power_watts",
                     "Rack input power measured (Redfish PowerSubsystem / DPS).")
    cap = reg.gauge("neocloud_rack_power_limit_watts", "Rack power cap applied by DPS.")
    peak = reg.gauge("neocloud_rack_power_transient_watts", "Rack transient peak estimate.")
    thr = reg.gauge("neocloud_rack_power_throttled", "1 when DPS throttles the rack below demand.")
    for rid in sorted(recs):
        r = recs[rid]
        rack = STORE.racks.get(rid)
        lab = {"rack": rid, "su": getattr(rack, "su_id", None),
               "site": getattr(rack, "site_id", None), "profile": r.get("profile")}
        meas.add(round(float(r["measured_kw"]) * 1000, 1), **lab)
        cap.add(round(float(r["cap_kw"]) * 1000, 1), **lab)
        peak.add(round(float(r.get("transient_kw") or r["measured_kw"]) * 1000, 1), **lab)
        thr.add(1 if r.get("throttled") else 0, **lab)
    reg.gauge("neocloud_rack_rated_power_watts",
              "Rack rating (power_model.py): profile max-p | max-q.").add(
        pm.RACK_MAXP_KW * 1000, profile="max-p").add(pm.RACK_MAXQ_KW * 1000, profile="max-q")
    d_lim = reg.gauge("neocloud_power_domain_limit_watts", "DPS power-domain effective limit.")
    d_meas = reg.gauge("neocloud_power_domain_power_watts", "DPS power-domain measured power.")
    d_fx = reg.gauge("neocloud_power_domain_flex_active", "Active flex (grid) events on a domain.")
    for did in sorted(dps.DPS.domains):
        v = dps._domain_view(dps.DPS.domains[did])
        lab = {"domain": did, "site": v.get("site"), "mode": v.get("mode")}
        d_lim.add(round(float(v["effective_limit_kw"]) * 1000, 1), **lab)
        d_meas.add(round(float(v["measured_kw"]) * 1000, 1), **lab)
        d_fx.add(len(v.get("flex_active") or []), **lab)


def _cdus(reg: Registry) -> None:
    fams = {
        "heat": reg.gauge("neocloud_cdu_heat_watts", "CDU heat removed (liquid loop)."),
        "rated": reg.gauge("neocloud_cdu_rated_watts", "CDU rated capacity."),
        "supply": reg.gauge("neocloud_cdu_supply_temperature_celsius",
                            "CDU coolant supply temperature by loop."),
        "return": reg.gauge("neocloud_cdu_return_temperature_celsius",
                            "CDU coolant return temperature by loop."),
        "flow": reg.gauge("neocloud_cdu_flow_liters_per_minute", "CDU coolant flow by loop."),
        "level": reg.gauge("neocloud_cdu_coolant_level_ratio", "CDU reservoir level."),
        "leak": reg.gauge("neocloud_cdu_leak_detected", "1 when a CDU leak sensor is wet."),
        "health": reg.gauge("neocloud_cdu_health", "CDU health (1 = current state)."),
    }
    for cid in sorted(obs.ENGINE.cdus):
        v = obs.ENGINE.cdu_view(obs.ENGINE.cdus[cid])
        lab = {"cdu": cid, "su": v["su_id"], "site": v["site"], "model": v["model"]}
        fams["heat"].add(round(v["measured_heat_kw"] * 1000, 1), **lab)
        fams["rated"].add(v["rated_capacity_kw"] * 1000, **lab)
        for loop in ("primary", "secondary"):
            fams["supply"].add(v[loop]["supply_c"], loop=loop, **lab)
            fams["return"].add(v[loop]["return_c"], loop=loop, **lab)
            fams["flow"].add(v[loop]["flow_lpm"], loop=loop, **lab)
        fams["level"].add(round(v["coolant"]["level_pct"] / 100.0, 4), **lab)
        fams["leak"].add(1 if v["leak"]["detected"] else 0, **lab)
        fams["health"].add(1, state=v["health"], **lab)


def _dpus(reg: Registry) -> None:
    for d in sorted(STORE.dpus.values(), key=lambda d: d.dpu_id):
        for k, v in d.telemetry.items():
            name = sanitize(k)
            mtype = "counter" if name.endswith("_total") else "gauge"
            reg.family(name, mtype, f"BlueField DPU telemetry {name}.").add(v, dpu=d.dpu_id)


def collect(rack: Optional[str] = None, su: Optional[str] = None, site: Optional[str] = None,
            dcgm: bool = False) -> Registry:
    reg = Registry()
    reg.gauge("neocloud_aiinfra_build_info", "AI-Infra emulator build information.").add(
        1, version=__version__)
    obs.ENGINE.tick()
    with STORE.lock:
        gpus = list(obs.ENGINE._gpus)
        sel = gpus
        if rack:
            sel = [g for g in sel if g["rack_id"] == rack]
        if su:
            sel = [g for g in sel if g["su_id"] == su]
        if site:
            sel = [g for g in sel if g["site"] == site]
        if (rack or su or site) and not sel:
            raise HTTPException(404, "no GPUs match the filter")
        if dcgm or rack or su or site:
            add_dcgm(reg, sel)
        _gpu_summary(reg, gpus)
        _racks(reg)
        _cdus(reg)
        _dpus(reg)
    return reg


def _respond(request: Request, reg: Registry) -> Response:
    om = wants_openmetrics(request.headers.get("accept"))
    return Response(reg.render(openmetrics=om), media_type=CONTENT_TYPE_OM if om else CONTENT_TYPE_TEXT)


def metrics_response(request: Request, rack: Optional[str] = None, su: Optional[str] = None,
                     site: Optional[str] = None, collect_: Optional[str] = None) -> Response:
    return _respond(request, collect(rack, su, site, dcgm=(collect_ == "dcgm")))


@router.get("/dcgm-exporter/{host}/metrics")
def node_exporter(host: str, request: Request) -> Response:
    """One node's dcgm-exporter view (compute tray = 4 GPUs)."""
    obs.ENGINE.tick()
    with STORE.lock:
        sel = [g for g in obs.ENGINE._gpus if g["tray_id"] == host]
    if not sel:
        raise HTTPException(404, f"host '{host}' not found")
    reg = Registry()
    add_dcgm(reg, sel, upstream=True)
    return _respond(request, reg)
