"""Out-of-band (OOB) plane — BMC reachability, power-shelf PSUs, leak
detection, inlet thermals and the System Event Log (SEL) per compute tray.

The Redfish surfaces (PowerSubsystem / ThermalSubsystem / LeakDetection /
LogServices/SEL) in redfish_oob.py render this state; every state change is
published on the DSX Exchange bus (and from there pushed to Redfish
EventService subscribers).

  GET  /emulator/v1/oob/summary?site=     per-rack OOB roll-up
  GET  /emulator/v1/oob/racks/{rack_id}   per-tray detail
  POST /emulator/v1/oob/inject            leak|psu_fail|bmc_unreachable|thermal|sel_critical
  POST /emulator/v1/oob/recover           clear one fault kind/target (or all)"""
import itertools
import zlib
from collections import deque
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .store import STORE, POWER_SHELVES, _iso
from . import exchange
from . import dps
from . import observability as obs

router = APIRouter(prefix="/emulator/v1/oob", tags=["oob"])

PSU_PER_SHELF = 6
PSU_RATED_W = 5500.0            # Delta 5.5 kW PSU in an ORv3-style power shelf
INLET_BASE_C = 24.0
INLET_WARN_C = 30.0
INLET_CRIT_C = 35.0
SEL_MAX = 500
LEAK_STATES = ("OK", "Warning", "Critical")
INJECT_KINDS = ("leak", "psu_fail", "bmc_unreachable", "thermal", "sel_critical")
_LEAK_RANK = {"OK": 0, "Warning": 1, "Critical": 2}


class _Oob:
    def __init__(self):
        self.seed_gen = -1
        self.bmc_down: Dict[str, str] = {}         # tray → since
        self.psu_failed: Dict[str, str] = {}       # "{rack}/PS{s}-PSU{p}" → since
        self.leak: Dict[str, str] = {}             # tray|rack → Warning|Critical
        self.thermal: Dict[str, float] = {}        # tray|rack → +°C
        self.sel: Dict[str, deque] = {}
        self._sel_ids: Dict[str, itertools.count] = {}

    def sync(self):
        cur = getattr(STORE, "seed_gen", 0)
        if cur != self.seed_gen:
            self.seed_gen = cur
            self.bmc_down, self.psu_failed = {}, {}
            self.leak, self.thermal = {}, {}
            self.sel, self._sel_ids = {}, {}


OOB = _Oob()


def psu_ids():
    return [f"PS{s}-PSU{p}" for s in range(POWER_SHELVES)
            for p in range(PSU_PER_SHELF)]


def _tray_or_404(tray_id: str):
    t = STORE.trays.get(tray_id)
    if not t:
        raise HTTPException(404, f"tray {tray_id} not found")
    return t


def _rack_or_404(rack_id: str):
    r = STORE.racks.get(rack_id)
    if not r:
        raise HTTPException(404, f"rack {rack_id} not found")
    return r


# ── SEL ───────────────────────────────────────────────────────────────
def sel_entries(tray_id: str) -> deque:
    OOB.sync()
    q = OOB.sel.get(tray_id)
    if q is None:
        q = OOB.sel[tray_id] = deque(maxlen=SEL_MAX)
        OOB._sel_ids[tray_id] = itertools.count(1)
        _sel_add(tray_id, "OK", "System boot completed", "SYS_BOOT",
                 "System Event", publish=False)
        _sel_add(tray_id, "OK", "BMC firmware 1.4.2 running", "BMC_FW",
                 "Management Subsystem Health", publish=False)
    return q


def sel_add(tray_id: str, severity: str, message: str, code: str,
            sensor_type: str = "System Event") -> dict:
    sel_entries(tray_id)
    return _sel_add(tray_id, severity, message, code, sensor_type)


