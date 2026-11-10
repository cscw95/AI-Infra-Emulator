"""AI Infra Emulator — FastAPI app entrypoint (standalone, port 9100)."""
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .store import STORE, RACK_ID, COMPUTE_TRAYS, GPU_PER_TRAY
from . import dpu, redfish, provisioning, fabric
from . import ufm, netq
from . import vast, converged, dell
from . import observability
from . import trayops
from . import exchange, dps, oob, redfish_oob
from . import security
from . import dynamo, dynamo_frontend, gpufaults
from . import flex
from . import bringup
from . import prom_metrics  # F26 Prometheus exposition

STATIC = Path(__file__).parent.parent / "static"

app = FastAPI(title="AI Infra Emulator", version=__version__,
              description="AI Infra Emulator — Vera Rubin NVL72 physical digital twin "
                          "(Compute/DCGM, VAST storage, UFM/IB, NetQ/Converged, DLC, DPU, BMC). "
                          "Driven by the NICo Emulator control plane.")

# NeoCloud OS control-plane (nocp :8000) + consoles (:8090) may call this
# service. Origins via EMU_CORS_ORIGINS; optional API key / Redfish Basic and
# request limits via app.security (all off by default).
app.add_middleware(security.SecurityMiddleware)

security.add_cors(app)

app.include_router(dpu.router)
app.include_router(redfish.router)
app.include_router(redfish_oob.router)
app.include_router(provisioning.router)
app.include_router(fabric.router)
app.include_router(observability.router)
app.include_router(ufm.router)
app.include_router(netq.router)
app.include_router(vast.router)
app.include_router(dell.router)
app.include_router(converged.router)
app.include_router(trayops.router)
app.include_router(dps.router)
app.include_router(oob.router)
app.include_router(exchange.router)
app.include_router(dynamo.router)
app.include_router(dynamo.k8s)
app.include_router(dynamo_frontend.router)
app.include_router(dynamo.kvbm.router)
app.include_router(gpufaults.router)
app.include_router(flex.router)
app.include_router(bringup.cm_router)
app.include_router(bringup.rms_router)
app.include_router(bringup.ztp_router)
app.include_router(prom_metrics.router)   # F26 /dcgm-exporter/{host}/metrics


@app.get("/versions")
@app.get("/emulator/v1/versions")
def versions():
    """DSX OS baseline version matrix (same payload in every NeoCloud repo)."""
    from . import DSX_VERSIONS
    return {**DSX_VERSIONS, "component": "ai-infra-emulator",
            "component_version": __version__, "hw_vendor": dell.HW_VENDOR,
            "hw_platform": dell.COMPUTE_MODEL}


@app.get("/healthz")
def healthz():
    from . import EMULATED_NICO
    return {"status": "ok", "version": __version__, "rack": RACK_ID,
            "compute_trays": len(STORE.trays), "dpus": len(STORE.dpus),
            "emulated_nico": EMULATED_NICO}


@app.get("/emulator/v1/twin")
def twin():
    with STORE.lock:
        return {
            "rack_id": RACK_ID, "model": "Vera Rubin NVL72",
            "racks": len(STORE.racks), "sites": len({r.site for r in STORE.racks.values()}),
            "compute_trays": len(STORE.trays), "gpu_per_tray": GPU_PER_TRAY,
            "gpus": len(STORE.trays) * GPU_PER_TRAY, "dpus": len(STORE.dpus),
            "tenant_networks": len(STORE.tenant_networks),
            "attachments": len(STORE.attachments),
            "tenants": sorted({a["tenant_id"] for a in STORE.attachments.values()}),
        }


@app.get("/emulator/v1/cluster")
def cluster():
    """Cluster overview — every rack, grouped by site/SU (light aggregation)."""
    with STORE.lock:
        sites = {}
        for r in STORE.racks.values():
            s = sites.setdefault(r.site_id, {
                "site_id": r.site_id, "site": r.site, "racks": 0, "trays": 0,
                "gpus": 0, "sus": {}, "tenants": set()})
            summ = STORE.rack_summary(r)
            s["racks"] += 1; s["trays"] += summ["trays"]; s["gpus"] += summ["gpus"]
            s["tenants"].update(summ["tenants"])
            su = s["sus"].setdefault(r.su_id, {"su_id": r.su_id, "racks": 0})
            su["racks"] += 1
        out = []
        for s in sites.values():
            s["tenants"] = sorted(s["tenants"])
            s["sus"] = sorted(s["sus"].values(),
                              key=lambda x: int(x["su_id"].split("-")[1]))
            out.append(s)
        return {"model": "Vera Rubin NVL72", "racks": len(STORE.racks),
                "trays": len(STORE.trays), "gpus": len(STORE.trays) * GPU_PER_TRAY,
                "dpus": len(STORE.dpus), "sites": out}


@app.get("/emulator/v1/cluster/racks")
def cluster_racks(site: Optional[str] = None, su: Optional[str] = None,
                  q: Optional[str] = None, offset: int = 0, limit: int = 500):
    """All racks with a light summary (filterable by site/SU/search)."""
    with STORE.lock:
        rs = list(STORE.racks.values())
        if site:
            rs = [r for r in rs if r.site_id == site or r.site == site]
        if su:
            rs = [r for r in rs if r.su_id == su]
        if q:
            rs = [r for r in rs if q in r.rack_id]
        total = len(rs)
        page = [STORE.rack_summary(r) for r in rs[offset:offset + limit]]
        return {"total": total, "offset": offset, "limit": limit, "racks": page}


@app.get("/emulator/v1/cluster/racks/{rack_id}")
def cluster_rack(rack_id: str):
    """One rack in detail — its 18 trays (power/health/lifecycle) + DPUs."""
    with STORE.lock:
        r = STORE.racks.get(rack_id)
        if not r:
            raise HTTPException(404, f"rack {rack_id} not found")
        trays = []
        for tid in r.trays:
            t = STORE.trays[tid]
            d = STORE.dpus.get(t.dpu_id)
            trays.append({
                "tray_id": tid, "power_state": STORE.power_state(t),
                "health": t.health, "lifecycle_state": t.lifecycle_state,
                "boot_stage": t.boot_stage, "gpus": t.gpus, "bmc_ip": t.bmc_ip,
                "dpu_id": t.dpu_id,
                "dpu_health": d.health if d else None,
                "dpu_mode": d.operating_mode if d else None,
                "tenants": sorted({f.tenant_id for f in d.functions.values()
                                   if f.tenant_id}) if d else []})
        return {**STORE.rack_summary(r), "tray_detail": trays}


@app.get("/emulator/v1/events")
def events(limit: int = 50):
    with STORE.lock:
        return list(STORE.events)[-limit:][::-1]


@app.post("/emulator/v1/reset")
def reset():
    STORE.reset()
    bringup.reset()
    return {"status": "reset", "compute_trays": len(STORE.trays)}


@app.get("/metrics")
def metrics(request: Request, rack: Optional[str] = None, su: Optional[str] = None,
            site: Optional[str] = None, collect: Optional[str] = None):
    """Prometheus exposition (F26): DCGM GPU · rack power · CDU · DPU — app/prom_metrics.py."""
    return prom_metrics.metrics_response(request, rack, su, site, collect)


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
def dashboard():
    idx = STATIC / "index.html"
    if idx.exists():
        return FileResponse(str(idx))
    return {"service": "NICo Emulator", "version": __version__,
            "docs": "/docs", "twin": "/emulator/v1/twin"}
