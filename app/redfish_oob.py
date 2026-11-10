"""Redfish OOB extensions (DMTF schema shapes) for compute-tray and rack
chassis: PowerSubsystem/PowerSupplies (power-shelf PSUs), ThermalSubsystem +
LeakDetection, Controls/PowerLimit, Chassis.Reset (AC power cycle),
LogServices/SEL and the OOB GpuReset action.

Chassis ids: a compute tray id (su-1-rack-00-tray-00) or a rack id
(su-1-rack-00). Trays draw from the rack's shared power-shelf busbar, so a
tray's PowerSupplies are the rack's shelf PSUs."""
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .store import STORE, _iso
from . import dps, oob, exchange
from . import observability as obs

router = APIRouter(prefix="/redfish/v1", tags=["redfish-oob"])

CHASSIS_RESET_TYPES = ["PowerCycle", "On", "ForceOff", "ForceRestart"]


def _health(states) -> str:
    rank = {"OK": 0, "Warning": 1, "Critical": 2}
    return max(states, key=lambda s: rank.get(s, 0), default="OK")


def resolve_chassis(cid: str):
    """→ ("tray"|"rack", rack_id, [tray_ids])"""
    if cid in STORE.trays:
        return "tray", STORE.trays[cid].rack_id, [cid]
    if cid in STORE.racks:
        return "rack", cid, list(STORE.racks[cid].trays)
    raise HTTPException(404, f"Chassis {cid} not found")


def _tray_or_404(tid: str):
    t = STORE.trays.get(tid)
    if not t:
        raise HTTPException(404, f"ComputerSystem {tid} not found")
    return t


def rack_chassis_view(rack_id: str) -> dict:
    r = STORE.racks[rack_id]
    base = f"/redfish/v1/Chassis/{rack_id}"
    ro = oob.rack_oob(rack_id)
    return {
        "@odata.type": "#Chassis.v1_25_0.Chassis", "@odata.id": base,
        "Id": rack_id, "Name": f"Vera Rubin NVL72 Rack {rack_id}",
        "ChassisType": "Rack", "Manufacturer": "NVIDIA",
        "Model": r.model,
        "Status": {"State": "Enabled", "Health": _health(
            [ro["leak"], "Critical" if ro["sel_critical"] else "OK",
             "Warning" if ro["psu_ok"] < ro["psu_total"] else "OK"])},
        "PowerSubsystem": {"@odata.id": f"{base}/PowerSubsystem"},
        "ThermalSubsystem": {"@odata.id": f"{base}/ThermalSubsystem"},
        "Controls": {"@odata.id": f"{base}/Controls"},
        "Links": {"Contains": [{"@odata.id": f"/redfish/v1/Chassis/{t}"}
                               for t in r.trays]},
        "Actions": {"#Chassis.Reset": {
            "target": f"{base}/Actions/Chassis.Reset",
            "ResetType@Redfish.AllowableValues": CHASSIS_RESET_TYPES}},
        "Oem": {"Nvidia": {"SuId": r.su_id, "Site": r.site_id,
                           "PowerShelves": r.power_shelves,
                           "NvlinkSwitchTrays": r.nvlink_switch_trays}},
    }


# ── PowerSubsystem ────────────────────────────────────────────────────
def _psu_view(rack_id: str, cid: str, p: dict) -> dict:
    return {
        "@odata.type": "#PowerSupply.v1_6_0.PowerSupply",
        "@odata.id": f"/redfish/v1/Chassis/{cid}/PowerSubsystem/PowerSupplies/{p['id']}",
        "Id": p["id"], "Name": f"Power shelf {p['shelf']} {p['id']}",
        "Manufacturer": "Delta", "Model": "ORv3 5.5kW PSU",
        "PowerSupplyType": "AC", "PowerCapacityWatts": p["rated_w"],
        "Status": {"State": p["state"], "Health": p["health"]},
        "Metrics": {"OutputPowerWatts": {"Reading": p["output_w"]}},
        "Location": {"PartLocation": {"LocationType": "Bay",
                                      "LocationOrdinalValue": p["shelf"],
                                      "ServiceLabel": f"{rack_id} shelf {p['shelf']}"}},
        "Oem": {"Nvidia": {"Rack": rack_id, "PowerShelf": p["shelf"]}},
    }


