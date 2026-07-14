"""VAST Data VMS 에뮬레이터 — 사이트별 AI Storage(VAST 클러스터 1식)를
VMS(VAST Management System) 개념 모델 수준으로 에뮬레이션한다 (prefix /vast/v1).

리소스 축은 VAST VMS REST API의 공개 컬렉션(views / quotas / clusters /
monitors)을 따르되, 경로는 에뮬레이터 로컬 규약(/vast/v1)을 쓴다.

Confirmed (공개 규격):
  - VMS REST 컬렉션 존재: views/clusters/monitors/quotas/viewpolicies
    (vast-data/vastpy SDK, VAST KB "The VAST REST API")
  - DASE(Disaggregated Shared-Everything): stateless CNode(CBox)와
    NVMe-oF JBOF DBox(SCM+QLC) 분리 (vastdata.com "How it works")
  - Similarity-based data reduction — 고객 대다수 ~3:1 이상 DRR 주장
    (VAST 블로그 "Similarity Reduction: Report from the Field")
  - VAST Cluster 버전 네이밍 5.x (VAST Cluster 5.3 Release Notes)
  - GPU당 스토리지 대역 baseline ~12.5Gb/s, 체크포인트는 write burst 성격
    (NVIDIA DGX SuperPOD storage RA)
Assumption (비공개·미확정 — 임의 정합값):
  - VMS API 버전 경로/알람 스키마(클러스터 내장 Swagger에만 존재) — 단순화
  - 사이트별 클러스터 규모: CBox 8 · DBox 10, raw 20PB / usable 14PB
  - CBox당 프런트엔드 대역 400Gb/s, 뷰 쿼터 GPU당 10TB, QoS GPU당 25Gb/s
  - 체크포인트 write burst 주기(12 tick 중 3 tick), p99 지연 기준치
동작: ufm/netq와 동일한 tick 캐시 패턴(TICK_SEC) — 카운터·부하는 tick_no
기반 결정적 파형으로 폴링마다 동적. 부하는 테넌트 활성 GPU 수(STORE
attachment 파생)에 비례. STORE.seed_gen 변경(리셋) 시 재구축 + 샘플 시드."""
import itertools
import time
import zlib
from collections import deque
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .store import STORE, CLUSTER, GPU_PER_TRAY, _iso

router = APIRouter(prefix="/vast/v1", tags=["vast"])

TICK_SEC = 2.5
CBOXES = 8                        # Assumption: 사이트별 컴퓨트 인클로저 수
DBOXES = 10                       # Assumption: 사이트별 용량 인클로저 수
RAW_PB = 20.0                     # Assumption
USABLE_PB = 14.0                  # Assumption (erasure coding 후)
VERSION = "5.3.0"                 # Confirmed naming: VAST Cluster 5.3
CBOX_BW_GBPS = 400.0              # Assumption: CBox당 프런트엔드 대역
QUOTA_TB_PER_GPU = 10.0           # Assumption: dataset+ckpt 쿼터
QOS_BW_GBPS_PER_GPU = 25.0        # Assumption: baseline 12.5Gb/s x2 헤드룸
QOS_IOPS_K_PER_GPU = 8.0          # Assumption
READ_GBPS_PER_GPU = 12.5          # Confirmed 계열: NVIDIA RA baseline
WRITE_GBPS_PER_GPU = 2.0          # Assumption: 평시 로그/셔플 쓰기
CKPT_WRITE_GBPS_PER_GPU = 18.0    # Assumption: 체크포인트 덤프 버스트
CKPT_PERIOD = 12                  # Assumption: 버스트 주기 (tick)
CKPT_TICKS = 3                    # Assumption: 버스트 지속 (tick)
LAT_P99_BASE_MS = 0.8             # Assumption: NVMe-oF/RDMA p99 근방
CAP_PRESSURE_PCT = 93.0           # capacity_pressure 주입 시 사용률
INJECT_KINDS = ("nvme_drive_fail", "cbox_down", "latency_spike",
                "capacity_pressure")

_K = 2654435761                   # Knuth 곱셈 해시 (ufm/netq와 동일 패턴)


def _crc(s: str) -> int:
    return zlib.crc32(s.encode())