def _sel_add(tray_id, severity, message, code, sensor_type, publish=True):
    eid = next(OOB._sel_ids[tray_id])
    e = {"Id": str(eid), "Created": _iso(), "Severity": severity,
         "Message": message, "MessageId": f"NeoCloudSEL.1.0.{code}",
         "SensorType": sensor_type, "EntryType": "SEL", "Resolved": False}
    OOB.sel[tray_id].append(e)
    if publish:
        exchange.publish(
            "health", f"SelEntry{severity}",
            "critical" if severity == "Critical" else
            "warning" if severity == "Warning" else "info",
            tray_id, {"sel_id": e["Id"], "message": message, "code": code,
                      "@odata.id": f"/redfish/v1/Systems/{tray_id}/LogServices"
                                   f"/SEL/Entries/{e['Id']}"},
            category="sel")
    return e


def sel_critical(tray_id: str) -> int:
    q = OOB.sel.get(tray_id)
    return sum(1 for e in q if e["Severity"] == "Critical"
               and not e["Resolved"]) if q else 0


# ── derived state ─────────────────────────────────────────────────────
def bmc_reachable(tray_id: str) -> bool:
    OOB.sync()
    return tray_id not in OOB.bmc_down


def require_bmc(tray_id: str):
    if not bmc_reachable(tray_id):
        raise HTTPException(503, f"BMC of {tray_id} unreachable "
                                 "(try an AC power cycle)")


def _cdu_leak_racks() -> set:
    out = set()
    for c in obs.ENGINE.cdus.values():
        if c.leak_detected:
            out.update(c.closed_branches)
    return out


def leak_state(chassis_id: str) -> str:
    """Leak state for a tray or rack chassis (worst of own detectors, its
    rack's manifold detector and the CDU branch leak)."""
    OOB.sync()
    rack_id = chassis_id if chassis_id in STORE.racks else \
        STORE.trays[chassis_id].rack_id
    states = [OOB.leak.get(chassis_id, "OK"), OOB.leak.get(rack_id, "OK")]
    if chassis_id == rack_id:
        states += [OOB.leak.get(t, "OK") for t in STORE.racks[rack_id].trays]
    if rack_id in _cdu_leak_racks():
        states.append("Critical")
    return max(states, key=lambda s: _LEAK_RANK[s])


def inlet_c(tray_id: str) -> float:
    OOB.sync()
    t = STORE.trays[tray_id]
    base = INLET_BASE_C + (zlib.crc32(tray_id.encode()) % 30) / 10.0
    if STORE.power_state(t) == "Off":
        base -= 1.0
    return round(base + OOB.thermal.get(tray_id, 0.0)
                 + OOB.thermal.get(t.rack_id, 0.0), 1)


def thermal_health(c: float) -> str:
    return "Critical" if c >= INLET_CRIT_C else \
        "Warning" if c >= INLET_WARN_C else "OK"


def psus(rack_id: str, rack_rec: Optional[dict] = None) -> list:
    OOB.sync()
    rec = rack_rec if rack_rec is not None else (dps.rack_power(rack_id) or {})
    load_w = rec.get("measured_kw", 0.0) * 1000.0
    ids = psu_ids()
    ok = [p for p in ids if f"{rack_id}/{p}" not in OOB.psu_failed]
    per = load_w / len(ok) if ok else 0.0
    out = []
    for p in ids:
        failed = f"{rack_id}/{p}" in OOB.psu_failed
        out.append({"id": p, "shelf": int(p[2:p.index("-")]),
                    "state": "Absent" if failed else "Enabled",
                    "health": "Critical" if failed else "OK",
                    "output_w": 0.0 if failed else round(per, 1),
                    "rated_w": PSU_RATED_W})
    return out


def rack_oob(rack_id: str, rack_rec: Optional[dict] = None) -> dict:
    r = STORE.racks[rack_id]
    rec = rack_rec if rack_rec is not None else (dps.rack_power(rack_id) or {})
    ps = psus(rack_id, rec)
    trays = r.trays
    psu_ok = sum(1 for p in ps if p["health"] == "OK")
    return {"rack_id": rack_id, "site": r.site_id, "su_id": r.su_id,
            "bmc_reachable": sum(1 for t in trays if bmc_reachable(t)),
            "bmc_total": len(trays),
            "psu_ok": psu_ok, "psu_total": len(ps),
            "psu_capacity_kw": round(psu_ok * PSU_RATED_W / 1000.0, 1),
            "leak": leak_state(rack_id),
            "max_inlet_c": max((inlet_c(t) for t in trays), default=None),
            "sel_critical": sum(sel_critical(t) for t in trays),
            "power_kw": round(rec.get("measured_kw", 0.0), 1),
            "cap_kw": round(rec["cap_kw"], 1) if "cap_kw" in rec else None}


