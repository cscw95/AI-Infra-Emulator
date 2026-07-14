"""UFM Enterprise 에뮬레이터 — VR NVL72 GPU 패브릭(InfiniBand)을 개별 스위치
수준까지 에뮬레이션한다 (prefix /ufm/v1).

리소스 모델은 NVIDIA UFM Enterprise REST API의 리소스 축(systems / ports /
links / pkeys / unhealthy ports / events)을 따르고, 포트 카운터 명은 IBTA
표준 PortCounters(SymbolErrorCounter, LinkErrorRecoveryCounter,
LinkDownedCounter, PortXmitWait, PortRcvErrors)를 축약해 사용한다.

Confirmed (공개 규격):
  - UFM Enterprise REST 리소스: systems/ports/links/pkeys/unhealthy
    ports/alarms/events (docs.nvidia.com UFM Enterprise REST API Guide)
  - Quantum-X800 Q3400: 144 x 800Gb/s XDR 포트(72 OSFP 케이지), Quantum-3
    ASIC, 115.2Tb/s, SHARPv4 (nvidia.com Quantum-X800 datasheet)
  - IB 표준 포트 에러 카운터 명 (IBTA / UFM telemetry counter set)
Assumption (비공개·미확정 — 임의 정합값):
  - VR NVL72 세대 rail-optimized 사이트 패브릭 구성: plane당 스파인
    gasan 2 / ansan 4, SU당 plane별 리프 4대 (기존 fabric.py 토폴로지와 정합)
  - 트레이 HCA: ConnectX-9 dual-plane 1포트/plane (CX9 세부 미공개)
  - FW 버전 문자열, 온도 범위, XDR BER 기준치(1e-14 근방), GUID 인코딩
동작: ObsEngine과 동일한 tick 캐시 패턴 — TICK_SEC 주기로만 상태를 갱신하고
카운터는 tick_no에 비례해 단조 증가(요청당 전량 재계산 금지).
STORE.seed_gen 변경(리셋) 시 토폴로지 재구축 + 데모 샘플 이상 1건 시드."""
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
PLANES = ("a", "b")                       # 듀얼 플레인 (fabric.py Fabric-A/B)
SPINES_PER_PLANE = {"gasan": 2, "ansan": 4}   # Assumption (사이트 규모 비례)
LEAVES_PER_SU_PLANE = 4                   # rail-optimized 4 rails (fabric.py 정합)
MODEL = "Quantum-X800 Q3400"
FW_VERSION = "31.2014.2036"               # Assumption: 임의 FW 문자열
PORTS_TOTAL = 144                         # Confirmed: Q3400 144x800G XDR
SPEED = "XDR 800G"
HCA_MODEL = "ConnectX-9"                  # Assumption: VR 세대 HCA
BER_OK = 1e-14                            # Assumption: XDR post-FEC BER 근방
BER_DEGRADED = 3e-9
INJECT_KINDS = ("link_degrade", "link_down", "port_flap", "switch_down")

_SITE_PREF = {"gasan": "ga", "ansan": "an"}


def _crc(s: str) -> int:
    return zlib.crc32(s.encode())


def _guid(name: str) -> str:
    """Assumption: NVIDIA OUI(0x0c42)風 유사 GUID — 이름 기반 결정적 생성."""
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
        self.hca: List[dict] = []                  # SU 집약 leaf↔HCA 링크
        self.leaf_trays: Dict[str, List[str]] = {}  # leaf name -> tray_ids
        self.site_names: Dict[str, str] = {}
        self.events: deque = deque(maxlen=500)
        self.alerts: Dict[tuple, dict] = {}
        self._alert_seq = itertools.count(1)
        self._event_seq = itertools.count(1)

    # ── topology (re)build — STORE reseed 감지 ────────────────────────
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
        # 사이트/SU 구성은 STORE.racks에서 파생 (NICO_RACKS_LIMIT 반영)
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
                        # rail-optimized: SU 트레이를 4개 리프에 분배
                        self.leaf_trays[lname] = trays[i - 1::LEAVES_PER_SU_PLANE]
                        for sp in spines:                 # leaf↔spine 전수
                            lid = f"{lname}--{sp['name']}"
                            self.links[lid] = {
                                "link_id": lid, "src": lname,
                                "dst": sp["name"], "plane": plane.upper(),
                                "su_id": su_id, "site": sid,
                                "fault": None,
                                "flaps_24h": _crc(lid) % 2,   # 평시 잔여 플랩
                            }
                    # leaf↔HCA — SU 집약 카운트 (트레이별 개별 링크는 요약)
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
        """리셋 직후 이벤트/알람 이력이 비지 않도록 resolved 샘플 1건 시드."""
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

    # ── alert/event helpers (ObsEngine _fire/_resolve 패턴) ───────────
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

    # ── tick — 상태/온도/플랩 갱신 (TICK_SEC 캐시) ─────────────────────
    def tick(self, force: bool = False):
        with STORE.lock:
            if self._ensure_topology():
                force = True
            now = time.time()
            if not force and self.tick_no and now - self.last < TICK_SEC:
                return
            self.tick_no += 1
            self.last = now
            # 링크 상태/플랩
            for l in self.links.values():
                if l["fault"] == "port_flap":
                    l["flaps_24h"] += 1
            # 스위치 상태·온도·활성 포트
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
        """스위치의 연결 포트 계획 [{number, peer, link_id|tray}] (경량)."""
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
        else:                                   # spine: 사이트/plane 내 전 리프
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
        else:                                   # leaf↔HCA 포트
            state = "down" if sw["fault"] == "switch_down" else "active"
        # 카운터 — tick_no 비례 단조 증가 (폴링마다 증가), 장애 시 급증
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

    # ── health / obs 연동 ─────────────────────────────────────────────
    def health(self, site: Optional[str] = None) -> dict:
        sws = [s for s in self.switches.values()
               if not site or self._site_ok(s["site"], site)]
        lks = [self.link_view(l) for l in self.links.values()
               if not site or self._site_ok(l["site"], site)]
        hca = [h for h in self.hca
               if not site or self._site_ok(h["site"], site)]
        down_sw = [s for s in sws if s["state"] == "down"]
        # leaf down이면 그 리프의 HCA 링크도 down으로 집계
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
        """(site_id, su_id, 'A'|'B') → up|degraded|down — fabric.py 토폴로지
        팝업이 UFM 링크 상태와 일치하도록 노출하는 공개 함수."""
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
        """obs alerts() merge용 공개 함수 — domain 'fabric' 알람 전체."""
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
        # leaf↔HCA는 SU 집약 (개별 트레이 링크 2,520개는 요약 필드로만)
        return {"count": len(lks), "links": lks,
                "hca_su_summary": hca,
                "hca_links_total": sum(h["links_total"] for h in hca)}


# ── 4) pkeys — 실제 DPU attachment 기반 (fabric.py P_Key 로직 재사용) ──
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
