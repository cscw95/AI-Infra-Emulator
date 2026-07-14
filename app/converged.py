"""Converged Network 플레인 에뮬레이터 — VR NVL72 converged rail(스토리지·
관리 N-S 트래픽)을 테넌트 스토리지 경로 수준으로 에뮬레이션한다
(prefix /converged/v1).

Confirmed (공개 규격):
  - GB300 NVL72 Enterprise RA: 컴퓨트 트레이당 E-W(GPU rail)는 ConnectX
    SuperNIC, N-S "converged"(스토리지+클라우드/관리)는 BlueField DPU
    dual-port 담당 — E-W/N-S 역할 분리 (docs.nvidia.com NVL72 AI Factory RA)
  - Vera Rubin 플랫폼: ConnectX-9 SuperNIC(1.6Tb/s), BlueField-4(64-core,
    CX-9 co-packaged, 800Gb/s) 발표 (NVIDIA Newsroom, CES 2026)
  - Spectrum-X Ethernet: per-packet adaptive routing + ECN/PFC 기반
    congestion control, effective bandwidth ~95% 주장 (NVIDIA whitepaper)
Assumption (비공개·미확정 — 임의 정합값):
  - VR NVL72 트레이당 converged rail 경로: CX-9 dual-port 일부 + BF-4 경유
    스토리지 경로 2 + 관리 경로 1 (GB300 BF-3 dual-port 패턴 유추)
  - 경로당 400Gb/s(800G dual-port 반분), oversubscription 1.5:1
  - latency/ECN/PFC 기준치, 혼잡 시 파형
동작: ufm/netq와 동일한 tick 캐시 패턴(TICK_SEC). 테넌트 스토리지 경로는
STORE attachment에서 파생(vast.py와 동일 축), VNI 세그먼트는
STORE.tenant_networks에서 파생. STORE.seed_gen 변경(리셋) 시 재구축."""
import itertools
import time
import zlib
from collections import deque
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .store import STORE, CLUSTER, GPU_PER_TRAY, _iso

router = APIRouter(prefix="/converged/v1", tags=["converged"])

TICK_SEC = 2.5
FABRIC = "Spectrum-X"
NIC_MODEL = "ConnectX-9 (converged dual-port)"   # Assumption: VR 세대
DPU_MODEL = "BlueField-4"                        # Confirmed 발표 / RA는 유추
STORAGE_PATHS_PER_TRAY = 2       # Assumption: CX-9 dual-port 스토리지 경로
MGMT_PATHS_PER_TRAY = 1          # Assumption: BF-4 관리(OOB/in-band) 경로
PATH_BW_GBPS = 400.0             # Assumption: 800G dual-port 반분
OVERSUB_RATIO = 1.5              # Assumption: converged rail 오버섭
LAT_US_BASE = 90.0               # Assumption: RoCE 스토리지 경로 RTT 근방
INJECT_KINDS = ("path_degrade", "storage_congestion")

_K = 2654435761


def _crc(s: str) -> int:
    return zlib.crc32(s.encode())


class FaultInject(BaseModel):
    kind: str                     # path_degrade|storage_congestion
    target: Optional[str] = None  # tenant_id | su_id | site_id | None


class FaultRecover(BaseModel):
    target: Optional[str] = None