# ── API ───────────────────────────────────────────────────────────────
class InjectBody(BaseModel):
    kind: str
    target: str                 # tray id, rack id, or "{rack}/PS{s}-PSU{p}"
    severity: str = "Critical"  # leak: Warning|Critical
    delta_c: float = 12.0       # thermal: added inlet °C


class RecoverBody(BaseModel):
    kind: Optional[str] = None
    target: Optional[str] = None


@router.get("/summary")
def summary(site: Optional[str] = None):
    with STORE.lock:
        OOB.sync()
        recs = dps.all_rack_power()
        rows = [rack_oob(r.rack_id, recs.get(r.rack_id, {}))
                for r in STORE.racks.values()
                if not site or site in (r.site_id, r.site)]
        tot = {"racks": len(rows),
               "bmc_reachable": sum(x["bmc_reachable"] for x in rows),
               "bmc_total": sum(x["bmc_total"] for x in rows),
               "psu_ok": sum(x["psu_ok"] for x in rows),
               "psu_total": sum(x["psu_total"] for x in rows),
               "leak_racks": sum(1 for x in rows if x["leak"] != "OK"),
               "sel_critical": sum(x["sel_critical"] for x in rows),
               "power_kw": round(sum(x["power_kw"] for x in rows), 1)}
        return {"at": _iso(), "site": site, "totals": tot, "racks": rows}


@router.get("/racks/{rack_id}")
def rack_detail(rack_id: str):
    with STORE.lock:
        OOB.sync()
        r = _rack_or_404(rack_id)
        rec = dps.rack_power(rack_id) or {}
        trays = []
        for tid in r.trays:
            t = STORE.trays[tid]
            tp = dps.tray_power(tid, rec)
            c = inlet_c(tid)
            trays.append({"tray_id": tid, "bmc_ip": t.bmc_ip,
                          "bmc_reachable": bmc_reachable(tid),
                          "power_state": STORE.power_state(t),
                          "leak": leak_state(tid), "inlet_c": c,
                          "thermal": thermal_health(c),
                          "sel_critical": sel_critical(tid),
                          "power_w": tp["measured_w"], "cap_w": tp["cap_w"],
                          "tray_limit_w": tp["tray_limit_w"]})
        return {**rack_oob(rack_id, rec), "psus": psus(rack_id, rec),
                "trays": trays}


def _publish_leak(target, state):
    exchange.publish("leak", "LeakDetected" if state != "OK" else "LeakCleared",
                     "critical" if state == "Critical" else
                     "warning" if state == "Warning" else "info", target,
                     {"state": state, "@odata.id": f"/redfish/v1/Chassis/{target}"
                                                   "/ThermalSubsystem/LeakDetection"},
                     category="leak")


