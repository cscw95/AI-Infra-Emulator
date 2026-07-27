"""UFM Enterprise emulator — emulates the VR NVL72 GPU fabric (InfiniBand)
down to individual switches (prefix /ufm/v1).

The resource model follows NVIDIA UFM Enterprise REST API's resource axes
(systems / ports / links / pkeys / unhealthy ports / events), and port counter
names abbreviate the IBTA-standard PortCounters (SymbolErrorCounter,
LinkErrorRecoveryCounter, LinkDownedCounter, PortXmitWait, PortRcvErrors).

Confirmed (public spec):
  - UFM Enterprise REST resources: systems/ports/links/pkeys/unhealthy
    ports/alarms/events (docs.nvidia.com UFM Enterprise REST API Guide)
  - Quantum-X800 Q3400: 144 x 800Gb/s XDR ports (72 OSFP cages), Quantum-3
    ASIC, 115.2Tb/s, SHARPv4 (nvidia.com Quantum-X800 datasheet)
  - IB-standard port error counter names (IBTA / UFM telemetry counter set)
Assumption (non-public / undetermined — arbitrary consistent values):
  - VR NVL72-generation rail-optimized site fabric layout: spines per plane
    gasan 2 / ansan 4, 4 leaves per plane per SU (consistent with the existing
    fabric.py topology)
  - Tray HCA: ConnectX-9 dual-plane, 1 port/plane (CX9 details not public)
  - FW version string, temperature range, XDR BER threshold (~1e-14), GUID encoding
Behavior: same tick-cache pattern as ObsEngine — state is refreshed only every
TICK_SEC, and counters increase monotonically in proportion to tick_no (no full
recompute per request). On STORE.seed_gen change (reset), rebuild the topology +
seed one demo sample anomaly."""
import itertools
import random
import time
import zlib
from collections import deque
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .store import STORE, CLUSTER, _iso
from . import fabric as _fabric

router = APIRouter(prefix="/ufm/v1", tags=["ufm"])

TICK_SEC = 2.5
PLANES = ("a", "b")                       # dual plane (fabric.py Fabric-A/B)
SPINES_PER_PLANE = {"gasan": 2, "ansan": 4}   # Assumption (proportional to site size)
LEAVES_PER_SU_PLANE = 4                   # rail-optimized 4 rails (consistent with fabric.py)
MODEL = "Quantum-X800 Q3400"
FW_VERSION = "31.2014.2036"               # Assumption: arbitrary FW string
PORTS_TOTAL = 144                         # Confirmed: Q3400 144x800G XDR
SPEED = "XDR 800G"
HCA_MODEL = "ConnectX-9"                  # Assumption: VR-generation HCA
BER_OK = 1e-14                            # Assumption: near the XDR post-FEC BER
BER_DEGRADED = 3e-9
INJECT_KINDS = ("link_degrade", "link_down", "port_flap", "switch_down")

_SITE_PREF = {"gasan": "ga", "ansan": "an"}


def _crc(s: str) -> int:
    return zlib.crc32(s.encode())


def _guid(name: str) -> str:
    """Assumption: NVIDIA OUI (0x0c42)-style pseudo GUID — deterministic, name-based."""
    return "0x0c42a103%08x" % _crc(name)


def _su_num(su_id: str) -> int:
    return int(su_id.split("-")[1])


class FaultInject(BaseModel):
    kind: str                              # link_degrade|link_down|port_flap|switch_down
    target: Optional[str] = None           # link_id | guid | switch name | su_id


class FaultRecover(BaseModel):
    target: Optional[str] = None