# ── engine ────────────────────────────────────────────────────────────
class ConvergedEngine:
    def __init__(self):
        self.seed_gen = -1
        self.tick_no = 0
        self.last = 0.0
        self.site_names: Dict[str, str] = {}
        self.site_trays: Dict[str, int] = {}
        self.congested: Dict[str, bool] = {}      # site_id -> 혼잡 주입
        self.degraded: set = set()                # (tenant, su, site) 저하 경로
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
        self.site_names, self.site_trays = {}, {}
        self.congested, self.degraded = {}, set()
        self.events.clear()
        self.alerts = {}
        self.tick_no, self.last = 0, 0.0
        for r in STORE.racks.values():
            self.site_names[r.site_id] = r.site
            self.site_trays[r.site_id] = (self.site_trays.get(r.site_id, 0)
                                          + len(r.trays))
            self.congested.setdefault(r.site_id, False)
        self._seed_sample()
        return True

    def _seed_sample(self):
        sid = next(iter(self.site_names), None)
        if not sid:
            return
        self._event("info", "roce", f"converged-{sid}",
                    "transient ECN marking burst during checkpoint window — "
                    "adaptive routing rebalanced (sample)")

    # ── alert/event helpers ───────────────────────────────────────────
    def _fire(self, key, severity, resource, summary):
        a = self.alerts.get(key)
        if a and a["state"] == "firing":
            a["severity"], a["summary"] = severity, summary
            return
        self.alerts[key] = {"alert_id": f"cnv-{next(self._alert_seq):04d}",
                            "domain": "storage", "severity": severity,
                            "resource": resource, "summary": summary,
                            "at": _iso(), "state": "firing",
                            "source": "converged"}

    def _resolve(self, key):
        a = self.alerts.get(key)
        if a and a["state"] == "firing":
            a["state"], a["at"] = "resolved", _iso()

    def _event(self, severity, category, obj, description):
        self.events.appendleft({
            "event_id": next(self._event_seq), "at": _iso(),
            "severity": severity, "category": category,
            "object": obj, "description": description})

    def _site_ok(self, sid: str, q: str) -> bool:
        return q.lower() in (sid, self.site_names.get(sid, "").lower())

    # ── 파생 축 (STORE.lock 보유 필요) ────────────────────────────────
    def tenant_paths_src(self) -> Dict[tuple, set]:
        """(tenant_id, su_id, site_id) -> attached tray_ids."""
        out: Dict[tuple, set] = {}
        for a in STORE.attachments.values():
            tid = a.get("tenant_id")
            d = STORE.dpus.get(a.get("dpu_id", ""))
            if not tid or not d:
                continue
            tr = STORE.trays.get(d.compute_tray_id)
            rk = STORE.racks.get(tr.rack_id) if tr else None
            if rk:
                out.setdefault((tid, rk.su_id, rk.site_id),
                               set()).add(tr.tray_id)
        return out

    def _site_vnis(self, sid: str) -> int:
        """사이트에 존재하는 테넌트의 VNI 세그먼트 수 (tenant_networks 파생)."""
        t_sites = {}
        for (tid, su, s), _ in self.tenant_paths_src().items():
            t_sites.setdefault(tid, set()).add(s)
        return sum(1 for n in STORE.tenant_networks.values()
                   if n.get("vni") and sid in t_sites.get(
                       n.get("tenant_id"), set()))

    # ── tick — 알람 평가 (TICK_SEC 캐시) ──────────────────────────────
    def tick(self, force: bool = False):
        with STORE.lock:
            if self._ensure_topology():
                force = True
            now = time.time()
            if not force and self.tick_no and now - self.last < TICK_SEC:
                return
            self.tick_no += 1
            self.last = now
            self._eval_alerts()

    def _eval_alerts(self):
        for sid, cong in self.congested.items():
            key = ("congestion", sid)
            if cong:
                self._fire(key, "major", f"converged-{sid}",
                           f"Converged storage_congestion on {sid} — "
                           "PFC pause/ECN marking storm on storage rail, "
                           "effective BW reduced")
            else:
                self._resolve(key)
        active = {("path",) + k for k in self.degraded}
        for k in self.degraded:
            tid, su, sid = k
            self._fire(("path",) + k, "major", f"{tid}@{su}",
                       f"Converged path_degrade — {tid} storage path "
                       f"{su} → vast-{sid} degraded (latency/ECN rising)")
        for key in list(self.alerts):
            if key[0] == "path" and key not in active:
                self._resolve(key)

    # ── views ─────────────────────────────────────────────────────────
    def overview_rows(self, site: Optional[str] = None) -> List[dict]:
        order = [m["id"] for m in CLUSTER]
        out = []
        for sid in sorted(self.site_names,
                          key=lambda s: order.index(s) if s in order else 99):
            if site and not self._site_ok(sid, site):
                continue
            trays = self.site_trays.get(sid, 0)
            st_total = trays * STORAGE_PATHS_PER_TRAY
            mg_total = trays * MGMT_PATHS_PER_TRAY
            n_deg = sum(len(self.tenant_paths_src().get(k, ()))
                        for k in self.degraded if k[2] == sid)
            cong = self.congested.get(sid, False)
            out.append({
                "site": sid, "site_name": self.site_names[sid],
                "fabric": FABRIC, "nic": NIC_MODEL, "dpu": DPU_MODEL,
                "vni_segments": self._site_vnis(sid),
                "storage_paths": {"active": max(0, st_total - n_deg),
                                  "total": st_total},
                "mgmt_paths": {"active": mg_total, "total": mg_total},
                "oversub_ratio": OVERSUB_RATIO,
                "path_bw_gbps": PATH_BW_GBPS,
                "state": ("congested" if cong else
                          "degraded" if n_deg else "ok"),
            })
        return out

    def path_rows(self, tenant: Optional[str] = None) -> List[dict]:
        t = self.tick_no
        out = []
        for (tid, su, sid), trays in sorted(self.tenant_paths_src().items()):
            if tenant and tid != tenant:
                continue
            h = _crc(f"{tid}/{su}/{sid}")
            gpus = len(trays) * GPU_PER_TRAY
            cong = self.congested.get(sid, False)
            deg = (tid, su, sid) in self.degraded
            wave = ((h ^ t * _K) % 400) / 1000.0          # 0.00~0.40
            bw = min(len(trays) * PATH_BW_GBPS * 0.8,
                     gpus * 12.5 * (0.6 + wave))          # RA baseline 근방
            if cong:
                bw *= 0.55                                 # 혼잡 → 유효 BW 저하
            if deg:
                bw *= 0.5
            lat = LAT_US_BASE * (1.0 + wave)
            if cong:
                lat *= 4.0
            if deg:
                lat *= 3.0
            # ECN/PFC — tick 비례 단조 증가, 혼잡/저하 시 급증
            ecn = (h % 9 + 1) * t + (12000 * t if cong else 0) \
                + (3000 * t if deg else 0)
            pfc = (h % 4) * (t // 3) + (30000 * t if cong else 0) \
                + (1500 * t if deg else 0)
            out.append({
                "tenant_id": tid, "src_su": su, "site": sid,
                "dst": f"vast-{sid}", "trays": len(trays), "gpus": gpus,
                "bw_gbps": round(bw, 1),
                "latency_us": round(lat, 1),
                "ecn_marks": ecn, "pfc_pause": pfc,
                "state": ("congested" if cong else
                          "degraded" if deg else "active"),
            })
        return out

    def alerts_for_obs(self) -> List[dict]:
        """obs alerts() merge용 — domain 'storage' 알람 전체."""
        self.tick()
        return [dict(a) for a in self.alerts.values()]


ENGINE = ConvergedEngine()


# ── 1) overview ───────────────────────────────────────────────────────
@router.get("/overview")
def overview(site: Optional[str] = None):
    ENGINE.tick()
    with STORE.lock:
        rows = ENGINE.overview_rows(site)
        return {"count": len(rows), "sites": rows}


# ── 2) tenant storage paths ───────────────────────────────────────────
@router.get("/paths")
def paths(tenant: Optional[str] = None):
    ENGINE.tick()
    with STORE.lock:
        rows = ENGINE.path_rows(tenant)
        return {"count": len(rows), "paths": rows}


# ── 3) events ─────────────────────────────────────────────────────────
@router.get("/events")
def events(limit: int = 50):
    ENGINE.tick()
    with STORE.lock:
        return list(ENGINE.events)[:limit]


# ── 4) fault inject / recover ─────────────────────────────────────────
@router.post("/faults/inject")
def inject(body: FaultInject):
    ENGINE.tick()
    with STORE.lock:
        if body.kind not in INJECT_KINDS:
            raise HTTPException(400, f"kind must be one of {list(INJECT_KINDS)}")
        if body.kind == "storage_congestion":
            sid = None
            if body.target:
                sid = next((s for s in ENGINE.site_names
                            if ENGINE._site_ok(s, body.target)), None)
            else:
                sid = next(iter(ENGINE.site_names), None)
            if not sid:
                raise HTTPException(404, f"site target '{body.target}' "
                                         "not found")
            ENGINE.congested[sid] = True
            target = f"converged-{sid}"
            ENGINE._event("major", "roce", target,
                          "storage_congestion injected — PFC/ECN storm on "
                          "storage rail")
        else:                                        # path_degrade
            keys = list(ENGINE.tenant_paths_src())
            if body.target:
                keys = [k for k in keys if body.target in k]
            if not keys:
                raise HTTPException(404, "no tenant storage path matches "
                                         f"target '{body.target}' — attach "
                                         "a tenant first")
            k = keys[0]
            ENGINE.degraded.add(k)
            target = f"{k[0]}@{k[1]}"
            ENGINE._event("major", "path", target,
                          f"path_degrade injected — {k[0]} {k[1]} → "
                          f"vast-{k[2]}")
        STORE.event("critical", "NeoCloudEmulator.1.0.ConvergedFaultInjected",
                    [body.kind, target])
    ENGINE.tick(force=True)
    with STORE.lock:
        return {"injected": body.kind, "target": target,
                "overview": ENGINE.overview_rows()}


@router.post("/faults/recover")
def recover(body: FaultRecover = FaultRecover()):
    ENGINE.tick()
    with STORE.lock:
        cleared = []
        for sid, cong in list(ENGINE.congested.items()):
            if cong and (not body.target or ENGINE._site_ok(sid, body.target)):
                ENGINE.congested[sid] = False
                cleared.append(f"converged-{sid}")
        for k in list(ENGINE.degraded):
            if not body.target or body.target in k:
                ENGINE.degraded.discard(k)
                cleared.append(f"{k[0]}@{k[1]}")
        for t in cleared:
            ENGINE._event("info", "recovery", t, "fault recovered")
        if cleared:
            STORE.event("info", "NeoCloudEmulator.1.0.ConvergedFaultRecovered",
                        [", ".join(cleared)])
    ENGINE.tick(force=True)
    with STORE.lock:
        return {"recovered": cleared, "overview": ENGINE.overview_rows()}
