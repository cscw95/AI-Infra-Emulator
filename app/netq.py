"""NetQ 에뮬레이터 — 사이트별 Spectrum-X converged Ethernet 패브릭을
스위치/프로토콜/RoCE/검증(validation) 수준까지 에뮬레이션한다 (prefix /netq/v1).

리소스·검증 체계는 NVIDIA NetQ의 공개 개념을 따른다.

Confirmed (공개 규격):
  - NetQ validation check 카테고리: agents/bgp/evpn/interfaces/mlag(clag)/
    mtu/roce/vlan/vxlan 등 (docs.nvidia.com Cumulus NetQ Validate Operations)
  - SN5600: Spectrum-4 ASIC, 64 x 800GbE, 51.2Tb/s (nvidia.com Spectrum-X)
  - RoCE 모니터링 지표 축: PFC pause 프레임, ECN 마킹, congestion drop
Assumption (비공개·미확정 — 임의 정합값):
  - VR NVL72 사이트 converged Ethernet 구성: 사이트당 SN5600 스파인 2 +
    SU당 리프 2 (스토리지/N-S converged, GPU 패브릭은 UFM/IB 담당)
  - 리프 다운링크는 랙당 1x800G/리프 (트레이 aggregation, 64포트 정합용)
  - Cumulus Linux 5.x 버전 문자열, MTU 9216, eBGP unnumbered 언더레이
동작: ufm.py와 동일한 tick 캐시 패턴(TICK_SEC), 카운터는 tick_no 비례
단조 증가. STORE.seed_gen 변경(리셋) 시 재구축 + 데모 샘플 이상 1건 시드."""
import itertools
import time
import zlib
from collections import deque
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .store import STORE, CLUSTER, _iso
from . import fabric as _fabric

router = APIRouter(prefix="/netq/v1", tags=["netq"])

TICK_SEC = 2.5
SPINES_PER_SITE = 2                       # Assumption
LEAVES_PER_SU = 2                         # Assumption
MODEL = "SN5600"
OS_VERSION = "Cumulus Linux 5.11"         # Assumption: 임의 버전 문자열
INTERFACES_TOTAL = 64                     # Confirmed: SN5600 64x800GbE
MTU = 9216
CHECKS = ("bgp", "evpn", "vxlan", "mtu", "roce", "clag")
INJECT_KINDS = ("bgp_flap", "link_down", "pfc_storm", "validation_fail")

_SITE_PREF = {"gasan": "ga", "ansan": "an"}


def _crc(s: str) -> int:
    return zlib.crc32(s.encode())


def _su_num(su_id: str) -> int:
    return int(su_id.split("-")[1])


class FaultInject(BaseModel):
    kind: str                              # bgp_flap|link_down|pfc_storm|validation_fail
    target: Optional[str] = None           # switch name | su_id | check name


class FaultRecover(BaseModel):
    target: Optional[str] = None


