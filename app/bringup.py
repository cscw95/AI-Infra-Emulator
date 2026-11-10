"""Rack bring-up physical twin (F23/F24): Config Manager (switch ZTP + LLDP
cable validation) and RMS (RackManager service; gRPC in DSX, REST here).

  /config-manager/v1/switches                  GET   ?rack= (fleet leaves + staged racks)
  /config-manager/v1/switches/{id}/ztp         POST  Kea DHCP → config render → NVUE apply → validate
  /config-manager/v1/lldp-validate             POST  {rack, cable_plan:[{a, b}]} → {mismatches}
  /config-manager/v1/lldp-faults               POST/DELETE  emulator hook: physical miswire
  /rms/v1/racks/{id}/power                     POST  {state: On|Off|PowerCycle}
  /rms/v1/racks/{id}/inventory                 GET
  /rms/v1/racks/{id}/firmware                  POST  {bundle}  (order BMC → BF → GPU → CX → NVSwitch)

Upstream message shapes / flow (local copies under neocloud/ref/upstream,
commit not yet recorded in the pack):
  - NVIDIA nv-config-manager docs/api-specs/ztp.openapi.json (ZTP API 0.1.0):
    switch-side ZTP calls are served at /ztp/v1/device/{device_uuid}/
    validate_serial (POST {serial} → string) · boot-script (GET text/plain) ·
    config/{configlet} (GET text/plain) · firmware (GET) · provisioned (POST → string);
    the switch ZTP run walks them in that order (ONIE/Cumulus ZTP flow).
  - NVIDIA nv-rms fern/proto/rack_manager.proto (gRPC, Apache-2.0):
    SetPowerState{node_id, operation: PowerOperation, rack_id} → {status: ReturnCode};
    UpdateFirmware{rack_id, firmware_targets[{target, filename}], activate,
    force_update} → {status, message, job_id}. Exposed as REST JSON here (proto
    field names kept); the v2 contract {state} / {bundle} keys are accepted too.

A rack that is not part of the seeded fleet (a new rack under bring-up) is
"staged" on first touch: 18 Dell compute trays, 9 NVLink switch trays,
4 power shelves, 1 CDU, powered off, factory firmware. Observed LLDP equals the
cable plan unless a miswire is injected through ``/config-manager/v1/lldp-faults``.
In-memory only; ``reset()`` runs from the emulator reset.
"""
import hashlib
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import PlainTextResponse

from . import dell
from .store import (STORE, COMPUTE_TRAYS, GPU_PER_TRAY, NVLINK_SWITCH_TRAYS,
                    POWER_SHELVES, CDU_COUNT, _iso)

cm_router = APIRouter(prefix="/config-manager/v1", tags=["config-manager"])
rms_router = APIRouter(prefix="/rms/v1", tags=["rms"])
ztp_router = APIRouter(prefix="/ztp/v1", tags=["ztp"])

NOS = "Cumulus Linux 5.12 (NVUE)"
# factory firmware on a fresh rack; a bundle brings it to the fleet baseline
FACTORY_FW = {"bmc": dell.IDRAC_FW, "bf": "3.0.0", "gpu": "97.08.31",
              "cx": "40.43.1000", "nvswitch": "2.4.0"}
FW_ORDER = ["bmc", "bf", "gpu", "cx", "nvswitch"]     # BMC → BF → GPU first (F24)
POWER_STATES = {"on": "On", "off": "Off", "powercycle": "PowerCycle", "cycle": "PowerCycle"}
# rack_manager.proto PowerOperation → emulator state
POWER_OPERATIONS = {"POWER_OPERATION_ON": "On", "POWER_OPERATION_FORCE_ON": "On",
                    "POWER_OPERATION_OFF": "Off", "POWER_OPERATION_FORCE_OFF": "Off",
                    "POWER_OPERATION_GRACEFUL_SHUTDOWN": "Off",
                    "POWER_OPERATION_RESET": "PowerCycle",
                    "POWER_OPERATION_GRACEFUL_RESTART": "PowerCycle",
                    "POWER_OPERATION_FORCE_RESTART": "PowerCycle"}
ZTP_ORDER = ["validate_serial", "boot-script", "config", "firmware", "provisioned"]

STAGED: Dict[str, Dict[str, Any]] = {}
SWITCHES: Dict[str, Dict[str, Any]] = {}
LLDP_FAULTS: Dict[str, Dict[str, Optional[str]]] = {}   # rack -> {a: actual_b}


def reset() -> None:
    STAGED.clear(); SWITCHES.clear(); LLDP_FAULTS.clear()


def _serial(key: str) -> str:
    return "SN" + hashlib.sha256(key.encode()).hexdigest()[:10].upper()