# ── engine ────────────────────────────────────────────────────────────
class UfmEngine:
    def __init__(self):
        self.seed_gen = -1
        self.tick_no = 0
        self.last = 0.0
        self.switches: Dict[str, dict] = {}      # guid -> switch
        self.by_name: Dict[str, str] = {}         # name -> guid
        self.links: Dict[str, dict] = {}          # link_id -> leaf↔spine link
        self.hca: List[dict] = []                  # SU-aggregated leaf↔HCA links
        self.leaf_trays: Dict[str, List[str]] = {}  # leaf name -> tray_ids
        self.site_names: Dict[str, str] = {}
        self.events: deque = deque(maxlen=500)
        self.alerts: Dict[tuple, dict] = {}
        self._alert_seq = itertools.count(1)
        self._event_seq = itertools.count(1)

    # ── topology (re)build — detect STORE reseed ────────────────────────
    def _ensure_topology(self) -> bool:
        cur = getattr(STORE, "seed_gen", 0)
        if cur == self.seed_gen:
            return False
        self.seed_gen = cur
        self.switches, self.by_name, self.links = {}, {}, {}
        self.hca, self.leaf_trays, self.site_names = [], {}, {}
        self.events.clear()
        self.alerts = {}
        self.tick_no, self.last = 0, 0.0
        # site/SU layout is derived from STORE.racks (respects NICO_RACKS_LIMIT)
        site_sus: Dict[str, Dict[str, List[str]]] = {}   # site_id -> su -> trays
        for r in STORE.racks.values():
            self.site_names[r.site_id] = r.site
            site_sus.setdefault(r.site_id, {}).setdefault(r.su_id, []).extend(
                r.trays)
        order = [m["id"] for m in CLUSTER]
        for sid in sorted(site_sus, key=lambda s: order.index(s) if s in order
                          else 99):
            pref = _SITE_PREF.get(sid, sid[:2])
            sus = site_sus[sid]
            for plane in PLANES:
                spines = []
                for i in range(1, SPINES_PER_PLANE.get(sid, 2) + 1):
                    name = f"{pref}-ib-spine-{plane}-{i:02d}"
                    spines.append(self._add_switch(name, "spine", plane, sid,
                                                   None))
                for su_id in sorted(sus, key=_su_num):
                    trays = sorted(sus[su_id])
                    for i in range(1, LEAVES_PER_SU_PLANE + 1):
                        lname = (f"{pref}-leaf{plane.upper()}"
                                 f"-su{_su_num(su_id)}-{i:02d}")
                        leaf = self._add_switch(lname, "leaf", plane, sid,
                                                su_id)
                        # rail-optimized: distribute SU trays across 4 leaves
                        self.leaf_trays[lname] = trays[i - 1::LEAVES_PER_SU_PLANE]
                        for sp in spines:                 # every leaf↔spine
                            lid = f"{lname}--{sp['name']}"
                            self.links[lid] = {
                                "link_id": lid, "src": lname,
                                "dst": sp["name"], "plane": plane.upper(),
                                "su_id": su_id, "site": sid,
                                "fault": None,
                                "flaps_24h": _crc(lid) % 2,   # residual flaps in normal operation
                            }
                    # leaf↔HCA — SU-aggregated count (per-tray links are summarized)
                    self.hca.append({
                        "scope": "su_aggregate", "site": sid, "su_id": su_id,
                        "plane": plane.upper(), "trays": len(trays),
                        "links_total": len(trays),
                        "hca_model": HCA_MODEL, "speed": SPEED,
                    })
        self._seed_sample()
        return True

    def _add_switch(self, name: str, typ: str, plane: str, sid: str,
                    su_id: Optional[str]) -> dict:
        g = _guid(name)
        sw = {"guid": g, "name": name, "type": typ, "plane": plane.upper(),
              "site": sid, "su_id": su_id, "model": MODEL, "fw": FW_VERSION,
              "ports_total": PORTS_TOTAL, "ports_active": 0,
              "temperature_c": 42.0 + (_crc(name) % 60) / 10.0,  # Assumption
              "state": "ok", "fault": None}
        self.switches[g] = sw
        self.by_name[name] = g
        return sw

    def _seed_sample(self):
        """Seed one resolved sample so event/alarm history is not empty right after reset."""
        if not self.links:
            return
        lid = next(iter(self.links))
        self._event("minor", "Communication",
                    lid, "Symbol errors threshold exceeded — link degraded "
                         "then recovered (sample)")
        self.alerts[("sample", lid)] = {
            "alert_id": f"ufm-{next(self._alert_seq):04d}", "domain": "fabric",
            "severity": "minor", "resource": lid,
            "summary": f"UFM link_degrade on {lid} — SymbolError spike (sample)",
            "at": _iso(), "state": "resolved", "source": "ufm"}

    # ── alert/event helpers (ObsEngine _fire/_resolve pattern) ───────────
    def _fire(self, key, severity, resource, summary):
        a = self.alerts.get(key)
        if a and a["state"] == "firing":
            a["severity"], a["summary"] = severity, summary
            return
        self.alerts[key] = {"alert_id": f"ufm-{next(self._alert_seq):04d}",
                            "domain": "fabric", "severity": severity,
                            "resource": resource, "summary": summary,
                            "at": _iso(), "state": "firing", "source": "ufm"}

    def _resolve(self, key):
        a = self.alerts.get(key)
        if a and a["state"] == "firing":
            a["state"], a["at"] = "resolved", _iso()

    def _event(self, severity, category, obj, description):
        self.events.appendleft({
            "event_id": next(self._event_seq), "at": _iso(),
            "severity": severity, "category": category,
            "object": obj, "description": description})

    # ── tick — refresh state/temperature/flaps (TICK_SEC cache) ─────────────────────
    def tick(self, force: bool = False):
        with STORE.lock:
            if self._ensure_topology():
                force = True
            now = time.time()
            if not force and self.tick_no and now - self.last < TICK_SEC:
                return
            self.tick_no += 1
            self.last = now
            # link state/flaps
            for l in self.links.values():
                if l["fault"] == "port_flap":
                    l["flaps_24h"] += 1
            # switch state / temperature / active ports
            for sw in self.switches.values():
                down = sw["fault"] == "switch_down"
                deg = any(l["fault"] for l in self.links.values()
                          if sw["name"] in (l["src"], l["dst"]))
                sw["state"] = "down" if down else ("degraded" if deg else "ok")
                base = 45.0 if sw["type"] == "spine" else 42.0
                walk = ((_crc(sw["name"]) ^ self.tick_no * 2654435761)
                        % 50) / 10.0
                sw["temperature_c"] = round(
                    22.0 if down else base + walk + (6.0 if deg else 0.0), 1)
                sw["ports_active"] = 0 if down else len(self._port_plan(sw))
            self._eval_alerts()

    def _eval_alerts(self):
        for lid, l in self.links.items():
            key = ("link", lid)
            if l["fault"]:
                sev = "critical" if l["fault"] == "link_down" else "major"
                self._fire(key, sev, lid,
                           f"UFM {l['fault']} on {lid} "
                           f"(plane {l['plane']}, {l['su_id']}) — "
                           f"SymbolErrorCounter spike, BER {self.link_ber(l):.1e}")
            else:
                self._resolve(key)
        for sw in self.switches.values():
            key = ("switch", sw["guid"])
            if sw["fault"] == "switch_down":
                self._fire(key, "critical", sw["name"],
                           f"UFM switch_down: {sw['name']} ({MODEL}) "
                           f"unresponsive — {sw['ports_total']} ports offline")
            else:
                self._resolve(key)

    # ── derived views ─────────────────────────────────────────────────
    @staticmethod
    def link_state(l: dict) -> str:
        return {"link_down": "down", "link_degrade": "degraded",
                "port_flap": "degraded"}.get(l["fault"], "active")

    @staticmethod
    def link_ber(l: dict) -> float:
        return BER_DEGRADED if l["fault"] else BER_OK

    def link_view(self, l: dict) -> dict:
        state = self.link_state(l)
        return {"link_id": l["link_id"], "src": l["src"], "dst": l["dst"],
                "plane": l["plane"], "su_id": l["su_id"], "site": l["site"],
                "speed": SPEED, "state": state, "ber": self.link_ber(l),
                "symbol_err_rate": (0.0 if state == "active" else
                                    round(120.0 + _crc(l["link_id"]) % 80, 1)),
                "flaps_24h": l["flaps_24h"]}

    def _port_plan(self, sw: dict) -> List[dict]:
        """A switch's planned connected ports [{number, peer, link_id|tray}] (lightweight)."""
        plan = []
        if sw["type"] == "leaf":
            spines = [s for s in self.switches.values()
                      if s["type"] == "spine" and s["site"] == sw["site"]
                      and s["plane"] == sw["plane"]]
            for i, sp in enumerate(sorted(spines, key=lambda s: s["name"])):
                plan.append({"number": i + 1, "peer": f"{sp['name']}",
                             "link_id": f"{sw['name']}--{sp['name']}"})
            for j, tid in enumerate(self.leaf_trays.get(sw["name"], [])):
                plan.append({"number": len(plan) + 1,
                             "peer": f"nh-{tid} ({HCA_MODEL} "
                                     f"P{sw['plane']})",
                             "tray": tid})
        else:                                   # spine: all leaves within the site/plane
            leaves = [s for s in self.switches.values()
                      if s["type"] == "leaf" and s["site"] == sw["site"]
                      and s["plane"] == sw["plane"]]
            for i, lf in enumerate(sorted(leaves, key=lambda s: s["name"])):
                plan.append({"number": i + 1, "peer": lf["name"],
                             "link_id": f"{lf['name']}--{sw['name']}"})
        return plan

    def port_view(self, sw: dict, p: dict) -> dict:
        lid = p.get("link_id")
        l = self.links.get(lid) if lid else None
        if sw["fault"] == "switch_down":
            state = "down"
        elif l:
            state = self.link_state(l)
        else:                                   # leaf↔HCA port
            state = "down" if sw["fault"] == "switch_down" else "active"
        # counters — monotonic increase with tick_no (grows per poll); spikes on fault
        h = _crc(f"{sw['guid']}/{p['number']}")
        t = self.tick_no
        degraded = state == "degraded"
        down = state == "down"
        c = {
            "symbol_err": (h % 3) * (t // 40)
            + (5000 * t if degraded else 0),
            "link_error_recovery": (h % 2) * (t // 120)
            + (12 * t if degraded else 0),
            "link_downed": (1 + t // 200 if down else 0)
            + (l["flaps_24h"] if l and l["fault"] == "port_flap" else 0),
            "xmit_wait": (h % 997 + 13) * t * (3 if degraded else 1),
            "rcv_errors": (h % 5) * (t // 60) + (900 * t if degraded else 0),
            "tx_gbps": 0.0 if down else round(
                (280 + (h ^ t * 2654435761) % 4200 / 10.0)
                * (0.35 if degraded else 1.0), 1),
        }
        c["rx_gbps"] = round(c["tx_gbps"] * 0.94, 1)
        return {"number": p["number"], "state": state, "speed": SPEED,
                "peer": p["peer"], "counters": c}

    # ── health / obs integration ─────────────────────────────────────────────
    def health(self, site: Optional[str] = None) -> dict:
        sws = [s for s in self.switches.values()
               if not site or self._site_ok(s["site"], site)]
        lks = [self.link_view(l) for l in self.links.values()
               if not site or self._site_ok(l["site"], site)]
        hca = [h for h in self.hca
               if not site or self._site_ok(h["site"], site)]
        down_sw = [s for s in sws if s["state"] == "down"]
        # if a leaf is down, that leaf's HCA links are counted as down too
        hca_total = sum(h["links_total"] for h in hca)
        hca_down = sum(len(self.leaf_trays.get(s["name"], []))
                       for s in down_sw if s["type"] == "leaf")
        deg = [l for l in lks if l["state"] == "degraded"]
        down = [l for l in lks if l["state"] == "down"]
        unhealthy = []
        for l in deg + down:
            unhealthy.append({"system": l["src"], "peer": l["dst"],
                              "link_id": l["link_id"], "state": l["state"],
                              "issue": ("LinkDowned" if l["state"] == "down"
                                        else "SymbolErrorCounter threshold")})
        flaps = sum(l["flaps_24h"] for l in lks)
        score = max(0.0, 100.0 - 2.0 * len(deg) - 5.0 * len(down)
                    - 10.0 * len(down_sw) - min(20.0, flaps / 10.0)
                    - 0.5 * hca_down)
        return {
            "site": site, "generated_at": _iso(),
            "switches": {"total": len(sws),
                         "ok": sum(1 for s in sws if s["state"] == "ok"),
                         "degraded": sum(1 for s in sws
                                         if s["state"] == "degraded"),
                         "down": len(down_sw)},
            "links_total": len(lks) + hca_total,
            "links_active": (len(lks) - len(deg) - len(down)
                             + hca_total - hca_down),
            "links_degraded": len(deg),
            "links_down": len(down) + hca_down,
            "unhealthy_ports": unhealthy,
            "flaps_24h": flaps,
            "score": round(score, 1),
        }

    def _site_ok(self, sid: str, q: str) -> bool:
        return q.lower() in (sid, self.site_names.get(sid, "").lower())

    def plane_states(self) -> Dict[tuple, str]:
        """(site_id, su_id, 'A'|'B') → up|degraded|down — public helper exposed so the
        fabric.py topology popup matches the UFM link state."""
        self.tick()
        out: Dict[tuple, str] = {}
        rank = {"up": 0, "degraded": 1, "down": 2}
        for l in self.links.values():
            key = (l["site"], l["su_id"], l["plane"])
            st = {"active": "up", "degraded": "degraded",
                  "down": "down"}[self.link_state(l)]
            if rank[st] > rank.get(out.get(key, "up"), 0):
                out[key] = st
        for sw in self.switches.values():
            if sw["fault"] == "switch_down" and sw["su_id"]:
                out[(sw["site"], sw["su_id"], sw["plane"])] = "down"
        return out

    def alerts_for_obs(self) -> List[dict]:
        """Public helper for obs alerts() merge — all 'fabric' domain alarms."""
        self.tick()
        return [dict(a) for a in self.alerts.values()]

    # ── fault targeting ───────────────────────────────────────────────
    def resolve_target(self, kind: str, target: Optional[str]):
        """target(link_id|guid|name|su_id) → ('link', link)|('switch', sw)."""
        if kind == "switch_down":
            if not target:
                sw = next((s for s in self.switches.values()
                           if s["type"] == "leaf"), None)
            elif target in self.switches:
                sw = self.switches[target]
            elif target in self.by_name:
                sw = self.switches[self.by_name[target]]
            else:
                sw = next((s for s in self.switches.values()
                           if s["su_id"] == target), None)
            if not sw:
                raise HTTPException(404, f"switch target '{target}' not found")
            return "switch", sw
        if not target:
            l = next(iter(self.links.values()), None)
        elif target in self.links:
            l = self.links[target]
        else:
            name = self.switches.get(target, {}).get("name") or (
                target if target in self.by_name else None)
            if name:
                l = next((x for x in self.links.values()
                          if name in (x["src"], x["dst"])), None)
            else:                               # su_id
                l = next((x for x in self.links.values()
                          if x["su_id"] == target), None)
        if not l:
            raise HTTPException(404, f"link target '{target}' not found")
        return "link", l


ENGINE = UfmEngine()


# ── 1) systems ────────────────────────────────────────────────────────
@router.get("/resources/systems")
def systems(site: Optional[str] = None, type: Optional[str] = None):
    ENGINE.tick()
    with STORE.lock:
        sws = list(ENGINE.switches.values())
        if site:
            sws = [s for s in sws if ENGINE._site_ok(s["site"], site)]
        if type:
            sws = [s for s in sws if s["type"] == type]
        out = [{k: v for k, v in s.items() if k != "fault"} for s in sws]
        return {"count": len(out), "systems": out}


# ── 2) ports ──────────────────────────────────────────────────────────
@router.get("/resources/ports")
def ports(system_guid: str, state: Optional[str] = None):
    ENGINE.tick()
    with STORE.lock:
        sw = ENGINE.switches.get(system_guid) or ENGINE.switches.get(
            ENGINE.by_name.get(system_guid, ""))
        if not sw:
            raise HTTPException(404, f"system {system_guid} not found")
        prts = [ENGINE.port_view(sw, p) for p in ENGINE._port_plan(sw)]
        if state:
            prts = [p for p in prts if p["state"] == state]
        return {"system": sw["name"], "guid": sw["guid"],
                "ports_total": sw["ports_total"],
                "count": len(prts), "ports": prts}


# ── 3) links ──────────────────────────────────────────────────────────
@router.get("/resources/links")
def links(state: Optional[str] = None, site: Optional[str] = None):
    ENGINE.tick()
    with STORE.lock:
        lks = [ENGINE.link_view(l) for l in ENGINE.links.values()]
        hca = list(ENGINE.hca)
        if site:
            lks = [l for l in lks if ENGINE._site_ok(l["site"], site)]
            hca = [h for h in hca if ENGINE._site_ok(h["site"], site)]
        if state:
            lks = [l for l in lks if l["state"] == state]
        # leaf↔HCA is SU-aggregated (the 2,520 individual tray links appear only as summary fields)
        return {"count": len(lks), "links": lks,
                "hca_su_summary": hca,
                "hca_links_total": sum(h["links_total"] for h in hca)}


# ── 4) pkeys — based on real DPU attachments (reuses fabric.py P_Key logic) ──
@router.get("/resources/pkeys")
def pkeys():
    ENGINE.tick()
    with STORE.lock:
        parts = _fabric._ib_partitions()
        t_sites = _fabric._tenant_sites()
        out = []
        for p in parts:
            guids = [_guid(fid) for fid in p.get("member_functions", [])]
            out.append({"pkey": p["pkey"], "partition_id": p["partition_id"],
                        "tenant_id": p.get("tenant_id"),
                        "guids": guids, "member_count": len(guids),
                        "sites": sorted(t_sites.get(p.get("tenant_id"), set())),
                        "membership": p.get("membership", "full")})
        return {"count": len(out), "pkeys": out}


# ── 5) fabric health ──────────────────────────────────────────────────
@router.get("/fabric/health")
def fabric_health(site: Optional[str] = None):
    ENGINE.tick()
    with STORE.lock:
        return ENGINE.health(site)


# ── 6) events ─────────────────────────────────────────────────────────
@router.get("/events")
def events(limit: int = 50):
    ENGINE.tick()
    with STORE.lock:
        return list(ENGINE.events)[:limit]


# ── 7) fault inject / recover ─────────────────────────────────────────
@router.post("/faults/inject")
def inject(body: FaultInject):
    ENGINE.tick()
    with STORE.lock:
        if body.kind not in INJECT_KINDS:
            raise HTTPException(400, f"kind must be one of {list(INJECT_KINDS)}")
        typ, obj = ENGINE.resolve_target(body.kind, body.target)
        if typ == "switch":
            obj["fault"] = "switch_down"
            target = obj["name"]
            ENGINE._event("critical", "Hardware", target,
                          f"switch_down injected — {MODEL} unresponsive")
        else:
            obj["fault"] = body.kind
            target = obj["link_id"]
            if body.kind == "port_flap":
                obj["flaps_24h"] += 5
            ENGINE._event("critical" if body.kind == "link_down" else "major",
                          "Communication", target,
                          f"{body.kind} injected — SymbolErrorCounter spike, "
                          f"BER {BER_DEGRADED:.0e}")
        STORE.event("critical", "NeoCloudEmulator.1.0.UfmFaultInjected",
                    [body.kind, target])
    ENGINE.tick(force=True)
    with STORE.lock:
        return {"injected": body.kind, "target_type": typ, "target": target,
                "health": ENGINE.health()}


@router.post("/faults/recover")
def recover(body: FaultRecover = FaultRecover()):
    ENGINE.tick()
    with STORE.lock:
        cleared = []
        for l in ENGINE.links.values():
            if l["fault"] and (not body.target or body.target in (
                    l["link_id"], l["src"], l["dst"], l["su_id"])):
                cleared.append(l["link_id"])
                l["fault"] = None
        for sw in ENGINE.switches.values():
            if sw["fault"] and (not body.target or body.target in (
                    sw["guid"], sw["name"], sw["su_id"])):
                cleared.append(sw["name"])
                sw["fault"] = None
        for t in cleared:
            ENGINE._event("info", "Communication", t, "fault recovered")
        if cleared:
            STORE.event("info", "NeoCloudEmulator.1.0.UfmFaultRecovered",
                        [", ".join(cleared)])
    ENGINE.tick(force=True)
    with STORE.lock:
        return {"recovered": cleared, "health": ENGINE.health()}