@router.get("/Chassis/{cid}/PowerSubsystem")
def power_subsystem(cid: str):
    with STORE.lock:
        kind, rack_id, trays = resolve_chassis(cid)
        if kind == "tray":
            oob.require_bmc(cid)
        rec = dps.rack_power(rack_id) or {}
        ps = oob.psus(rack_id, rec)
        ok = [p for p in ps if p["health"] == "OK"]
        if kind == "tray":
            tp = dps.tray_power(cid, rec)
            alloc, req, meas = tp["cap_w"], tp["demand_w"], tp["measured_w"]
        else:
            alloc = rec.get("cap_kw", 0.0) * 1000
            req = rec.get("demand_kw", 0.0) * 1000
            meas = rec.get("measured_kw", 0.0) * 1000
        base = f"/redfish/v1/Chassis/{cid}/PowerSubsystem"
        return {
            "@odata.type": "#PowerSubsystem.v1_1_2.PowerSubsystem",
            "@odata.id": base, "Id": "PowerSubsystem",
            "Name": f"Power Subsystem {cid}",
            "CapacityWatts": len(ok) * oob.PSU_RATED_W,
            "Allocation": {"AllocatedWatts": round(alloc, 1),
                           "RequestedWatts": round(req, 1)},
            "PowerSupplyRedundancy": [{
                "RedundancyType": "NPlusM", "MaxSupportedInGroup": len(ps),
                "MinNeededInGroup": max(1, int(rec.get("peak_kw", 0) * 1000
                                               // oob.PSU_RATED_W) + 1),
                "Status": {"State": "Enabled",
                           "Health": "OK" if len(ok) == len(ps) else "Warning"}}],
            "PowerSupplies": {"@odata.id": f"{base}/PowerSupplies"},
            "Status": {"State": "Enabled",
                       "Health": "OK" if len(ok) == len(ps) else "Warning"},
            "Oem": {"Nvidia": {"MeasuredWatts": round(meas, 1),
                               "PowerLimitWatts": round(alloc, 1),
                               "SharedBusbar": kind == "tray"}},
        }


@router.get("/Chassis/{cid}/PowerSubsystem/PowerSupplies")
def power_supplies(cid: str):
    with STORE.lock:
        kind, rack_id, _ = resolve_chassis(cid)
        if kind == "tray":
            oob.require_bmc(cid)
        ps = oob.psus(rack_id)
        base = f"/redfish/v1/Chassis/{cid}/PowerSubsystem/PowerSupplies"
        return {"@odata.type": "#PowerSupplyCollection.PowerSupplyCollection",
                "@odata.id": base, "Name": f"Power Supplies {rack_id}",
                "Members@odata.count": len(ps),
                "Members": [_psu_view(rack_id, cid, p) for p in ps]}


@router.get("/Chassis/{cid}/PowerSubsystem/PowerSupplies/{psu}")
def power_supply(cid: str, psu: str):
    with STORE.lock:
        kind, rack_id, _ = resolve_chassis(cid)
        if kind == "tray":
            oob.require_bmc(cid)
        p = next((x for x in oob.psus(rack_id) if x["id"] == psu), None)
        if not p:
            raise HTTPException(404, f"PowerSupply {psu} not found")
        return _psu_view(rack_id, cid, p)


# ── ThermalSubsystem + LeakDetection ──────────────────────────────────
def _leak_view(cid: str, trays: List[str], rack_id: str) -> dict:
    state = oob.leak_state(cid)
    groups = [{"GroupName": "ColdPlates", "HumidityPercent": None,
               "Detectors": [{"DataSourceUri": f"/redfish/v1/Chassis/{t}"
                                               "/ThermalSubsystem/LeakDetection",
                              "DetectorState": oob.leak_state(t)
                              if cid == rack_id else state}
                             for t in trays],
               "Status": {"State": "Enabled", "Health": state}}]
    if cid == rack_id:
        groups.append({"GroupName": "Manifold", "Detectors": [{
            "DataSourceUri": f"/redfish/v1/Chassis/{rack_id}",
            "DetectorState": _health([oob.OOB.leak.get(rack_id, "OK"),
                                      "Critical" if rack_id in oob._cdu_leak_racks()
                                      else "OK"])}],
            "Status": {"State": "Enabled", "Health": state}})
    return {"@odata.type": "#LeakDetection.v1_1_0.LeakDetection",
            "@odata.id": f"/redfish/v1/Chassis/{cid}/ThermalSubsystem/LeakDetection",
            "Id": "LeakDetection", "Name": f"Leak Detection {cid}",
            "DetectorState": state, "LeakDetectorGroups": groups,
            "Status": {"State": "Enabled", "Health": state}}


@router.get("/Chassis/{cid}/ThermalSubsystem")
def thermal_subsystem(cid: str):
    with STORE.lock:
        kind, rack_id, trays = resolve_chassis(cid)
        if kind == "tray":
            oob.require_bmc(cid)
        readings = []
        for t in trays:
            c = oob.inlet_c(t)
            readings.append({"DeviceName": f"{t} Inlet", "Reading": c,
                             "DataSourceUri": f"/redfish/v1/Chassis/{t}/Sensors/InletTemp"})
        if kind == "tray":
            tray = STORE.trays[cid]
            idx = int(cid.split("-")[-1]) if cid.split("-")[-1].isdigit() else 0
            on = STORE.power_state(tray) != "Off"
            for g in range(tray.gpus):
                readings.append({"DeviceName": f"GPU{g}",
                                 "Reading": 55.0 + ((idx + g) % 8) if on else 40.0,
                                 "DataSourceUri": f"/redfish/v1/Chassis/{cid}"
                                                  f"/Sensors/GPU{g}_Temp"})
        max_inlet = max(oob.inlet_c(t) for t in trays)
        th = oob.thermal_health(max_inlet)
        leak = _leak_view(cid, trays, rack_id)
        base = f"/redfish/v1/Chassis/{cid}/ThermalSubsystem"
        return {
            "@odata.type": "#ThermalSubsystem.v1_3_0.ThermalSubsystem",
            "@odata.id": base, "Id": "ThermalSubsystem",
            "Name": f"Thermal Subsystem {cid}",
            "ThermalMetrics": {
                "@odata.id": f"{base}/ThermalMetrics",
                "TemperatureSummaryCelsius": {"Intake": {"Reading": max_inlet}},
                "TemperatureReadingsCelsius": readings},
            "CoolingType": "LiquidCooled",
            "LeakDetection": {**leak, "@odata.id": f"{base}/LeakDetection"},
            "Status": {"State": "Enabled",
                       "Health": _health([th, leak["DetectorState"]])},
        }


@router.get("/Chassis/{cid}/ThermalSubsystem/LeakDetection")
def leak_detection(cid: str):
    with STORE.lock:
        kind, rack_id, trays = resolve_chassis(cid)
        if kind == "tray":
            oob.require_bmc(cid)
        return _leak_view(cid, trays, rack_id)


# ── Controls/PowerLimit ───────────────────────────────────────────────
class PowerLimitPatch(BaseModel):
    SetPoint: Optional[float] = None
    ControlMode: Optional[str] = None      # Automatic | Override | Manual | Disabled


def _power_limit_view(cid: str) -> dict:
    kind, rack_id, _ = resolve_chassis(cid)
    rec = dps.rack_power(rack_id) or {}
    if kind == "tray":
        tp = dps.tray_power(cid, rec)
        sp = dps.DPS.tray_limit_w.get(cid)
        eff, meas = tp["cap_w"], tp["measured_w"]
        lo, hi = 200.0, round(rec.get("peak_kw", 0) * 1000 / 18, 1)
    else:
        sp = dps.DPS.manual_cap.get(rack_id)
        sp = sp * 1000 if sp else None
        eff = rec.get("cap_kw", 0.0) * 1000
        meas = rec.get("measured_kw", 0.0) * 1000
        lo, hi = dps.STANDBY_KW * 1000, round(rec.get("peak_kw", 0) * 1000, 1)
    return {"@odata.type": "#Control.v1_5_0.Control",
            "@odata.id": f"/redfish/v1/Chassis/{cid}/Controls/PowerLimit",
            "Id": "PowerLimit", "Name": f"Power Limit {cid}",
            "ControlType": "Power", "PhysicalContext": "Chassis" if kind == "tray"
            else "Rack", "ControlMode": "Override" if sp else "Automatic",
            "SetPoint": sp, "SetPointUnits": "W",
            "AllowableMin": lo, "AllowableMax": hi,
            "Sensor": {"Reading": round(meas, 1),
                       "DataSourceUri": f"/redfish/v1/Chassis/{cid}/PowerSubsystem"},
            "Oem": {"Nvidia": {"EffectiveLimitWatts": round(eff, 1),
                               "Source": "redfish" if sp else "dps"}}}


@router.get("/Chassis/{cid}/Controls")
def controls(cid: str):
    with STORE.lock:
        resolve_chassis(cid)
        return {"@odata.type": "#ControlCollection.ControlCollection",
                "@odata.id": f"/redfish/v1/Chassis/{cid}/Controls",
                "Members@odata.count": 1,
                "Members": [{"@odata.id": f"/redfish/v1/Chassis/{cid}/Controls/PowerLimit"}]}


@router.get("/Chassis/{cid}/Controls/PowerLimit")
def get_power_limit(cid: str):
    with STORE.lock:
        return _power_limit_view(cid)


@router.patch("/Chassis/{cid}/Controls/PowerLimit")
def patch_power_limit(cid: str, body: PowerLimitPatch):
    """SetPoint in watts. Tray → tray limit (GPU limit = share); rack → manual
    rack cap fed into DPS allocation. ControlMode Automatic/Disabled or
    SetPoint null clears the override."""
    with STORE.lock:
        kind, rack_id, _ = resolve_chassis(cid)
        if kind == "tray":
            oob.require_bmc(cid)
        clear = body.ControlMode in ("Automatic", "Disabled") or body.SetPoint is None
        if not clear and body.SetPoint <= 0:
            raise HTTPException(400, "SetPoint must be > 0 W")
        dps.DPS.sync()
        if kind == "tray":
            if clear:
                dps.DPS.tray_limit_w.pop(cid, None)
            else:
                dps.DPS.tray_limit_w[cid] = float(body.SetPoint)
        else:
            obs.ENGINE._ensure_topology()
            if clear:
                dps.DPS.manual_cap.pop(rack_id, None)
                ctl = obs.ENGINE.rack_ctrl.get(rack_id)
                if ctl:
                    ctl["cap_kw"], ctl["cap_source"] = None, "dps"
            else:
                dps.DPS.manual_cap[rack_id] = float(body.SetPoint) / 1000.0
            did = dps.DPS.rack_domain(rack_id)
            if did:
                dps.enforce(did)
            elif not clear:
                ctl = obs.ENGINE.rack_ctrl.setdefault(rack_id, obs._default_ctrl())
                ctl["cap_kw"], ctl["cap_source"] = round(body.SetPoint / 1000, 1), "manual"
        exchange.publish("power", "PowerLimitCleared" if clear else "PowerLimitSet",
                         "info", cid, {"set_point_w": None if clear else body.SetPoint,
                                       "@odata.id": f"/redfish/v1/Chassis/{cid}"
                                                    "/Controls/PowerLimit"},
                         category="power")
        return _power_limit_view(cid)


# ── Chassis.Reset (AC power cycle) ────────────────────────────────────
class ChassisReset(BaseModel):
    ResetType: str = "PowerCycle"


@router.post("/Chassis/{cid}/Actions/Chassis.Reset")
def chassis_reset(cid: str, body: ChassisReset):
    """AC power cycle via the power shelf/RMS path — works even when the BMC
    is unreachable, and restores BMC reachability."""
    with STORE.lock:
        kind, rack_id, trays = resolve_chassis(cid)
        rt = body.ResetType
        if rt not in CHASSIS_RESET_TYPES:
            raise HTTPException(400, f"ResetType must be one of {CHASSIS_RESET_TYPES}")
        oob.OOB.sync()
        mapped = {"PowerCycle": "ForceRestart", "On": "On",
                  "ForceOff": "ForceOff", "ForceRestart": "ForceRestart"}[rt]
        for t in trays:
            tray = STORE.trays[t]
            STORE.set_power(tray, mapped)
            if rt == "PowerCycle":
                tray.power_state = "Off"            # AC drop, then PoweringOn
                tray.power_target = "On"
                oob.OOB.bmc_down.pop(t, None)
            oob.sel_add(t, "OK", f"Chassis reset {rt} ({'AC power cycle' if rt == 'PowerCycle' else rt})",
                        "AC_POWER_CYCLE" if rt == "PowerCycle" else "CHASSIS_RESET",
                        "Power Unit")
        STORE.event("info", "NeoCloudEmulator.1.0.ChassisReset", [cid, rt])
        return {"status": "accepted", "ResetType": rt, "Chassis": cid,
                "Trays": trays, "PowerState": [STORE.power_state(STORE.trays[t])
                                               for t in trays][0]}


# ── LogServices / SEL ─────────────────────────────────────────────────
@router.get("/Systems/{tid}/LogServices")
def log_services(tid: str):
    with STORE.lock:
        _tray_or_404(tid)
        return {"@odata.type": "#LogServiceCollection.LogServiceCollection",
                "@odata.id": f"/redfish/v1/Systems/{tid}/LogServices",
                "Members@odata.count": 1,
                "Members": [{"@odata.id": f"/redfish/v1/Systems/{tid}/LogServices/SEL"}]}


@router.get("/Systems/{tid}/LogServices/SEL")
def sel_service(tid: str):
    with STORE.lock:
        _tray_or_404(tid)
        oob.require_bmc(tid)
        q = oob.sel_entries(tid)
        base = f"/redfish/v1/Systems/{tid}/LogServices/SEL"
        return {"@odata.type": "#LogService.v1_5_0.LogService", "@odata.id": base,
                "Id": "SEL", "Name": "System Event Log", "LogEntryType": "SEL",
                "MaxNumberOfRecords": oob.SEL_MAX, "OverWritePolicy": "WrapsWhenFull",
                "Entries": {"@odata.id": f"{base}/Entries"},
                "Actions": {"#LogService.ClearLog": {"target": f"{base}/Actions/LogService.ClearLog"}},
                "Status": {"State": "Enabled",
                           "Health": "Critical" if oob.sel_critical(tid) else "OK"},
                "Oem": {"Nvidia": {"Entries": len(q),
                                   "CriticalUnresolved": oob.sel_critical(tid)}}}


def _entry_view(tid: str, e: dict) -> dict:
    return {"@odata.type": "#LogEntry.v1_15_0.LogEntry",
            "@odata.id": f"/redfish/v1/Systems/{tid}/LogServices/SEL/Entries/{e['Id']}",
            **{k: v for k, v in e.items()}}


@router.get("/Systems/{tid}/LogServices/SEL/Entries")
def sel_list(tid: str, severity: Optional[str] = None, top: int = 200):
    with STORE.lock:
        _tray_or_404(tid)
        oob.require_bmc(tid)
        es = list(oob.sel_entries(tid))
        if severity:
            es = [e for e in es if e["Severity"] == severity]
        es = es[-top:][::-1]
        return {"@odata.type": "#LogEntryCollection.LogEntryCollection",
                "@odata.id": f"/redfish/v1/Systems/{tid}/LogServices/SEL/Entries",
                "Name": f"SEL Entries {tid}", "Members@odata.count": len(es),
                "Members": [_entry_view(tid, e) for e in es]}


@router.get("/Systems/{tid}/LogServices/SEL/Entries/{eid}")
def sel_entry(tid: str, eid: str):
    with STORE.lock:
        _tray_or_404(tid)
        oob.require_bmc(tid)
        e = next((x for x in oob.sel_entries(tid) if x["Id"] == eid), None)
        if not e:
            raise HTTPException(404, f"LogEntry {eid} not found")
        return _entry_view(tid, e)


@router.post("/Systems/{tid}/LogServices/SEL/Actions/LogService.ClearLog")
def sel_clear(tid: str):
    with STORE.lock:
        _tray_or_404(tid)
        oob.require_bmc(tid)
        oob.sel_entries(tid).clear()
        oob.sel_add(tid, "OK", "SEL cleared", "SEL_CLEARED", "Event Logging Disabled")
        return {"status": "cleared", "System": tid}


# ── OOB GPU reset ─────────────────────────────────────────────────────
class GpuResetBody(BaseModel):
    GpuIds: Optional[List[int]] = None       # default: every GPU of the tray


@router.post("/Systems/{tid}/Actions/GpuReset")
def gpu_reset(tid: str, body: Optional[GpuResetBody] = None):
    """OOB GPU reset through the BMC (NICo AdminGpuReset). Requires the BMC
    and the host powered on."""
    with STORE.lock:
        tray = _tray_or_404(tid)
        oob.require_bmc(tid)
        if STORE.power_state(tray) != "On":
            raise HTTPException(409, f"{tid} is not powered on")
        ids = (body.GpuIds if body and body.GpuIds is not None
               else list(range(tray.gpus)))
        bad = [g for g in ids if not 0 <= g < tray.gpus]
        if bad:
            raise HTTPException(400, f"invalid GpuIds {bad} (0..{tray.gpus - 1})")
        if tray.health == "warning":
            tray.health = "ok"
        oob.sel_add(tid, "OK", f"OOB GPU reset {ids}", "GPU_RESET", "Processor")
        exchange.publish("health", "GpuReset", "info", tid, {"gpus": ids},
                         category="health")
        STORE.event("info", "NeoCloudEmulator.1.0.GpuReset",
                    [tid, ",".join(map(str, ids))])
        return {"@odata.type": "#Task.v1_7_0.Task", "Id": f"gpureset-{tid}",
                "Name": "GpuReset", "TaskState": "Completed", "TaskStatus": "OK",
                "System": tid, "GpuIds": ids, "EndTime": _iso()}