def _stage(rack_id: str) -> Dict[str, Any]:
    """Staged (new) rack record; seeded fleet racks are never staged."""
    r = STAGED.get(rack_id)
    if r is None:
        r = {"rack_id": rack_id, "serial": _serial(rack_id), "power": "Off",
             "firmware": dict(FACTORY_FW), "staged_at": _iso()}
        STAGED[rack_id] = r
        for role, i in (("mgmt", 0), ("leaf", 0)):
            sid = f"{rack_id}-{role}-sw-{i}"
            SWITCHES[sid] = {"id": sid, "rack": rack_id, "role": role,
                             "model": dell.LEAF_PRODUCT if role == "leaf"
                             else "Dell PowerSwitch N3248TE-ON",
                             "serial": _serial(sid), "nos": NOS,
                             "state": "discovered", "mgmt_ip": None,
                             "config_hash": None, "ztp": [],
                             "device_uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"ztp/{sid}"))}
        STORE.event("info", "NeoCloudEmulator.1.0.RackStaged", [rack_id])
    return r


def _fleet_leaves(rack: Optional[str]) -> List[Dict[str, Any]]:
    """Seeded fleet leaves (NetQ twin), already configured; filtered by the rack's SU."""
    from . import netq
    netq.ENGINE._ensure_topology()
    su = STORE.racks[rack].su_id if rack in STORE.racks else None
    return [{"id": s["name"], "rack": None, "su_id": s["su_id"], "role": "leaf",
             "model": dell.LEAF_PRODUCT, "nos": NOS, "state": "configured",
             "source": "fleet"}
            for s in netq.ENGINE.switches.values()
            if s.get("role") == "leaf" and (not su or s.get("su_id") == su)]


# ── Config Manager ────────────────────────────────────────────────────
@cm_router.get("/switches")
def list_switches(rack: Optional[str] = None):
    with STORE.lock:
        if rack and rack not in STORE.racks:
            _stage(rack)
        staged = [dict(s, source="bringup") for s in SWITCHES.values()
                  if not rack or s["rack"] == rack]
        fleet = _fleet_leaves(rack) if (not rack or rack in STORE.racks) else []
        items = staged + fleet
        return {"switches": items, "count": len(items)}


@cm_router.post("/switches/{switch_id}/ztp")
def ztp(switch_id: str, body: Optional[Dict[str, Any]] = Body(None)):
    """Zero-touch provisioning: Kea DHCP lease (option 239 → ZTP script) →
    config render from the DCIM intent → NVUE apply → NetQ-style validate."""
    body = body or {}
    with STORE.lock:
        s = SWITCHES.get(switch_id)
        if not s:
            raise HTTPException(404, f"switch {switch_id} not found")
        n = len(SWITCHES)
        s["mgmt_ip"] = s["mgmt_ip"] or f"10.250.{n // 250}.{n % 250 + 10}"
        cfg = f"{switch_id}|{s['role']}|{body.get('intent', 'default')}"
        s["config_hash"] = hashlib.sha256(cfg.encode()).hexdigest()[:16]
        fail = body.get("fail")                      # emulator: force a stage failure
        stages = []
        for st in ZTP_ORDER:
            ok = st != fail
            stages.append({"stage": st, "status": "ok" if ok else "failed",
                           "call": _ZTP_CALLS[st].format(u=s["device_uuid"]), "at": _iso()})
            if not ok:
                break
        s["ztp"] = stages
        s["state"] = "configured" if not fail else "ztp_failed"
        STORE.event("info" if not fail else "warning",
                    "NeoCloudEmulator.1.0.SwitchZtp", [switch_id, s["state"]])
        if not fail:
            s["provisioned"] = True
        return {"id": switch_id, "device_uuid": s["device_uuid"], "state": s["state"],
                "mgmt_ip": s["mgmt_ip"],
                "nos": s["nos"], "config_hash": s["config_hash"], "stages": stages}


@cm_router.post("/lldp-validate")
def lldp_validate(body: Dict[str, Any] = Body(...)):
    """Compare the DCIM cable plan with LLDP neighbours seen on the wire."""
    rack = body.get("rack")
    plan = body.get("cable_plan") or []
    if not rack:
        raise HTTPException(400, "rack is required")
    if not isinstance(plan, list):
        raise HTTPException(400, "cable_plan must be a list of {a, b}")
    with STORE.lock:
        faults = LLDP_FAULTS.get(rack, {})
        mismatches, links = [], []
        for link in plan:
            a, b = (link or {}).get("a"), (link or {}).get("b")
            if not a or not b:
                raise HTTPException(400, "each cable_plan entry needs a and b")
            actual = faults[a] if a in faults else b
            links.append({"a": a, "b": actual})
            if actual != b:
                mismatches.append({"a": a, "expected_b": b, "actual_b": actual,
                                   "kind": "missing" if actual is None else "miswired"})
        STORE.event("warning" if mismatches else "info",
                    "NeoCloudEmulator.1.0.LldpValidation",
                    [rack, str(len(plan)), str(len(mismatches))])
        return {"rack": rack, "checked": len(plan),
                "matched": len(plan) - len(mismatches), "ok": not mismatches,
                "mismatches": mismatches, "observed": links, "at": _iso()}


@cm_router.post("/lldp-faults")
def inject_lldp_fault(body: Dict[str, Any] = Body(...)):
    """Emulator hook: port ``a`` is physically cabled to ``actual_b`` (None = unplugged)."""
    rack, a = body.get("rack"), body.get("a")
    if not rack or not a:
        raise HTTPException(400, "rack and a are required")
    with STORE.lock:
        LLDP_FAULTS.setdefault(rack, {})[a] = body.get("actual_b")
        return {"rack": rack, "faults": dict(LLDP_FAULTS[rack])}


@cm_router.delete("/lldp-faults")
def clear_lldp_faults(rack: Optional[str] = None):
    with STORE.lock:
        if rack:
            LLDP_FAULTS.pop(rack, None)
        else:
            LLDP_FAULTS.clear()
        return {"cleared": rack or "all"}


_ZTP_CALLS = {"validate_serial": "POST /ztp/v1/device/{u}/validate_serial",
              "boot-script": "GET /ztp/v1/device/{u}/boot-script",
              "config": "GET /ztp/v1/device/{u}/config/startup",
              "firmware": "GET /ztp/v1/device/{u}/firmware",
              "provisioned": "POST /ztp/v1/device/{u}/provisioned"}


def _device(device_uuid: str) -> Dict[str, Any]:
    s = next((x for x in SWITCHES.values() if x["device_uuid"] == device_uuid), None)
    if s is None:
        raise HTTPException(404, "Not Found")
    return s


@ztp_router.post("/device/{device_uuid}/validate_serial")
def ztp_validate_serial(device_uuid: str, body: Dict[str, Any] = Body(...)):
    if not isinstance(body.get("serial"), str):
        raise HTTPException(422, [{"loc": ["body", "serial"], "msg": "Field required",
                                   "type": "missing"}])
    with STORE.lock:
        s = _device(device_uuid)
        if body["serial"] != s["serial"]:
            raise HTTPException(404, "Not Found")
        return "OK"


@ztp_router.get("/device/{device_uuid}/boot-script", response_class=PlainTextResponse)
def ztp_boot_script(device_uuid: str):
    with STORE.lock:
        s = _device(device_uuid)
        return ("#!/bin/bash\n# CUMULUS-AUTOPROVISIONING\n"
                f"curl -fsS $ZTP/v1/device/{device_uuid}/config/startup | nv config patch -\n"
                "nv config apply -y\n"
                f"curl -fsS -X POST $ZTP/v1/device/{device_uuid}/provisioned\n"
                f"# switch {s['id']} role {s['role']}\n")


@ztp_router.get("/device/{device_uuid}/config/{configlet}", response_class=PlainTextResponse)
def ztp_config(device_uuid: str, configlet: str):
    with STORE.lock:
        s = _device(device_uuid)
        return (f"- set:\n    system:\n      hostname: {s['id']}\n"
                f"    interface:\n      eth0:\n        ip:\n          address:\n"
                f"            {s['mgmt_ip'] or 'dhcp'}: {{}}\n# configlet {configlet}\n")


@ztp_router.get("/device/{device_uuid}/firmware")
def ztp_firmware(device_uuid: str):
    with STORE.lock:
        s = _device(device_uuid)
        return {"platform": s["model"], "version": NOS, "filename": "cumulus-linux-5.12.bin"}


@ztp_router.post("/device/{device_uuid}/provisioned")
def ztp_provisioned(device_uuid: str):
    with STORE.lock:
        s = _device(device_uuid)
        s["provisioned"], s["state"] = True, "configured"
        return "OK"


# ── RMS ───────────────────────────────────────────────────────────────
def _fleet_inventory(rack) -> Dict[str, Any]:
    trays = [STORE.trays[t] for t in rack.trays]
    pw = {STORE.power_state(t) for t in trays}
    return {"rack_id": rack.rack_id, "source": "fleet", "model": rack.model,
            "serial": _serial(rack.rack_id),
            "power": "On" if pw == {"On"} else ("Off" if pw == {"Off"} else "Mixed"),
            "compute_trays": [{"id": t.tray_id, "model": dell.COMPUTE_MODEL,
                               "serial": t.serial, "gpus": t.gpus,
                               "power": STORE.power_state(t), "bmc_fw": dell.IDRAC_FW}
                              for t in trays],
            "nvlink_switch_trays": rack.nvlink_switch_trays,
            "power_shelves": rack.power_shelves, "cdus": rack.cdus,
            "firmware": None}


def _staged_inventory(r: Dict[str, Any]) -> Dict[str, Any]:
    rid = r["rack_id"]
    return {"rack_id": rid, "source": "bringup", "model": "Vera Rubin NVL72",
            "rack_model": dell.RACK_MODEL, "serial": r["serial"], "power": r["power"],
            "compute_trays": [{"id": f"{rid}-tray-{t:02d}", "model": dell.COMPUTE_MODEL,
                               "serial": _serial(f"{rid}-tray-{t:02d}"),
                               "gpus": GPU_PER_TRAY, "power": r["power"],
                               "bmc_fw": r["firmware"]["bmc"]}
                              for t in range(COMPUTE_TRAYS)],
            "nvlink_switch_trays": NVLINK_SWITCH_TRAYS,
            "power_shelves": POWER_SHELVES, "cdus": CDU_COUNT,
            "firmware": dict(r["firmware"])}


@rms_router.get("/racks/{rack_id}/inventory")
def rms_inventory(rack_id: str):
    with STORE.lock:
        rack = STORE.racks.get(rack_id)
        return _fleet_inventory(rack) if rack else _staged_inventory(_stage(rack_id))


@rms_router.post("/racks/{rack_id}/power")
def rms_power(rack_id: str, body: Dict[str, Any] = Body(...)):
    state = (POWER_OPERATIONS.get(str(body.get("operation", "")))
             or POWER_STATES.get(str(body.get("state", "")).lower()))
    if not state:
        raise HTTPException(400, f"unsupported power state {body.get('state') or body.get('operation')}")
    with STORE.lock:
        rack = STORE.racks.get(rack_id)
        if rack:
            rt = {"On": "On", "Off": "ForceOff", "PowerCycle": "ForceRestart"}[state]
            for tid in rack.trays:
                STORE.set_power(STORE.trays[tid], rt)
        else:
            _stage(rack_id)["power"] = "On" if state in ("On", "PowerCycle") else "Off"
        STORE.event("info", "NeoCloudEmulator.1.0.RmsRackPower", [rack_id, state])
        return {"status": "RETURN_CODE_SUCCESS", "rack_id": rack_id,
                "node_id": body.get("node_id") or rack_id, "state": state, "trays": COMPUTE_TRAYS,
                "power_shelves": POWER_SHELVES, "at": _iso()}


def bundle_components(bundle: Any) -> Dict[str, str]:
    """``bundle`` = {version, components:{bmc, gpu, nvswitch, cx, bf}} or a bare version."""
    if isinstance(bundle, dict):
        comps = bundle.get("components") or {}
        if not isinstance(comps, dict) or not comps:
            raise HTTPException(400, "bundle.components is required")
        unknown = set(comps) - set(FW_ORDER)
        if unknown:
            raise HTTPException(400, f"unknown bundle components {sorted(unknown)}")
        return {k: str(v) for k, v in comps.items()}
    raise HTTPException(400, "bundle must be {version, components}")


def apply_order(comps: Dict[str, str]) -> List[str]:
    return [c for c in FW_ORDER if c in comps]


@rms_router.post("/racks/{rack_id}/firmware")
def rms_firmware(rack_id: str, body: Dict[str, Any] = Body(...)):
    bundle = body.get("bundle")
    if bundle is None and body.get("firmware_targets"):     # rack_manager.proto shape
        bundle = {"version": body.get("version"),
                  "components": {t.get("target"): t.get("filename")
                                 for t in body["firmware_targets"] if isinstance(t, dict)}}
    comps = bundle_components(bundle)
    version = bundle.get("version") if isinstance(bundle, dict) else None
    with STORE.lock:
        rack = STORE.racks.get(rack_id)
        r = None if rack else _stage(rack_id)
        if r is not None and r["power"] != "On":
            raise HTTPException(409, f"rack {rack_id} is powered off")
        steps = []
        for c in apply_order(comps):
            before = r["firmware"].get(c) if r else None
            if r:
                r["firmware"][c] = comps[c]
            steps.append({"component": c, "from": before, "to": comps[c],
                          "status": "Completed"})
        STORE.event("info", "NeoCloudEmulator.1.0.FirmwareBundleApplied",
                    [rack_id, version or "-", ">".join(s["component"] for s in steps)])
        return {"status": "RETURN_CODE_SUCCESS", "job_id": str(uuid.uuid4()),
                "message": f"{len(steps)} components updated",
                "rack_id": rack_id, "bundle": version, "task_state": "Completed",
                "order": [s["component"] for s in steps], "steps": steps,
                "firmware": dict(r["firmware"]) if r else None, "at": _iso()}