@router.post("/inject")
def inject(body: InjectBody):
    with STORE.lock:
        OOB.sync()
        k, tgt = body.kind, body.target
        if k not in INJECT_KINDS:
            raise HTTPException(422, f"kind must be one of {list(INJECT_KINDS)}")
        if k == "psu_fail":
            rack_id, _, psu = tgt.partition("/")
            _rack_or_404(rack_id)
            if not psu:
                psu = next((p for p in psu_ids()
                            if f"{rack_id}/{p}" not in OOB.psu_failed), None)
                if not psu:
                    raise HTTPException(409, f"all PSUs of {rack_id} already failed")
            if psu not in psu_ids():
                raise HTTPException(404, f"PSU {psu} not found")
            OOB.psu_failed[f"{rack_id}/{psu}"] = _iso()
            for tid in STORE.racks[rack_id].trays[:1]:
                sel_add(tid, "Critical", f"Power shelf {psu} failure (AC lost)",
                        "PSU_FAIL", "Power Supply")
            exchange.publish("power", "PsuFailed", "critical", f"{rack_id}/{psu}",
                             {"rack": rack_id, "psu": psu,
                              "@odata.id": f"/redfish/v1/Chassis/{rack_id}"
                                           f"/PowerSubsystem/PowerSupplies/{psu}"},
                             category="psu")
            return {"injected": k, "target": f"{rack_id}/{psu}",
                    "rack": rack_oob(rack_id)}
        is_rack = tgt in STORE.racks
        if not is_rack:
            _tray_or_404(tgt)
        rack_id = tgt if is_rack else STORE.trays[tgt].rack_id
        trays = STORE.racks[rack_id].trays if is_rack else [tgt]
        if k == "leak":
            if body.severity not in ("Warning", "Critical"):
                raise HTTPException(422, "leak severity must be Warning|Critical")
            OOB.leak[tgt] = body.severity
            sel_add(trays[0], body.severity, f"Leak detected at {tgt}",
                    "LEAK", "Leak Detection")
            _publish_leak(tgt, body.severity)
        elif k == "bmc_unreachable":
            for t in trays:
                OOB.bmc_down[t] = _iso()
            exchange.publish("health", "BmcUnreachable", "critical", tgt,
                             {"trays": trays}, category="health")
        elif k == "thermal":
            OOB.thermal[tgt] = OOB.thermal.get(tgt, 0.0) + body.delta_c
            c = max(inlet_c(t) for t in trays)
            sev = thermal_health(c)
            exchange.publish("thermal", "InletTemperatureHigh",
                             sev.lower() if sev != "OK" else "info", tgt,
                             {"inlet_c": c, "@odata.id": f"/redfish/v1/Chassis/"
                                                         f"{tgt}/ThermalSubsystem"},
                             category="thermal")
            if sev != "OK":
                sel_add(trays[0], sev, f"Inlet temperature {c} °C", "INLET_TEMP",
                        "Temperature")
        elif k == "sel_critical":
            for t in trays:
                sel_add(t, "Critical", "Uncorrectable machine check (GPU/HBM)",
                        "MCE_UNCORR", "Processor")
        return {"injected": k, "target": tgt, "rack": rack_oob(rack_id)}


@router.post("/recover")
def recover(body: Optional[RecoverBody] = None):
    body = body or RecoverBody()
    with STORE.lock:
        OOB.sync()
        k, tgt = body.kind, body.target
        if k and k not in INJECT_KINDS:
            raise HTTPException(422, f"kind must be one of {list(INJECT_KINDS)}")

        def hit(key: str) -> bool:
            if not tgt:
                return True
            return key == tgt or key.startswith(tgt + "/") or (
                key in STORE.trays and STORE.trays[key].rack_id == tgt)

        cleared = 0
        if k in (None, "psu_fail"):
            for key in [x for x in OOB.psu_failed if hit(x)]:
                del OOB.psu_failed[key]; cleared += 1
                exchange.publish("power", "PsuRecovered", "info", key, {},
                                 category="psu")
        if k in (None, "leak"):
            for key in [x for x in OOB.leak if hit(x)]:
                del OOB.leak[key]; cleared += 1
                _publish_leak(key, "OK")
        if k in (None, "bmc_unreachable"):
            for key in [x for x in OOB.bmc_down if hit(x)]:
                del OOB.bmc_down[key]; cleared += 1
        if k in (None, "thermal"):
            for key in [x for x in OOB.thermal if hit(x)]:
                del OOB.thermal[key]; cleared += 1
        if k in (None, "sel_critical"):
            for tid, q in OOB.sel.items():
                if hit(tid):
                    for e in q:
                        if e["Severity"] == "Critical" and not e["Resolved"]:
                            e["Resolved"] = True; cleared += 1
        return {"recovered": k or "all", "target": tgt, "cleared": cleared}