# ── engine ────────────────────────────────────────────────────────────
class NetqEngine:
    def __init__(self):
        self.seed_gen = -1
        self.tick_no = 0
        self.last = 0.0
        self.switches: Dict[str, dict] = {}       # name -> switch
        self.site_names: Dict[str, str] = {}
        self.failed_checks: Dict[str, str] = {}    # check -> reason
        self.events: deque = deque(maxlen=500)
        self.alerts: Dict[tuple, dict] = {}
        self._alert_seq = itertools.count(1)
        self._event_seq = itertools.count(1)

    # ── topology (re)build ────────────────────────────────────────────
    def _ensure_topology(self) -> bool:
        cur = getattr(STORE, "seed_gen", 0)
        if cur == self.seed_gen:
            return False
        self.seed_gen = cur
        self.switches, self.site_names, self.failed_checks = {}, {}, {}
        self.events.clear()
        self.alerts = {}
        self.tick_no, self.last = 0, 0.0
        site_sus: Dict[str, Dict[str, int]] = {}      # site -> su -> racks
        for r in STORE.racks.values():
            self.site_names[r.site_id] = r.site
            sus = site_sus.setdefault(r.site_id, {})
            sus[r.su_id] = sus.get(r.su_id, 0) + 1
        order = [m["id"] for m in CLUSTER]
        for sid in sorted(site_sus, key=lambda s: order.index(s) if s in order
                          else 99):
            pref = _SITE_PREF.get(sid, sid[:2])
            for i in range(1, SPINES_PER_SITE + 1):
                self._add_switch(f"{pref}-eth-spine-{i:02d}", "spine", sid,
                                 None, 0)
            for su_id in sorted(site_sus[sid], key=_su_num):
                for i in range(1, LEAVES_PER_SU + 1):
                    self._add_switch(
                        f"{pref}-eth-leaf-su{_su_num(su_id)}-{i:02d}", "leaf",
                        sid, su_id, site_sus[sid][su_id])
        self._seed_sample()
        return True

    def _add_switch(self, name, role, sid, su_id, racks):
        self.switches[name] = {
            "name": name, "model": MODEL, "os": OS_VERSION, "role": role,
            "site": sid, "su_id": su_id, "racks": racks,
            "interfaces_total": INTERFACES_TOTAL, "interfaces_up": 0,
            "temperature_c": 40.0, "state": "ok", "fault": None}

    def _seed_sample(self):
        if not self.switches:
            return
        leaf = next((s for s in self.switches.values()
                     if s["role"] == "leaf"), None)
        if not leaf:
            return
        self._event("info", "bgp", leaf["name"],
                    "BGP session flap detected and re-established (sample)")
        self.alerts[("sample", leaf["name"])] = {
            "alert_id": f"netq-{next(self._alert_seq):04d}",
            "domain": "fabric", "severity": "minor",
            "resource": leaf["name"],
            "summary": f"NetQ bgp_flap on {leaf['name']} — "
                       "peer re-established (sample)",
            "at": _iso(), "state": "resolved", "source": "netq"}

    # ── alert/event helpers ───────────────────────────────────────────
    def _fire(self, key, severity, resource, summary):
        a = self.alerts.get(key)
        if a and a["state"] == "firing":
            a["severity"], a["summary"] = severity, summary
            return
        self.alerts[key] = {"alert_id": f"netq-{next(self._alert_seq):04d}",
                            "domain": "fabric", "severity": severity,
                            "resource": resource, "summary": summary,
                            "at": _iso(), "state": "firing", "source": "netq"}

    def _resolve(self, key):
        a = self.alerts.get(key)
        if a and a["state"] == "firing":
            a["state"], a["at"] = "resolved", _iso()

    def _event(self, severity, category, obj, description):
        self.events.appendleft({
            "event_id": next(self._event_seq), "at": _iso(),
            "severity": severity, "category": category,
            "object": obj, "description": description})

    # ── helpers ───────────────────────────────────────────────────────
    def _site_ok(self, sid: str, q: str) -> bool:
        return q.lower() in (sid, self.site_names.get(sid, "").lower())

    def _site_leaves(self, sid: str) -> List[dict]:
        return [s for s in self.switches.values()
                if s["role"] == "leaf" and s["site"] == sid]

    def _if_planned(self, sw: dict) -> int:
        """활성 인터페이스 계획 수 — leaf: 스파인 업링크 + 랙당 1 다운링크
        (Assumption), spine: 사이트 내 전 리프."""
        if sw["role"] == "leaf":
            return SPINES_PER_SITE + sw["racks"]
        return len(self._site_leaves(sw["site"]))

    # ── tick ──────────────────────────────────────────────────────────
    def tick(self, force: bool = False):
        with STORE.lock:
            if self._ensure_topology():
                force = True
            now = time.time()
            if not force and self.tick_no and now - self.last < TICK_SEC:
                return
            self.tick_no += 1
            self.last = now
            for sw in self.switches.values():
                planned = self._if_planned(sw)
                f = sw["fault"]
                sw["interfaces_up"] = max(0, planned - (1 if f == "link_down"
                                                        else 0))
                sw["state"] = ("degraded" if f in ("link_down", "bgp_flap",
                                                   "pfc_storm") else "ok")
                walk = ((_crc(sw["name"]) ^ self.tick_no * 2654435761)
                        % 40) / 10.0
                sw["temperature_c"] = round(
                    38.0 + walk + (5.0 if f == "pfc_storm" else 0.0), 1)
            self._eval_alerts()

    def _eval_alerts(self):
        for name, sw in self.switches.items():
            key = ("netq", name)
            f = sw["fault"]
            if f == "bgp_flap":
                self._fire(key, "major", name,
                           f"NetQ bgp_flap on {name} — BGP peer down/up "
                           "cycling, underlay unstable")
            elif f == "link_down":
                self._fire(key, "major", name,
                           f"NetQ link_down on {name} — interface down "
                           f"({sw['interfaces_up']}/{self._if_planned(sw)} up)")
            elif f == "pfc_storm":
                self._fire(key, "critical", name,
                           f"NetQ pfc_storm on {name} — PFC pause storm, "
                           "RoCE congestion drops rising")
            else:
                self._resolve(key)
        for check, reason in self.failed_checks.items():
            self._fire(("netq-check", check), "major", f"validation:{check}",
                       f"NetQ validation '{check}' failed — {reason}")
        for check in CHECKS:
            if check not in self.failed_checks:
                self._resolve(("netq-check", check))

    # ── site VNI/터널 파생 (STORE 세그먼트/테넌트 기반) ────────────────
    def _site_vnis(self, sid: str) -> int:
        t_sites = _fabric._tenant_sites()
        return sum(1 for n in STORE.tenant_networks.values()
                   if n.get("vni") and sid in t_sites.get(
                       n.get("tenant_id"), set()))

    # ── views ─────────────────────────────────────────────────────────
    def switch_view(self, sw: dict) -> dict:
        return {k: v for k, v in sw.items() if k != "fault"}

    def interfaces_for(self, sw: dict) -> List[dict]:
        out = []
        t = self.tick_no
        planned = self._if_planned(sw)
        peers: List[str] = []
        if sw["role"] == "leaf":
            pref = _SITE_PREF.get(sw["site"], sw["site"][:2])
            peers += [f"{pref}-eth-spine-{i:02d}"
                      for i in range(1, SPINES_PER_SITE + 1)]
            peers += [f"{sw['su_id']}-rack-{r:02d}"
                      for r in range(sw["racks"])]
        else:
            peers += [s["name"] for s in
                      sorted(self._site_leaves(sw["site"]),
                             key=lambda x: x["name"])]
        for i, peer in enumerate(peers[:planned]):
            name = f"swp{i + 1}"
            down = sw["fault"] == "link_down" and i == 0
            h = _crc(f"{sw['name']}/{name}")
            out.append({
                "interface": name, "state": "down" if down else "up",
                "speed": "800G", "mtu": MTU, "peer": peer,
                "counters": {
                    "rx_gbps": 0.0 if down else round(
                        120 + (h ^ t * 2654435761) % 3000 / 10.0, 1),
                    "tx_gbps": 0.0 if down else round(
                        110 + (h ^ (t + 7) * 2654435761) % 3000 / 10.0, 1),
                    "rx_errors": (h % 3) * (t // 80),
                    "tx_drops": (h % 2) * (t // 100),
                    "carrier_transitions": (h % 2) + (
                        t if sw["fault"] == "link_down" and i == 0 else 0),
                }})
        return out

    def protocols_for_site(self, sid: str) -> List[dict]:
        vnis = self._site_vnis(sid)
        leaves = self._site_leaves(sid)
        out = []
        for sw in sorted((s for s in self.switches.values()
                          if s["site"] == sid), key=lambda x: x["name"]):
            total = (SPINES_PER_SITE if sw["role"] == "leaf" else len(leaves))
            up = max(0, total - (1 if sw["fault"] == "bgp_flap" else 0))
            is_leaf = sw["role"] == "leaf"
            out.append({
                "switch": sw["name"], "role": sw["role"],
                "bgp_peers_up": up, "bgp_peers_total": total,
                "evpn_vnis": vnis if is_leaf else 0,
                "vxlan_tunnels": (max(0, len(leaves) - 1)
                                  if is_leaf and vnis else 0),
                "state": "ok" if up == total else "degraded"})
        return out

    def roce_for_site(self, sid: str) -> List[dict]:
        t = self.tick_no
        out = []
        for sw in sorted((s for s in self.switches.values()
                          if s["site"] == sid), key=lambda x: x["name"]):
            h = _crc(sw["name"])
            storm = sw["fault"] == "pfc_storm"
            out.append({
                "switch": sw["name"],
                "pfc_pause_rx": (h % 7) * t + (50000 * t if storm else 0),
                "pfc_pause_tx": (h % 5) * t + (42000 * t if storm else 0),
                "ecn_marked": (h % 11 + 2) * t + (9000 * t if storm else 0),
                "drops": (h % 2) * (t // 50) + (800 * t if storm else 0),
                "state": "storm" if storm else "ok"})
        return out

    def validation(self) -> List[dict]:
        now = _iso()
        n_sw = len(self.switches)
        flap = [s["name"] for s in self.switches.values()
                if s["fault"] == "bgp_flap"]
        linkdown = [s["name"] for s in self.switches.values()
                    if s["fault"] == "link_down"]
        storm = [s["name"] for s in self.switches.values()
                 if s["fault"] == "pfc_storm"]
        vnis = sum(self._site_vnis(sid) for sid in self.site_names)
        results = {
            "bgp": ("fail", f"sessions down on {', '.join(flap)}") if flap
            else ("pass", f"all BGP sessions established ({n_sw} switches)"),
            "evpn": ("warn", f"EVPN routes unstable on {', '.join(flap)}")
            if flap else ("pass", f"{vnis} VNIs consistent across leaves"),
            "vxlan": ("warn", f"tunnel endpoint unreachable via "
                              f"{', '.join(linkdown)}") if linkdown
            else ("pass", f"{vnis} VNIs / VTEP reachability OK"),
            "mtu": ("pass", f"MTU {MTU} consistent on all fabric links"),
            "roce": ("fail", f"PFC pause storm on {', '.join(storm)}")
            if storm else ("pass", "lossless RoCE config consistent "
                                   "(PFC/ECN enabled)"),
            "clag": ("pass", "no peerlink inconsistencies"),
        }
        for check, reason in self.failed_checks.items():
            results[check] = ("fail", reason)
        return [{"check": c, "result": results[c][0],
                 "detail": results[c][1], "checked_at": now}
                for c in CHECKS]

    def alerts_for_obs(self) -> List[dict]:
        """obs alerts() merge용 공개 함수 — domain 'fabric' 알람 전체."""
        self.tick()
        return [dict(a) for a in self.alerts.values()]


ENGINE = NetqEngine()


# ── 1) switches ───────────────────────────────────────────────────────
@router.get("/switches")
def switches(site: Optional[str] = None):
    ENGINE.tick()
    with STORE.lock:
        sws = list(ENGINE.switches.values())
        if site:
            sws = [s for s in sws if ENGINE._site_ok(s["site"], site)]
        return {"count": len(sws),
                "switches": [ENGINE.switch_view(s) for s in sws]}


# ── 2) interfaces ─────────────────────────────────────────────────────
@router.get("/interfaces")
def interfaces(switch: str):
    ENGINE.tick()
    with STORE.lock:
        sw = ENGINE.switches.get(switch)
        if not sw:
            raise HTTPException(404, f"switch {switch} not found")
        ifs = ENGINE.interfaces_for(sw)
        return {"switch": switch, "count": len(ifs), "interfaces": ifs}


# ── 3) protocols (BGP/EVPN/VXLAN) ─────────────────────────────────────
@router.get("/protocols")
def protocols(site: Optional[str] = None):
    ENGINE.tick()
    with STORE.lock:
        sids = [s for s in ENGINE.site_names
                if not site or ENGINE._site_ok(s, site)]
        out = [p for sid in sids for p in ENGINE.protocols_for_site(sid)]
        return {"count": len(out), "protocols": out}


# ── 4) RoCE counters ──────────────────────────────────────────────────
@router.get("/roce")
def roce(site: Optional[str] = None):
    ENGINE.tick()
    with STORE.lock:
        sids = [s for s in ENGINE.site_names
                if not site or ENGINE._site_ok(s, site)]
        out = [r for sid in sids for r in ENGINE.roce_for_site(sid)]
        return {"count": len(out), "roce": out}


# ── 5) validation ─────────────────────────────────────────────────────
@router.get("/validation")
def validation():
    ENGINE.tick()
    with STORE.lock:
        checks = ENGINE.validation()
        return {"count": len(checks),
                "summary": {"pass": sum(1 for c in checks
                                        if c["result"] == "pass"),
                            "warn": sum(1 for c in checks
                                        if c["result"] == "warn"),
                            "fail": sum(1 for c in checks
                                        if c["result"] == "fail")},
                "checks": checks}


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
        if body.kind == "validation_fail":
            check = body.target if body.target in CHECKS else "mtu"
            ENGINE.failed_checks[check] = "validation_fail injected (demo)"
            target = f"validation:{check}"
            ENGINE._event("major", check, target,
                          f"validation '{check}' forced to fail (injected)")
        else:
            sw = None
            if body.target:
                sw = ENGINE.switches.get(body.target) or next(
                    (s for s in ENGINE.switches.values()
                     if s["su_id"] == body.target), None)
            else:
                sw = next((s for s in ENGINE.switches.values()
                           if s["role"] == "leaf"), None)
            if not sw:
                raise HTTPException(404, f"switch target '{body.target}' "
                                         "not found")
            sw["fault"] = body.kind
            target = sw["name"]
            ENGINE._event("critical" if body.kind == "pfc_storm" else "major",
                          {"bgp_flap": "bgp", "link_down": "interfaces",
                           "pfc_storm": "roce"}[body.kind], target,
                          f"{body.kind} injected on {target}")
        STORE.event("critical", "NeoCloudEmulator.1.0.NetqFaultInjected",
                    [body.kind, target])
    ENGINE.tick(force=True)
    with STORE.lock:
        return {"injected": body.kind, "target": target,
                "validation": ENGINE.validation()}


@router.post("/faults/recover")
def recover(body: FaultRecover = FaultRecover()):
    ENGINE.tick()
    with STORE.lock:
        cleared = []
        for sw in ENGINE.switches.values():
            if sw["fault"] and (not body.target or body.target in (
                    sw["name"], sw["su_id"])):
                cleared.append(sw["name"])
                sw["fault"] = None
        for check in list(ENGINE.failed_checks):
            if not body.target or body.target in (check,
                                                  f"validation:{check}"):
                cleared.append(f"validation:{check}")
                del ENGINE.failed_checks[check]
        for t in cleared:
            ENGINE._event("info", "recovery", t, "fault recovered")
        if cleared:
            STORE.event("info", "NeoCloudEmulator.1.0.NetqFaultRecovered",
                        [", ".join(cleared)])
    ENGINE.tick(force=True)
    with STORE.lock:
        return {"recovered": cleared, "validation": ENGINE.validation()}