class FaultInject(BaseModel):
    kind: str                     # nvme_drive_fail|cbox_down|latency_spike|capacity_pressure
    target: Optional[str] = None  # cluster name(vast-gasan) | site_id | None(first)


class FaultRecover(BaseModel):
    target: Optional[str] = None


# ── engine ────────────────────────────────────────────────────────────
class VastEngine:
    def __init__(self):
        self.seed_gen = -1
        self.tick_no = 0
        self.last = 0.0
        self.clusters: Dict[str, dict] = {}      # name -> cluster
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
        self.clusters, self.site_names = {}, {}
        self.events.clear()
        self.alerts = {}
        self.tick_no, self.last = 0, 0.0
        sids = []
        for r in STORE.racks.values():
            self.site_names[r.site_id] = r.site
            if r.site_id not in sids:
                sids.append(r.site_id)
        order = [m["id"] for m in CLUSTER]
        for sid in sorted(sids, key=lambda s: order.index(s) if s in order
                          else 99):
            name = f"vast-{sid}"
            h = _crc(name)
            self.clusters[name] = {
                "name": name, "site": sid, "site_name": self.site_names[sid],
                "cboxes": CBOXES, "dboxes": DBOXES,
                "raw_pb": RAW_PB, "usable_pb": USABLE_PB,
                # Assumption: 기저 사용량(플랫폼/타 워크로드) 7~8PB 근방
                "base_used_pb": 7.0 + (h // 7 % 10) / 10.0,
                "drr": round(2.8 + (_crc(name + "/drr") % 50) / 100.0, 2),  # ~3:1
                "version": VERSION,
                "fault": None, "fault_at": None,
                "failed_drives": 0,
            }
        self._seed_sample()
        return True

    def _seed_sample(self):
        """리셋 직후 이벤트/알람 이력이 비지 않도록 resolved 샘플 1건 시드."""
        c = next(iter(self.clusters.values()), None)
        if not c:
            return
        self._event("minor", "hardware", c["name"],
                    "NVMe SSD wear threshold crossed — drive replaced, "
                    "RAID rebuild completed (sample)")
        self.alerts[("sample", c["name"])] = {
            "alert_id": f"vast-{next(self._alert_seq):04d}",
            "domain": "storage", "severity": "minor",
            "resource": c["name"],
            "summary": f"VAST nvme_drive_fail on {c['name']} — rebuild "
                       "completed (sample)",
            "at": _iso(), "state": "resolved", "source": "vast"}

    # ── alert/event helpers (UfmEngine 패턴) ──────────────────────────
    def _fire(self, key, severity, resource, summary):
        a = self.alerts.get(key)
        if a and a["state"] == "firing":
            a["severity"], a["summary"] = severity, summary
            return
        self.alerts[key] = {"alert_id": f"vast-{next(self._alert_seq):04d}",
                            "domain": "storage", "severity": severity,
                            "resource": resource, "summary": summary,
                            "at": _iso(), "state": "firing", "source": "vast"}

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
        return q.lower() in (sid, self.site_names.get(sid, "").lower(),
                             f"vast-{sid}")

    # ── tenant/뷰 파생 (STORE attachment 기반) ────────────────────────
    def tenant_site_trays(self) -> Dict[tuple, set]:
        """(tenant_id, site_id) -> attached tray_ids (STORE.lock 보유 필요)."""
        out: Dict[tuple, set] = {}
        for a in STORE.attachments.values():
            tid = a.get("tenant_id")
            d = STORE.dpus.get(a.get("dpu_id", ""))
            if not tid or not d:
                continue
            tr = STORE.trays.get(d.compute_tray_id)
            rk = STORE.racks.get(tr.rack_id) if tr else None
            if rk:
                out.setdefault((tid, rk.site_id), set()).add(tr.tray_id)
        return out

    def view_rows(self, tenant: Optional[str] = None) -> List[dict]:
        rows = []
        t = self.tick_no
        for (tid, sid), trays in sorted(self.tenant_site_trays().items()):
            if tenant and tid != tenant:
                continue
            gpus = len(trays) * GPU_PER_TRAY
            quota = max(50.0, gpus * QUOTA_TB_PER_GPU)
            h = _crc(f"{tid}/{sid}")
            # 사용량: 시작 30% 근방에서 tick에 따라 완만 증가 (Assumption)
            used = quota * min(0.88, 0.30 + (h % 15) / 100.0 + 0.002 * t)
            rows.append({
                "path": f"/{tid}/dataset", "tenant_id": tid,
                "cluster": f"vast-{sid}", "site": sid,
                "protocols": ["NFS", "S3"],
                "capacity_tb": round(quota, 1),
                "used_tb": round(used, 1),
                "quota_tb": round(quota, 1),
                "qos": {"bw_gbps": round(gpus * QOS_BW_GBPS_PER_GPU, 1),
                        "iops_k": round(gpus * QOS_IOPS_K_PER_GPU, 1)},
                "gpus": gpus, "clients": len(trays),
                "state": "active",
            })
        return rows

    # ── 성능 모델 — 테넌트 활성 GPU 비례 + 체크포인트 버스트 ──────────
    def _view_perf(self, row: dict) -> dict:
        t = self.tick_no
        h = _crc(row["path"] + row["cluster"])
        gpus = row["gpus"]
        # 위상은 뷰별로 어긋나게(h) — 폴링마다 파형이 움직인다
        wave = ((h ^ t * _K) % 400) / 1000.0          # 0.00~0.40
        burst = ((t + h) % CKPT_PERIOD) < CKPT_TICKS  # 체크포인트 write burst
        read = gpus * READ_GBPS_PER_GPU * (0.55 + wave) * (0.35 if burst else 1.0)
        write = gpus * (CKPT_WRITE_GBPS_PER_GPU if burst
                        else WRITE_GBPS_PER_GPU) * (0.8 + wave)
        return {"read": read, "write": write, "burst": burst,
                "riops": gpus * (3.0 + wave * 8),      # K IOPS
                "wiops": gpus * ((6.0 if burst else 1.2) + wave * 2)}

    def perf_rows(self, site: Optional[str] = None) -> List[dict]:
        t = self.tick_no
        out = []
        views = self.view_rows()
        for c in self.clusters.values():
            if site and not self._site_ok(c["site"], site):
                continue
            h = _crc(c["name"])
            cbox_active = CBOXES - (1 if c["fault"] == "cbox_down" else 0)
            scale = cbox_active / CBOXES
            max_bw = cbox_active * CBOX_BW_GBPS
            vrows = [v for v in views if v["cluster"] == c["name"]]
            # 백그라운드(플랫폼) 소량 + 테넌트 뷰 롤업
            read = 6.0 + ((h ^ t * _K) % 60) / 10.0
            write = 2.0 + ((h ^ (t + 3) * _K) % 40) / 10.0
            riops, wiops, clients = 4.0, 1.5, 2
            vperfs = []
            for v in vrows:
                p = self._view_perf(v)
                read += p["read"]; write += p["write"]
                riops += p["riops"]; wiops += p["wiops"]
                clients += v["clients"]
                vperfs.append((v, p))
            # cbox_down → 프런트엔드 대역 저하
            read = min(read * scale, max_bw * 0.7)
            write = min(write * scale, max_bw * 0.5)
            load = (read + write) / max(1.0, max_bw)
            lat = LAT_P99_BASE_MS * (1.0 + 2.5 * load) \
                + ((h ^ t * _K) % 30) / 100.0
            if c["fault"] == "latency_spike":
                lat = 25.0 + ((h ^ t * _K) % 200) / 10.0     # p99 급등
            elif c["fault"] == "nvme_drive_fail":
                lat *= 1.3                                    # rebuild 영향
            cache = 96.0 - 10.0 * load - ((h ^ t * _K) % 30) / 10.0
            if c["fault"] == "latency_spike":
                cache -= 20.0
            out.append({
                "name": c["name"], "scope": "cluster", "site": c["site"],
                "read_gbps": round(read, 1), "write_gbps": round(write, 1),
                "read_iops_k": round(riops, 1),
                "write_iops_k": round(wiops, 1),
                "latency_ms_p99": round(lat, 2),
                "cache_hit_pct": round(max(40.0, cache), 1),
                "active_clients": clients,
                "ckpt_burst": any(p["burst"] for _, p in vperfs),
            })
            for v, p in vperfs:                     # 뷰 단위 롤업
                out.append({
                    "name": v["path"], "scope": "view", "site": c["site"],
                    "tenant_id": v["tenant_id"],
                    "read_gbps": round(p["read"] * scale, 1),
                    "write_gbps": round(p["write"] * scale, 1),
                    "read_iops_k": round(p["riops"], 1),
                    "write_iops_k": round(p["wiops"], 1),
                    "latency_ms_p99": round(
                        lat * (1.1 if p["burst"] else 1.0), 2),
                    "cache_hit_pct": round(max(40.0, cache), 1),
                    "active_clients": v["clients"],
                    "ckpt_burst": p["burst"],
                })
        return out

    # ── 용량/상태 뷰 ──────────────────────────────────────────────────
    def used_pb(self, c: dict, views: List[dict]) -> float:
        if c["fault"] == "capacity_pressure":
            return c["usable_pb"] * CAP_PRESSURE_PCT / 100.0
        # 테넌트 뷰의 논리 used를 DRR로 나눠 물리 사용량(PB) 산출 후,
        # 정상 상태는 usable의 82%를 넘지 않게 클램프(오탐 방지).
        vt = sum(v["used_tb"] for v in views if v["cluster"] == c["name"])
        physical = c["base_used_pb"] + (vt / 1000.0) / max(1.0, c["drr"])
        return min(physical, c["usable_pb"] * 0.82)

    def cluster_view(self, c: dict, views: List[dict]) -> dict:
        used = self.used_pb(c, views)
        state = "DEGRADED" if c["fault"] in ("cbox_down", "latency_spike") \
            else "HEALTHY"
        return {
            "name": c["name"], "site": c["site"],
            "site_name": c["site_name"], "state": state,
            "cboxes": c["cboxes"], "dboxes": c["dboxes"],
            "cboxes_active": c["cboxes"] - (1 if c["fault"] == "cbox_down"
                                            else 0),
            "raw_pb": c["raw_pb"], "usable_pb": c["usable_pb"],
            "used_pb": round(used, 2),
            "used_pct": round(100.0 * used / c["usable_pb"], 1),
            "drr": c["drr"], "version": c["version"],
            "failed_drives": c["failed_drives"],
            "fault": c["fault"],
        }

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
            views = self.view_rows()
            for c in self.clusters.values():
                self._eval_alarms(c, views)

    def _eval_alarms(self, c: dict, views: List[dict]):
        name, k = c["name"], c["fault"]
        if k == "nvme_drive_fail":
            self._fire(("drive", name), "major", name,
                       f"VAST nvme_drive_fail on {name} — DBox NVMe SSD "
                       f"failed ({c['failed_drives']} drive), RAID rebuild "
                       "in progress")
        else:
            self._resolve(("drive", name))
        if k == "cbox_down":
            self._fire(("cbox", name), "critical", name,
                       f"VAST cbox_down on {name} — CBox unresponsive "
                       f"({CBOXES - 1}/{CBOXES} active), front-end BW "
                       "reduced")
        else:
            self._resolve(("cbox", name))
        if k == "latency_spike":
            self._fire(("latency", name), "major", name,
                       f"VAST latency_spike on {name} — read latency p99 "
                       ">25ms (threshold 5ms), cache hit degraded")
        else:
            self._resolve(("latency", name))
        used_pct = 100.0 * self.used_pb(c, views) / c["usable_pb"]
        if used_pct >= 90.0:
            self._fire(("capacity", name), "major", name,
                       f"VAST capacity_pressure on {name} — usable "
                       f"{used_pct:.0f}% used (threshold 90%)")
        else:
            self._resolve(("capacity", name))

    # ── obs 연동 공개 함수 ────────────────────────────────────────────
    def alerts_for_obs(self) -> List[dict]:
        """obs alerts() merge용 — domain 'storage' 알람 전체."""
        self.tick()
        return [dict(a) for a in self.alerts.values()]

    def summary_for_obs(self) -> dict:
        """obs /summary의 storage 블록."""
        self.tick()
        with STORE.lock:
            views = self.view_rows()
            perf = [p for p in self.perf_rows() if p["scope"] == "cluster"]
            return {
                "clusters": len(self.clusters),
                "used_pb": round(sum(self.used_pb(c, views)
                                     for c in self.clusters.values()), 2),
                "usable_pb": round(sum(c["usable_pb"]
                                       for c in self.clusters.values()), 1),
                "read_gbps": round(sum(p["read_gbps"] for p in perf), 1),
                "write_gbps": round(sum(p["write_gbps"] for p in perf), 1),
                "alarms_open": sum(1 for a in self.alerts.values()
                                   if a["state"] == "firing"),
            }

    # ── fault targeting ───────────────────────────────────────────────
    def resolve_target(self, target: Optional[str]) -> dict:
        if not target:
            c = next(iter(self.clusters.values()), None)
        else:
            c = self.clusters.get(target) or next(
                (x for x in self.clusters.values()
                 if self._site_ok(x["site"], target)), None)
        if not c:
            raise HTTPException(404, f"cluster target '{target}' not found")
        return c


ENGINE = VastEngine()


# ── 1) clusters ───────────────────────────────────────────────────────
@router.get("/clusters")
def clusters():
    ENGINE.tick()
    with STORE.lock:
        views = ENGINE.view_rows()
        return {"count": len(ENGINE.clusters),
                "clusters": [ENGINE.cluster_view(c, views)
                             for c in ENGINE.clusters.values()]}


# ── 2) views (테넌트 파생) ────────────────────────────────────────────
@router.get("/views")
def views(tenant: Optional[str] = None):
    ENGINE.tick()
    with STORE.lock:
        rows = ENGINE.view_rows(tenant)
        return {"count": len(rows), "views": rows}


# ── 3) performance — 클러스터·뷰 롤업 (폴링마다 동적) ─────────────────
@router.get("/performance")
def performance(site: Optional[str] = None):
    ENGINE.tick()
    with STORE.lock:
        rows = ENGINE.perf_rows(site)
        return {"count": len(rows), "performance": rows}


# ── 4) alarms / events ────────────────────────────────────────────────
@router.get("/alarms")
def alarms():
    ENGINE.tick()
    with STORE.lock:
        items = sorted(ENGINE.alerts.values(),
                       key=lambda a: (a["state"] != "firing", a["at"]))
        return {"count": len(items),
                "open": sum(1 for a in items if a["state"] == "firing"),
                "alarms": [dict(a) for a in items]}


@router.get("/events")
def events(limit: int = 50):
    ENGINE.tick()
    with STORE.lock:
        return list(ENGINE.events)[:limit]


# ── 5) fault inject / recover ─────────────────────────────────────────
@router.post("/faults/inject")
def inject(body: FaultInject):
    ENGINE.tick()
    with STORE.lock:
        if body.kind not in INJECT_KINDS:
            raise HTTPException(400, f"kind must be one of {list(INJECT_KINDS)}")
        c = ENGINE.resolve_target(body.target)
        c["fault"], c["fault_at"] = body.kind, _iso()
        if body.kind == "nvme_drive_fail":
            c["failed_drives"] += 1
        sev = "critical" if body.kind == "cbox_down" else "major"
        ENGINE._event(sev, "fault", c["name"],
                      f"{body.kind} injected on {c['name']}")
        STORE.event("critical", "NeoCloudEmulator.1.0.VastFaultInjected",
                    [body.kind, c["name"]])
    ENGINE.tick(force=True)
    with STORE.lock:
        views = ENGINE.view_rows()
        return {"injected": body.kind, "target": c["name"],
                "cluster": ENGINE.cluster_view(c, views)}


@router.post("/faults/recover")
def recover(body: FaultRecover = FaultRecover()):
    ENGINE.tick()
    with STORE.lock:
        cleared = []
        for c in ENGINE.clusters.values():
            if c["fault"] and (not body.target or body.target in (
                    c["name"], c["site"])):
                cleared.append(f"{c['name']}:{c['fault']}")
                c["fault"], c["fault_at"] = None, None
                c["failed_drives"] = 0
        for t in cleared:
            ENGINE._event("info", "recovery", t, "fault recovered")
        if cleared:
            STORE.event("info", "NeoCloudEmulator.1.0.VastFaultRecovered",
                        [", ".join(cleared)])
    ENGINE.tick(force=True)
    with STORE.lock:
        views = ENGINE.view_rows()
        return {"recovered": cleared,
                "clusters": [ENGINE.cluster_view(c, views)
                             for c in ENGINE.clusters.values()]}
