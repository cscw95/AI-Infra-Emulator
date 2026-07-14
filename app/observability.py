"""통합 Observability 텔레메트리 생성기 — 30MW Vera Rubin NVL72 설계서 기반.

4개 플레인을 하나의 API(prefix /emulator/v1/obs)로 노출한다:
  - Compute plane  : DCGM 스타일 per-GPU 텔레메트리 (util/temp/power/XID/ECC)
  - Rack/DCIM plane: 랙 전력·수열(inlet/outlet) 뷰
  - SMCI DLC plane : in-row DLC-2 CDU (1차/2차 루프, 펌프, 누수, 알람)
  - SLA plane      : 테넌트별 GPU 가용성·에러버짓 + 정규화 알림 + GPU↔DLC 상관

Assumption (SMCI DLC 토폴로지):
  - SU당 in-row DLC-2 CDU 1대(풀클러스터 기준 총 11대), rated 1,600 kW
  - 랙당 CDM(rack manifold) 1개 = CDU branch, 트레이는 콜드플레이트 DLC
  - cdu_id = "cdu-{su_id}"  (예: cdu-su-4)
  - Rubin GPU 보드파워는 CDU 1.6MW 정합을 위해 1,300W로 축소 가정

동작 모델: 폴링 시각 기반 random-walk. 값은 TICK_SEC 캐시 단위로 1회만
materialize 하고(전 GPU 매 요청 재계산 금지) 요청은 캐시를 필터/페이지한다.
GPU↔DLC 결합: CDU 장애 주입 → flow_factor 저하 → 담당 랙 GPU 온도가 tick마다
점진 상승 → 85°C 초과 thermal throttle / 90°C+ XID → alerts/correlate 반영.
recover 시 tick마다 점진 정상화.

Rack control plane (운영 콘솔 계약):
  POST /racks/{rack_id}/control, POST /racks/control(일괄) —
  power_on/power_off/restart/power_cap/power_uncap/workload/cordon/uncordon.
  제어 상태는 ObsEngine.rack_ctrl에 유지(리셋 시 초기화)되고 tick 캐시
  (DCGM/racks/summary/SLO/alerts)에 즉시 반영된다."""
import itertools
import random
import time
import zlib
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .store import STORE, GPU_PER_TRAY, _iso

router = APIRouter(prefix="/emulator/v1/obs", tags=["observability"])

# ── tunables (Assumption 값들) ─────────────────────────────────────────
TICK_SEC = 2.5                    # 텔레메트리 캐시/랜덤워크 주기
GPU_MEM_GB = 288                  # Rubin HBM 용량
GPU_POWER_LIMIT_W = 1300.0        # Assumption: 1.6MW CDU 정합용 축소 보드파워
GPU_IDLE_POWER_W = 120.0
RACK_OVERHEAD_KW = 24.0           # NVSwitch 트레이 9 + CPU/보드/팬 (Assumption)
CDU_RATED_KW = 1600.0             # SMCI in-row DLC-2 정격 (Assumption)
HEAT_CAPTURE = 0.97               # IT 전력 중 액체로 회수되는 열 비율
THROTTLE_TEMP_C = 85.0
XID_TEMP_C = 90.0

# rack control plane
CTRL_ACTIONS = ("power_on", "power_off", "restart", "power_cap", "power_uncap",
                "workload", "cordon", "uncordon")
PROFILE_UTIL = {"idle": 2.0, "steady": 52.0, "train": 77.0, "burst": 92.0}
RESTART_TICKS = 2                 # restart 후 부트에 걸리는 tick 수


def _default_ctrl() -> dict:
    return {"power": "on", "restart_ticks": 0, "cap_kw": None,
            "profile": None, "cordoned": False, "reason": ""}
SLO_TARGET_PCT = 99.5
ERROR_BUDGET_MIN = 43200 * (100 - SLO_TARGET_PCT) / 100.0   # 30일 = 216분

INJECT_KINDS = ("pump_failure", "flow_loss", "leak", "hx_fouling", "filter_clog")

_KIND_DESC = {
    "pump_failure": "2차측 펌프 고장(standby 전환)",
    "flow_loss": "2차측 유량 저하",
    "leak": "브랜치 누수",
    "hx_fouling": "열교환기(HX) 파울링",
    "filter_clog": "필터 막힘(ΔP 상승)",
}
_KIND_ACTION = {
    "pump_failure": "standby 펌프 duty 전환 확인, 고장 펌프 교체. 해당 SU 신규 배치 중단 권고.",
    "flow_loss": "2차측 밸브/펌프 점검으로 유량 복구. 임계 초과 지속 시 해당 SU GPU 파워캡 적용.",
    "leak": "누수 브랜치 격리 유지, 해당 랙 워크로드 드레인·마이그레이션 후 매니폴드 점검.",
    "hx_fouling": "HX 세정/화학처리 스케줄링, 1차측 수질 점검.",
    "filter_clog": "필터 카트리지 교체, 교체 전까지 유량 모니터링 강화.",
}


def _crc(s: str) -> int:
    return zlib.crc32(s.encode())


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class CduInject(BaseModel):
    kind: str                     # pump_failure|flow_loss|leak|hx_fouling|filter_clog


# ── per-CDU state ─────────────────────────────────────────────────────
@dataclass
class _Cdu:
    cdu_id: str
    su_id: str
    site: str                     # site_id (gasan|ansan)
    site_name: str
    rack_ids: List[str] = field(default_factory=list)
    fault: Optional[str] = None
    fault_at: Optional[str] = None
    flow_factor: float = 1.0      # 1.0 = 정상 유량
    temp_offset: float = 0.0      # 담당 랙 GPU 온도 가산(°C)
    hx_eff: float = 93.0
    filter_dp: float = 35.0
    coolant_level: float = 98.0
    leak_detected: bool = False
    leak_location: Optional[str] = None
    closed_branches: Set[str] = field(default_factory=set)
    rack_extra: Dict[str, float] = field(default_factory=dict)   # 누수 랙 가산온도
    pumps: List[dict] = field(default_factory=list)
    alarms: Dict[str, dict] = field(default_factory=dict)        # open alarms
    d: dict = field(default_factory=dict)                        # per-tick derived

    def init_pumps(self):
        self.pumps = [
            {"pump_id": f"{self.cdu_id}-p1", "state": "duty", "rpm": 3450, "power_w": 5500},
            {"pump_id": f"{self.cdu_id}-p2", "state": "standby", "rpm": 0, "power_w": 40},
        ]


# ── engine ────────────────────────────────────────────────────────────
class ObsEngine:
    def __init__(self):
        self.seed_gen = -1
        self.tick_no = 0
        self.last = 0.0
        self.walks: Dict[str, dict] = {}
        self.cdus: Dict[str, _Cdu] = {}
        self.rack_cdu: Dict[str, str] = {}
        self.rack_hist: Dict[str, deque] = {}
        self.site_names: Dict[str, str] = {}
        self.alerts: Dict[tuple, dict] = {}
        self.rack_ctrl: Dict[str, dict] = {}    # rack_id → 제어 상태
        self._alert_seq = itertools.count(1)
        self._faulted_prev: Set[str] = set()
        self.tenant_acc: Dict[str, dict] = {}
        # per-tick caches
        self._gpus: List[dict] = []
        self._by_uuid: Dict[str, dict] = {}
        self._rack_views: List[dict] = []
        self._rack_agg: Dict[str, dict] = {}
        self._tenants: Dict[str, dict] = {}
        self._counts = {"total": 0, "active": 0, "idle": 0, "throttled": 0, "faulted": 0}
        self._avg_util = 0.0
        self._it_power_mw = 0.0

    # ── topology (re)build — STORE reseed 감지 ────────────────────────
    def _ensure_topology(self) -> bool:
        cur = getattr(STORE, "seed_gen", 0)
        if cur == self.seed_gen:
            return False
        self.seed_gen = cur
        self.walks, self.cdus, self.rack_cdu = {}, {}, {}
        self.rack_hist, self.site_names = {}, {}
        self.alerts, self.tenant_acc = {}, {}
        self.rack_ctrl = {}
        self._faulted_prev = set()
        self.tick_no, self.last = 0, 0.0
        for r in STORE.racks.values():
            cid = f"cdu-{r.su_id}"
            c = self.cdus.get(cid)
            if not c:
                c = _Cdu(cdu_id=cid, su_id=r.su_id, site=r.site_id, site_name=r.site)
                c.init_pumps()
                self.cdus[cid] = c
            c.rack_ids.append(r.rack_id)
            self.rack_cdu[r.rack_id] = cid
            self.walks[r.rack_id] = {"util": random.uniform(66, 84),
                                     "temp": random.uniform(59, 67)}
            self.rack_hist[r.rack_id] = deque(maxlen=30)
            self.site_names[r.site_id] = r.site
        return True

    # ── alert helpers ─────────────────────────────────────────────────
    def _fire(self, key, domain, severity, resource, summary):
        a = self.alerts.get(key)
        if a and a["state"] == "firing":
            a["severity"], a["summary"] = severity, summary     # 최신화
            return
        self.alerts[key] = {"alert_id": f"al-{next(self._alert_seq):04d}",
                            "domain": domain, "severity": severity,
                            "resource": resource, "summary": summary,
                            "at": _iso(), "state": "firing"}

    def _resolve(self, key):
        a = self.alerts.get(key)
        if a and a["state"] == "firing":
            a["state"], a["at"] = "resolved", _iso()

    # ── main tick ─────────────────────────────────────────────────────
    def tick(self, force: bool = False):
        with STORE.lock:
            if self._ensure_topology():
                force = True
            now = time.time()
            if not force and self.tick_no and now - self.last < TICK_SEC:
                return
            dt_min = _clamp((now - self.last) / 60.0, 0.0, 5.0) if self.tick_no else 0.0
            tick = self.tick_no

            # 1) rack random-walk bases
            for w in self.walks.values():
                w["util"] = _clamp(w["util"] + random.uniform(-4, 4), 62.0, 92.0)
                w["temp"] = _clamp(w["temp"] + random.uniform(-1.2, 1.2), 58.0, 70.0)

            # 1b) rack 제어 — restart 부트 진행
            for rid, ctl in self.rack_ctrl.items():
                if ctl["power"] == "restart":
                    ctl["restart_ticks"] -= 1
                    if ctl["restart_ticks"] <= 0:
                        ctl["power"] = "on"
                        rack = STORE.racks.get(rid)
                        if rack:
                            for tid in rack.trays:
                                STORE.set_power(STORE.trays[tid], "On")
                                STORE.trays[tid].lifecycle_state = "Ready"

            # 2) CDU 장애/복구 다이내믹스 (tick마다 점진 변화)
            for c in self.cdus.values():
                k = c.fault
                if k == "flow_loss":
                    c.flow_factor = max(0.30, c.flow_factor - 0.12)
                elif k == "pump_failure":
                    c.flow_factor = max(0.62, c.flow_factor - 0.10)
                elif k == "filter_clog":
                    c.filter_dp = min(130.0, c.filter_dp + 9.0)
                    c.flow_factor = max(0.55, 1.0 - (c.filter_dp - 35.0) / 160.0)
                elif k == "hx_fouling":
                    c.hx_eff = max(62.0, c.hx_eff - 2.5)
                elif k == "leak":
                    c.coolant_level = max(60.0, c.coolant_level - 1.5)
                else:                                   # 정상/복구 진행
                    c.flow_factor = min(1.0, c.flow_factor + 0.15)
                    c.filter_dp = max(35.0, c.filter_dp - 12.0)
                    c.hx_eff = min(93.0, c.hx_eff + 3.0)
                    c.coolant_level = min(98.0, c.coolant_level + 0.5)
                target = (1.0 - c.flow_factor) * 45.0 + (93.0 - c.hx_eff) * 0.55
                if k == "leak":
                    target = max(target, 2.0)
                step = 3.0
                c.temp_offset = (min(target, c.temp_offset + step)
                                 if c.temp_offset < target
                                 else max(target, c.temp_offset - step))
                for rid in list(c.rack_extra):
                    if k == "leak":
                        c.rack_extra[rid] = min(20.0, c.rack_extra[rid] + 3.5)
                    else:
                        c.rack_extra[rid] -= 4.0
                        if c.rack_extra[rid] <= 0:
                            del c.rack_extra[rid]

            # 3) tray → tenant 매핑 (DPU tenant attachment 기준)
            tray_tenant = {}
            for d in STORE.dpus.values():
                for f in d.functions.values():
                    if f.tenant_id:
                        tray_tenant[d.compute_tray_id] = f.tenant_id
                        break

            # 3b) 트레이 작업(재기동/HW 교체) 진행 중 트레이 — GPU idle +
            #     원 테넌트 SLO unavail 집계 (trayops 지연 import — 순환 방지)
            try:
                from . import trayops as _trayops
                _trayops.ENGINE.tick()
                busy_trays = _trayops.ENGINE.busy_trays()
            except Exception:
                busy_trays = {}

            # 4) materialize — per-GPU + rack aggregate (tick당 1회)
            gpus, by_uuid, rack_agg = [], {}, {}
            tray_off_seen = []           # (tray_id, tenant, rack_off)
            tenants: Dict[str, dict] = {}
            counts = {"total": 0, "active": 0, "idle": 0, "throttled": 0,
                      "faulted": 0, "off": 0}
            util_sum, util_n = 0.0, 0
            faulted_now: Set[str] = set()
            for rack in STORE.racks.values():
                c = self.cdus[self.rack_cdu[rack.rack_id]]
                w = self.walks[rack.rack_id]
                offset = c.temp_offset + c.rack_extra.get(rack.rack_id, 0.0)
                power_sum, thr, flt, alloc = 0.0, 0, 0, 0
                rack_tenants = set()
                trays_off_n = 0
                ctl = self.rack_ctrl.get(rack.rack_id) or _default_ctrl()
                rack_off = ctl["power"] in ("off", "restart")
                cordoned = ctl["cordoned"]
                profile = ctl["profile"]
                n_gpu = len(rack.trays) * GPU_PER_TRAY
                cap_w = (ctl["cap_kw"] * 1000.0 / n_gpu) if ctl["cap_kw"] else None
                for tid in rack.trays:
                    tray = STORE.trays[tid]
                    tenant = tray_tenant.get(tid)
                    tray_busy = tid in busy_trays        # 재기동/교체 진행 중
                    if tray_busy and not tenant:
                        tenant = busy_trays.get(tid)     # 원 테넌트로 SLO 집계
                    # 트레이 단위 전원 Off (Redfish 등) — in-band 텔레메트리 유실
                    tray_off = (not tray_busy
                                and STORE.power_state(tray) == "Off")
                    no_ib = rack_off or tray_off
                    if tray_off:
                        trays_off_n += 1
                        tray_off_seen.append((tid, tenant, rack_off))
                    tray_bad = tray.health == "critical" and not no_ib
                    # 부하 대상: 테넌트 할당 또는 워크로드 프로필 지정(데모 부하)
                    loaded = ((tenant is not None or
                               profile in ("steady", "train", "burst"))
                              and not no_ib and not cordoned
                              and not tray_busy and profile != "idle")
                    ubase = PROFILE_UTIL.get(profile, w["util"]) \
                        if profile else w["util"]
                    for gi in range(GPU_PER_TRAY):
                        uuid = f"GPU-{tid}-g{gi}"
                        h = _crc(uuid)
                        r = random.Random((h * 1000003) ^ (tick * 2654435761))
                        if no_ib:
                            util, temp, mem, nvl = 0.0, 26.0 + r.uniform(-1, 1), 0.0, 0.0
                        elif loaded:
                            util = _clamp(ubase + r.uniform(-12, 10), 1.0, 99.0)
                            temp = w["temp"] + offset + r.uniform(-5, 5)
                            mem = GPU_MEM_GB * (0.30 + util / 100 * 0.60)
                            nvl = max(0.0, util * 58 + r.uniform(-150, 150))
                        else:
                            util = r.uniform(0, 3)
                            temp = 34.0 + offset * 0.4 + r.uniform(-2, 2)
                            mem = r.uniform(2, 6)
                            nvl = r.uniform(0, 3)
                        throttled = loaded and temp >= THROTTLE_TEMP_C
                        gpu_xid = loaded and temp >= XID_TEMP_C and h % 25 == 0
                        faulted = tray_bad or gpu_xid
                        xids = [79] if tray_bad else ([63] if gpu_xid else [])
                        if no_ib:
                            power, clock = 0.0, 0.0
                        elif throttled:
                            power = min(GPU_POWER_LIMIT_W,
                                        0.72 * GPU_POWER_LIMIT_W + r.uniform(-40, 40))
                            clock = 1650 + r.uniform(-80, 80)
                        elif loaded:
                            power = (GPU_IDLE_POWER_W + util / 100
                                     * (GPU_POWER_LIMIT_W - GPU_IDLE_POWER_W))
                            clock = 2300 + util * 3
                        else:
                            power = max(60.0, GPU_IDLE_POWER_W + r.uniform(-15, 15))
                            clock = 1350.0
                        capped = bool(cap_w) and not no_ib and power > cap_w
                        if capped:                       # rack power cap 반영
                            scale = cap_w / power
                            power = cap_w
                            util = round(util * max(0.35, scale), 1)
                            clock = min(clock, 1900.0)
                        temp = min(temp, 97.0)
                        # rack off/restart: in-band(DCGM) 텔레메트리 수신 불가
                        # — 판독값 null + state "off" (OOB/DCIM만 가용)
                        state = ("off" if no_ib else
                                 "faulted" if faulted else
                                 "throttled" if throttled else
                                 "active" if loaded else "idle")
                        health = ("unknown" if no_ib else
                                  "critical" if faulted else
                                  "warning" if throttled else "ok")
                        reasons = ([] if no_ib else
                                   (["thermal"] if throttled else [])
                                   + (["power_cap"] if capped else []))
                        rec = {
                            "gpu_uuid": uuid, "idx": gi, "tray_id": tid,
                            "rack_id": rack.rack_id, "su_id": rack.su_id,
                            "site": rack.site_id, "tenant_id": tenant,
                            "util_pct": None if no_ib else round(util, 1),
                            "sm_util_pct": None if no_ib else
                                round(max(0.0, util - r.uniform(0, 6)), 1),
                            "mem_used_gb": None if no_ib else round(mem, 1),
                            "mem_total_gb": GPU_MEM_GB,
                            "temp_c": None if no_ib else round(temp, 1),
                            "mem_temp_c": None if no_ib else
                                round(temp + 8, 1),
                            "power_w": None if no_ib else round(power),
                            "power_limit_w": round(cap_w if cap_w else
                                                   GPU_POWER_LIMIT_W),
                            "sm_clock_mhz": None if no_ib else round(clock),
                            "throttle_reasons": reasons,
                            "ecc_corr": h % 5 + tick // 40,
                            "ecc_uncorr": 1 if gpu_xid else 0,
                            "xid_recent": xids,
                            "nvlink_tx_gbps": None if no_ib else
                                round(nvl, 1),
                            "nvlink_rx_gbps": None if no_ib else
                                round(nvl * 0.93, 1),
                            "pcie_replay": h % 3,
                            "health": health, "state": state,
                            "telemetry_source": "none" if no_ib else "dcgm",
                        }
                        gpus.append(rec)
                        by_uuid[uuid] = rec
                        power_sum += power
                        counts["total"] += 1
                        counts[state] += 1
                        thr += 1 if throttled else 0
                        flt += 1 if faulted else 0
                        if gpu_xid:
                            faulted_now.add(uuid)
                        if tenant:
                            alloc += 1
                            if not no_ib:                # 가동 GPU 기준 평균
                                util_sum += util
                                util_n += 1
                            rack_tenants.add(tenant)
                            t = tenants.setdefault(tenant, {
                                "contracted": 0, "unavail": 0,
                                "throttled": 0, "cooling_unavail": 0})
                            t["contracted"] += 1
                            if throttled:
                                t["throttled"] += 1
                            if throttled or faulted or rack_off or cordoned \
                                    or tray_busy:
                                t["unavail"] += 1
                                if offset > 3.0 and not (rack_off or cordoned):
                                    t["cooling_unavail"] += 1
                gpu_kw = power_sum / 1000.0
                rack_agg[rack.rack_id] = {
                    "rack_id": rack.rack_id, "su_id": rack.su_id,
                    "site": rack.site_id, "gpu_kw": gpu_kw,
                    "it_kw": 0.3 if rack_off else gpu_kw + RACK_OVERHEAD_KW,
                    "thr": thr, "flt": flt, "alloc": alloc,
                    "tenants": rack_tenants, "offset": offset,
                    "ctl": ctl, "off": rack_off,
                    "trays_off": trays_off_n,
                }

            # 5) CDU derived values — 물리 정합 (rack heat ≈ CDU measured_heat)
            for c in self.cdus.values():
                heat = sum(rack_agg[r]["it_kw"] for r in c.rack_ids) * HEAT_CAPTURE
                flow2 = max(250.0, 1900.0 * c.flow_factor
                            * max(0.25, heat / CDU_RATED_KW))
                dt2 = heat * 14.34 / flow2                    # Q=ṁ·cp·ΔT (물, lpm)
                supply2 = 30.0 + (1.0 - c.flow_factor) * 4.0
                flow1 = flow2 * 0.85
                dt1 = heat * 14.34 / flow1 / max(0.5, c.hx_eff / 100.0)
                c.d = {"heat": heat, "flow2": flow2, "dt2": dt2, "supply2": supply2,
                       "flow1": flow1, "dt1": dt1,
                       "util": heat / CDU_RATED_KW * 100.0,
                       "headroom": CDU_RATED_KW - heat}
                self._eval_alarms(c)

            # 6) rack views (DCIM plane)
            rack_views = []
            for rid, agg in rack_agg.items():
                c = self.cdus[self.rack_cdu[rid]]
                closed = rid in c.closed_branches
                n_open = max(1, len(c.rack_ids) - len(c.closed_branches))
                bflow = c.d["flow2"] / n_open
                rdt = 0.0 if closed else min(
                    25.0, agg["it_kw"] * HEAT_CAPTURE * 14.34 / max(1.0, bflow))
                inlet = c.d["supply2"] + 0.7
                ctl = agg["ctl"]
                rack_views.append({
                    "rack_id": rid, "su_id": agg["su_id"], "site": agg["site"],
                    # off 랙은 in-band 유실 — BMC/DCIM OOB 판독만 가용
                    "telemetry_source": "oob" if agg["off"] else "inband",
                    "it_power_kw": round(agg["it_kw"], 1),
                    "gpu_power_kw": round(agg["gpu_kw"], 1),
                    "inlet_c": round(inlet, 1),
                    "outlet_c": round(inlet + rdt, 1),
                    "cdu_id": c.cdu_id,
                    "cooling_headroom_kw": round(
                        CDU_RATED_KW / len(c.rack_ids)
                        - agg["it_kw"] * HEAT_CAPTURE, 1),
                    "throttled_gpus": agg["thr"],
                    "power_state": ("off" if ctl["power"] == "off" else
                                    "mixed" if ctl["power"] == "restart" else
                                    "partial" if agg["trays_off"] else "on"),
                    "trays_off": agg["trays_off"],
                    "trays_total": len(STORE.racks[rid].trays)
                        if rid in STORE.racks else 18,
                    "power_cap_kw": ctl["cap_kw"],
                    "workload_profile": ctl["profile"] or "steady",
                    "cordoned": ctl["cordoned"],
                    "tenants": sorted(agg["tenants"]),
                    "allocated_gpus": agg["alloc"],
                    "health": ("critical" if agg["flt"] or closed else
                               "warning" if agg["thr"] else "ok"),
                })

            # 7) GPU-domain alerts (thermal throttle per rack + XID per GPU)
            for rid, agg in rack_agg.items():
                key = ("gpu-thermal", rid)
                if agg["thr"]:
                    self._fire(key, "gpu", "warning", rid,
                               f"{agg['thr']} GPU thermal-throttled on {rid} "
                               f"(cdu={self.rack_cdu[rid]}, +{agg['offset']:.1f}°C)")
                else:
                    self._resolve(key)
            # 7a-2) 트레이 단위 전원 Off 알림 (Redfish 등 개별 차단)
            off_now = set()
            for tid, tenant, r_off in tray_off_seen:
                if r_off:
                    continue                     # 랙 알림이 커버
                off_now.add(tid)
                key = ("tray-off", tid)
                self._fire(key, "gpu",
                           "major" if tenant else "warning", tid,
                           f"TRAY_POWERED_OFF: {tid} 전원 차단"
                           + (f" — tenant {tenant} 영향 (GPU 4 유실)"
                              if tenant else " (미할당)"))
            for key in [k for k in self.alerts
                        if k[0] == "tray-off" and k[1] not in off_now]:
                self._resolve(key)

            # 7b) rack 제어 알림 — 전원 차단(테넌트 영향 major)·cordon
            for rid, agg in rack_agg.items():
                ctl, koff, kcord = agg["ctl"], ("ctl-off", rid), ("ctl-cordon", rid)
                if agg["off"]:
                    sev = "major" if agg["tenants"] else "warning"
                    self._fire(koff, "gpu", sev, rid,
                               f"RACK_POWERED_OFF: {rid} 전원 차단"
                               + (f" — tenant {'/'.join(sorted(agg['tenants']))} 영향"
                                  if agg["tenants"] else " (미할당)"))
                else:
                    self._resolve(koff)
                if ctl["cordoned"]:
                    self._fire(kcord, "gpu", "warning", rid,
                               f"RACK_CORDONED: {rid} 신규 부하 차단"
                               + (f" — {ctl['reason']}" if ctl["reason"] else ""))
                else:
                    self._resolve(kcord)
            for uuid in faulted_now:
                self._fire(("gpu-xid", uuid), "gpu", "critical", uuid,
                           f"XID 63 (thermal) on {uuid}")
            for uuid in self._faulted_prev - faulted_now:
                self._resolve(("gpu-xid", uuid))
            self._faulted_prev = faulted_now

            # 8) SLO 분 누적 (cooling-caused unavailable / throttling)
            for tenant, st in tenants.items():
                acc = self.tenant_acc.setdefault(tenant, {
                    "cooling_min": 0.0, "throttle_min": 0.0, "unavail_min": 0.0})
                if st["cooling_unavail"]:
                    acc["cooling_min"] += dt_min
                if st["throttled"]:
                    acc["throttle_min"] += dt_min
                if st["unavail"]:
                    acc["unavail_min"] += dt_min

            # 9) history (rack 단위 base 기록 → GPU history 재구성용)
            ts = _iso()
            for rid, agg in rack_agg.items():
                self.rack_hist[rid].append({
                    "ts": ts, "tick": tick,
                    "util": self.walks[rid]["util"],
                    "temp": self.walks[rid]["temp"] + agg["offset"]})

            # 10) commit caches
            self._gpus, self._by_uuid = gpus, by_uuid
            self._rack_views, self._rack_agg = rack_views, rack_agg
            self._tenants = tenants
            self._counts = counts
            self._avg_util = util_sum / util_n if util_n else 0.0
            self._it_power_mw = sum(a["it_kw"] for a in rack_agg.values()) / 1000.0
            self.tick_no += 1
            self.last = now

    # ── CDU alarms ────────────────────────────────────────────────────
    def _eval_alarms(self, c: _Cdu):
        active = {}
        failed = [p["pump_id"] for p in c.pumps if p["state"] == "failed"]
        if failed:
            active["PUMP_FAILURE"] = (
                "critical", f"{failed[0]} failed — standby 펌프 duty 전환")
        if c.flow_factor < 0.85:
            active["FLOW_LOW"] = (
                "major", f"secondary flow {c.d['flow2']:.0f} lpm "
                         f"({c.flow_factor * 100:.0f}% of nominal)")
        if c.leak_detected:
            active["LEAK_DETECTED"] = ("critical", f"leak at {c.leak_location}")
        if c.filter_dp > 90:
            active["FILTER_DP_HIGH"] = ("major", f"filter ΔP {c.filter_dp:.0f} kPa")
        if c.hx_eff < 78:
            active["HX_FOULING"] = ("major", f"HX efficiency {c.hx_eff:.0f}%")
        if c.d["util"] > 96:
            active["CAPACITY_HIGH"] = ("minor", f"utilization {c.d['util']:.0f}%")
        for code in list(c.alarms):
            if code not in active:
                del c.alarms[code]
                self._resolve(("cooling", c.cdu_id, code))
        for code, (sev, det) in active.items():
            if code not in c.alarms:
                c.alarms[code] = {"severity": sev, "code": code,
                                  "detail": det, "at": _iso()}
            self._fire(("cooling", c.cdu_id, code), "cooling", sev,
                       c.cdu_id, f"{code}: {det}")

    # ── views ─────────────────────────────────────────────────────────
    def cdu_view(self, c: _Cdu) -> dict:
        d = c.d
        health = ("critical" if (c.leak_detected or c.fault in
                                 ("leak", "pump_failure")) else
                  "warning" if c.alarms else "ok")
        return {
            "cdu_id": c.cdu_id, "model": "SMCI LCS-DLC2-1600 (in-row)",
            "oem": "Supermicro", "type": "in-row-dlc2",
            "site": c.site, "su_id": c.su_id, "rack_ids": list(c.rack_ids),
            "rated_capacity_kw": CDU_RATED_KW,
            "measured_heat_kw": round(d["heat"], 1),
            "utilization_pct": round(d["util"], 1),
            "headroom_kw": round(d["headroom"], 1),
            "primary": {"supply_c": 20.0,
                        "return_c": round(20.0 + d["dt1"], 1),
                        "flow_lpm": round(d["flow1"], 1),
                        "pressure_kpa": round(300.0, 1)},
            "secondary": {"supply_c": round(d["supply2"], 1),
                          "return_c": round(d["supply2"] + d["dt2"], 1),
                          "delta_t": round(d["dt2"], 1),
                          "flow_lpm": round(d["flow2"], 1),
                          "pressure_kpa": round(250.0 * c.flow_factor, 1)},
            "pumps": [dict(p) for p in c.pumps],
            "hx_efficiency_pct": round(c.hx_eff, 1),
            "filter_dp_kpa": round(c.filter_dp, 1),
            "coolant": {"level_pct": round(c.coolant_level, 1),
                        "conductivity_us_cm": 2.5, "ph": 9.8,
                        "concentration_pct": 25.0},
            "dew_point_margin_c": round(d["supply2"] - 21.5, 1),
            "leak": {"detected": c.leak_detected, "location": c.leak_location},
            "alarms": list(c.alarms.values()),
            "health": health,
        }

    def cdu_detail(self, c: _Cdu) -> dict:
        v = self.cdu_view(c)
        n_open = max(1, len(c.rack_ids) - len(c.closed_branches))
        share = c.d["flow2"] / n_open
        branches, sensors = [], []
        for i, rid in enumerate(c.rack_ids):
            closed = rid in c.closed_branches
            agg = self._rack_agg.get(rid, {"it_kw": RACK_OVERHEAD_KW})
            rdt = 0.0 if closed else min(
                25.0, agg["it_kw"] * HEAT_CAPTURE * 14.34 / max(1.0, share))
            leak_here = c.leak_detected and c.leak_location and rid in c.leak_location
            branches.append({
                "branch_id": f"{c.cdu_id}-br-{i:02d}", "rack_id": rid,
                "flow_lpm": 0.0 if closed else round(share, 1),
                "supply_c": round(c.d["supply2"] + 0.7, 1),
                "return_c": round(c.d["supply2"] + 0.7 + rdt, 1),
                "valve": "closed" if closed else "open",
                "server_loops": 18,
                "imbalance_pct": round((_crc(rid) % 70) / 10.0 - 3.5, 1),
            })
            sensors.append({"sensor_id": f"{c.cdu_id}-ls-{i:02d}",
                            "location": f"{rid} manifold",
                            "state": "wet" if leak_here else "dry"})
        sensors.append({"sensor_id": f"{c.cdu_id}-ls-tray", "location": "cdu drip tray",
                        "state": "wet" if c.leak_detected else "dry"})
        v["branches"] = branches
        v["leak_sensors"] = sensors
        return v

    def gpu_history(self, rec: dict) -> dict:
        uuid, allocated = rec["gpu_uuid"], bool(rec["tenant_id"])
        h = _crc(uuid)
        out = {"ts": [], "util": [], "temp": [], "power": []}
        for e in self.rack_hist.get(rec["rack_id"], []):
            r = random.Random((h * 1000003) ^ (e["tick"] * 2654435761))
            if allocated:
                util = _clamp(e["util"] + r.uniform(-12, 10), 40.0, 99.0)
                temp = min(97.0, e["temp"] + r.uniform(-5, 5))
                if temp >= THROTTLE_TEMP_C:
                    power = min(GPU_POWER_LIMIT_W,
                                0.72 * GPU_POWER_LIMIT_W + r.uniform(-40, 40))
                else:
                    power = (GPU_IDLE_POWER_W + util / 100
                             * (GPU_POWER_LIMIT_W - GPU_IDLE_POWER_W))
            else:
                util = r.uniform(0, 3)
                temp = 34.0 + r.uniform(-2, 2)
                power = max(60.0, GPU_IDLE_POWER_W + r.uniform(-15, 15))
            out["ts"].append(e["ts"])
            out["util"].append(round(util, 1))
            out["temp"].append(round(temp, 1))
            out["power"].append(round(power))
        return out

    def alert_list(self) -> List[dict]:
        items = [dict(a) for a in self.alerts.values()]
        for i, f in enumerate(STORE.faults):
            items.append({"alert_id": f"prov-{i:04d}", "domain": "provisioning",
                          "severity": "critical", "resource": f["tray_id"],
                          "summary": f"{f['kind']}: {f['detail']}", "at": f["at"],
                          "state": "resolved" if f.get("resolved") else "firing"})
        items.extend(_fabric_alerts())
        items.extend(_storage_alerts())
        items.extend(_trayops_alerts())
        items.sort(key=lambda a: (a["state"] != "firing", a["at"]), reverse=False)
        items.sort(key=lambda a: a["at"], reverse=True)
        items.sort(key=lambda a: a["state"] != "firing")
        return items

    def open_alert_count(self) -> int:
        n = sum(1 for a in self.alerts.values() if a["state"] == "firing")
        n += sum(1 for f in STORE.faults if not f.get("resolved"))
        return n


def _fabric_alerts() -> List[dict]:
    """UFM(IB)·NetQ(Ethernet) 상태를 fabric 도메인 알림으로 merge."""
    out: List[dict] = []
    try:
        from . import ufm as _ufm, netq as _netq
        h = _ufm.fabric_health(site=None)
        bad = (h.get("links_degraded") or 0) + (h.get("links_down") or 0)
        if bad:
            def _pname(p):
                if isinstance(p, dict):
                    return p.get("port") or p.get("name") or p.get("link_id") \
                        or p.get("system") or "port"
                return str(p)
            samp = ", ".join(_pname(p)
                             for p in (h.get("unhealthy_ports") or [])[:2])
            out.append({
                "alert_id": "fab-ib", "domain": "fabric",
                "severity": "major" if h.get("links_down") else "warning",
                "resource": "ufm",
                "summary": f"IB 링크 이상 — degraded {h.get('links_degraded', 0)}"
                           f" · down {h.get('links_down', 0)}"
                           + (f" ({samp})" if samp else ""),
                "at": _iso(), "state": "firing"})
        v = _netq.validation()
        checks = v.get("checks", v) if isinstance(v, dict) else v
        fails = [c for c in checks if c.get("result") == "fail"]
        warns = [c for c in checks if c.get("result") == "warn"]
        if fails or warns:
            first = (fails or warns)[0]
            out.append({
                "alert_id": "fab-netq", "domain": "fabric",
                "severity": "major" if fails else "warning",
                "resource": "netq",
                "summary": "NetQ validation "
                           + (f"fail {len(fails)}" if fails else "")
                           + (" · " if fails and warns else "")
                           + (f"warn {len(warns)}" if warns else "")
                           + f" — {first.get('check')}: "
                           + str(first.get('detail', ''))[:60],
                "at": _iso(), "state": "firing"})
    except Exception:
        pass                                    # fabric 에뮬레이터 미가용 시 무시
    return out


def _trayops_alerts() -> List[dict]:
    """트레이 재기동/HW 교체 작업 알림(domain 'trayops') merge —
    /emulator/v1/faults 피드로도 전파된다 (provisioning.faults가 이 목록 사용)."""
    try:
        from . import trayops as _trayops
        return _trayops.ENGINE.alerts_for_obs()
    except Exception:
        return []


def _storage_alerts() -> List[dict]:
    """VAST(AI Storage)·Converged rail 상태를 storage 도메인 알림으로 merge.

    ufm/netq처럼 엔진 공개 함수(alerts_for_obs)를 직접 호출한다 —
    엔드포인트의 래핑 응답({count, alarms:[...]})을 언랩할 필요가 없다."""
    out: List[dict] = []
    try:
        from . import vast as _vast, converged as _converged
        out.extend(_vast.ENGINE.alerts_for_obs())
        out.extend(_converged.ENGINE.alerts_for_obs())
    except Exception:
        pass                                    # storage 에뮬레이터 미가용 시 무시
    return out


ENGINE = ObsEngine()


def _cdu_or_404(cdu_id: str) -> _Cdu:
    c = ENGINE.cdus.get(cdu_id)
    if not c:
        raise HTTPException(404, f"cdu {cdu_id} not found")
    return c


def _site_match(val: str, q: str, names: Dict[str, str]) -> bool:
    return q in (val, names.get(val, ""))


# ── 1) summary ────────────────────────────────────────────────────────
@router.get("/summary")
def summary():
    ENGINE.tick()
    with STORE.lock:
        counts = dict(ENGINE._counts)
        contracted = sum(t["contracted"] for t in ENGINE._tenants.values())
        unavail = sum(t["unavail"] for t in ENGINE._tenants.values())
        avail_pct = 100.0 if not contracted else (
            100.0 * (contracted - unavail) / contracted)
        cdus = list(ENGINE.cdus.values())
        try:
            from . import vast as _vast
            storage = _vast.ENGINE.summary_for_obs()
        except Exception:
            storage = None                     # storage 에뮬레이터 미가용 시
        return {
            "gpus": counts,
            "avg_util_pct": round(ENGINE._avg_util, 1),
            "it_power_mw": round(ENGINE._it_power_mw, 3),
            "cooling": {
                "cdus": len(cdus),
                "alarms_open": sum(len(c.alarms) for c in cdus),
                "avg_utilization_pct": round(
                    sum(c.d["util"] for c in cdus) / len(cdus), 1) if cdus else 0.0,
                "headroom_kw": round(sum(c.d["headroom"] for c in cdus), 1),
            },
            "racks": len(STORE.racks),
            "racks_off": sum(1 for a in ENGINE._rack_agg.values() if a["off"]),
            "trays_off": sum(a.get("trays_off", 0)
                             for a in ENGINE._rack_agg.values()),
            "racks_cordoned": sum(1 for a in ENGINE._rack_agg.values()
                                  if a["ctl"]["cordoned"]),
            "racks_capped": sum(1 for a in ENGINE._rack_agg.values()
                                if a["ctl"]["cap_kw"]),
            "tenants": len(ENGINE._tenants),
            "alerts_open": ENGINE.open_alert_count(),
            "slo": {"gpu_availability_pct": round(avail_pct, 3)},
            "storage": storage,
        }


# ── 2-3) DCGM plane ───────────────────────────────────────────────────
def _gpu_attn(g: dict) -> bool:
    """운영 콘솔 '예외만' 판정과 동일한 서버측 기준 (off = 판독 없음 → 제외)."""
    if g["state"] == "off":
        return False
    return (g["state"] in ("faulted", "throttled") or g["temp_c"] >= 78.0
            or g["ecc_uncorr"] > 0 or g["pcie_replay"] >= 3)


_GPU_SORT = {                          # off GPU(null 판독)는 최하위 정렬
    "temp": lambda g: -(g["temp_c"] if g["temp_c"] is not None else -273.0),
    "util": lambda g: -(g["util_pct"] if g["util_pct"] is not None else -1.0),
    "power": lambda g: -(g["power_w"] if g["power_w"] is not None else -1),
    "ecc": lambda g: -(g["ecc_uncorr"] * 1000 + g["ecc_corr"]),
}


@router.get("/dcgm/gpus")
def dcgm_gpus(site: Optional[str] = None, su: Optional[str] = None,
              rack: Optional[str] = None, tenant: Optional[str] = None,
              state: Optional[str] = None, attn: bool = False,
              sort: Optional[str] = None, limit: int = 100, offset: int = 0):
    ENGINE.tick()
    with STORE.lock:
        gs = ENGINE._gpus
        if site:
            gs = [g for g in gs if _site_match(g["site"], site, ENGINE.site_names)]
        if su:
            gs = [g for g in gs if g["su_id"] == su]
        if rack:
            gs = [g for g in gs if g["rack_id"] == rack]
        if tenant:
            gs = [g for g in gs if g["tenant_id"] == tenant]
        if state:
            gs = [g for g in gs if g["state"] == state]
        if attn:                               # 예외만 — 전 플릿 기준 필터
            gs = [g for g in gs if _gpu_attn(g)]
        if sort in _GPU_SORT:                  # 전 플릿 기준 정렬 후 페이지
            gs = sorted(gs, key=_GPU_SORT[sort])
        return {"total": len(gs), "offset": offset, "limit": limit,
                "gpus": gs[offset:offset + limit]}


@router.get("/dcgm/su-summary")
def dcgm_su_summary(site: Optional[str] = None):
    """SU 단위 집계 + 플릿 히스토그램 — 콘솔 히트맵/분포 라이브 바인딩용."""
    ENGINE.tick()
    with STORE.lock:
        sus: Dict[str, dict] = {}
        uh = [0] * 10
        th = [0] * 10
        for g in ENGINE._gpus:
            if site and not _site_match(g["site"], site, ENGINE.site_names):
                continue
            s = sus.setdefault(g["su_id"], {
                "su_id": g["su_id"], "site": g["site"], "gpus": 0,
                "active": 0, "throttled": 0, "faulted": 0, "off": 0,
                "_util": 0.0, "_n": 0, "max_temp_c": 0.0, "ecc_uncorr": 0})
            s["gpus"] += 1
            if g["state"] == "off":       # in-band 판독 없음 — 집계 제외
                s["off"] += 1
                continue
            if g["state"] in ("active", "throttled", "faulted"):
                s["active"] += 1
            if g["state"] == "throttled":
                s["throttled"] += 1
            if g["state"] == "faulted":
                s["faulted"] += 1
            s["_util"] += g["util_pct"]
            s["_n"] += 1
            s["max_temp_c"] = max(s["max_temp_c"], g["temp_c"])
            s["ecc_uncorr"] += g["ecc_uncorr"]
            uh[min(9, int(g["util_pct"] // 10))] += 1
            th[min(9, max(0, int((g["temp_c"] - 20) // 8)))] += 1
        out = []
        for s in sorted(sus.values(), key=lambda x: int(x["su_id"].split("-")[1])):
            s["avg_util_pct"] = round(s["_util"] / max(1, s["_n"]), 1)
            del s["_util"], s["_n"]
            out.append(s)
        return {"sus": out,
                "hist": {"util_buckets": uh, "temp_buckets": th,
                         "util_edges": "0-100 step10",
                         "temp_edges": "20-100 step8"}}


@router.get("/dcgm/gpus/{gpu_uuid}")
def dcgm_gpu(gpu_uuid: str):
    ENGINE.tick()
    with STORE.lock:
        rec = ENGINE._by_uuid.get(gpu_uuid)
        if not rec:
            raise HTTPException(404, f"gpu {gpu_uuid} not found")
        out = dict(rec)
        out["history"] = ENGINE.gpu_history(rec)
        return out


# ── 4) racks (DCIM plane) ─────────────────────────────────────────────
@router.get("/racks")
def racks(site: Optional[str] = None, su: Optional[str] = None):
    ENGINE.tick()
    with STORE.lock:
        rs = ENGINE._rack_views
        if site:
            rs = [r for r in rs if _site_match(r["site"], site, ENGINE.site_names)]
        if su:
            rs = [r for r in rs if r["su_id"] == su]
        return rs


# ── 4b) rack control plane ────────────────────────────────────────────
class RackControlBody(BaseModel):
    action: str
    params: dict = {}


class BulkScope(BaseModel):
    all: bool = False
    site: Optional[str] = None
    su: Optional[str] = None
    rack_ids: Optional[List[str]] = None


class BulkControlBody(BaseModel):
    scope: BulkScope
    action: str
    params: dict = {}


def _ctrl_state(ctl: dict) -> dict:
    return {"power_state": ("off" if ctl["power"] == "off" else
                            "mixed" if ctl["power"] == "restart" else "on"),
            "power_cap_kw": ctl["cap_kw"],
            "workload_profile": ctl["profile"] or "steady",
            "cordoned": ctl["cordoned"]}


def _apply_rack_control(rack_id: str, action: str, params: dict) -> dict:
    """단일 랙에 제어 적용 (STORE.lock 보유 상태에서 호출)."""
    ENGINE._ensure_topology()      # reseed 직후라면 먼저 재구축(제어 유실 방지)
    rack = STORE.racks.get(rack_id)
    if not rack:
        raise HTTPException(404, f"rack {rack_id} not found")
    if action not in CTRL_ACTIONS:
        raise HTTPException(422, f"unknown action '{action}' "
                                 f"(supported: {', '.join(CTRL_ACTIONS)})")
    ctl = ENGINE.rack_ctrl.setdefault(rack_id, _default_ctrl())
    if action == "power_off":
        ctl["power"] = "off"
        for tid in rack.trays:
            STORE.set_power(STORE.trays[tid], "ForceOff")
    elif action == "power_on":
        ctl["power"] = "on"
        for tid in rack.trays:
            STORE.set_power(STORE.trays[tid], "On")
    elif action == "restart":
        ctl["power"], ctl["restart_ticks"] = "restart", RESTART_TICKS
        for tid in rack.trays:
            STORE.set_power(STORE.trays[tid], "ForceRestart")
            STORE.trays[tid].lifecycle_state = "Provisioning"
    elif action == "power_cap":
        cap = params.get("cap_kw")
        if cap is None and params.get("cap_pct") is not None:
            n_gpu = len(rack.trays) * GPU_PER_TRAY
            cap = (n_gpu * GPU_POWER_LIMIT_W / 1000.0 + RACK_OVERHEAD_KW) \
                * float(params["cap_pct"]) / 100.0
        if not cap or float(cap) <= 0:
            raise HTTPException(422, "power_cap requires cap_kw or cap_pct > 0")
        ctl["cap_kw"] = round(float(cap), 1)
    elif action == "power_uncap":
        ctl["cap_kw"] = None
    elif action == "workload":
        profile = params.get("profile")
        if profile not in PROFILE_UTIL:
            raise HTTPException(422, f"workload requires profile in "
                                     f"{sorted(PROFILE_UTIL)}")
        ctl["profile"] = profile
    elif action == "cordon":
        ctl["cordoned"], ctl["reason"] = True, params.get("reason", "")
    elif action == "uncordon":
        ctl["cordoned"], ctl["reason"] = False, ""
    STORE.event("info", "NeoCloudEmulator.1.0.RackControl",
                [rack_id, action, str(params or {})])
    return _ctrl_state(ctl)


@router.post("/racks/{rack_id}/control")
def rack_control(rack_id: str, body: RackControlBody):
    with STORE.lock:
        state = _apply_rack_control(rack_id, body.action, body.params)
    ENGINE.tick(force=True)                      # 즉시 텔레메트리 수렴
    return {"rack_id": rack_id, "action": body.action,
            "applied": True, "state": state}


@router.post("/racks/control")
def racks_control(body: BulkControlBody):
    """전체/사이트/SU/랙 목록 범위 일괄 제어."""
    sc = body.scope
    with STORE.lock:
        if sc.rack_ids:
            targets = list(sc.rack_ids)
        else:
            targets = [r.rack_id for r in STORE.racks.values()
                       if (sc.all or sc.site or sc.su)
                       and (not sc.site or _site_match(
                           r.site_id, sc.site, ENGINE.site_names))
                       and (not sc.su or r.su_id == sc.su)]
        if not targets:
            raise HTTPException(422, "empty scope — set all/site/su/rack_ids")
        applied, failed = 0, []
        for rid in targets:
            try:
                _apply_rack_control(rid, body.action, body.params)
                applied += 1
            except HTTPException as e:
                failed.append({"rack_id": rid, "error": str(e.detail)})
    ENGINE.tick(force=True)
    return {"matched": len(targets), "applied": applied, "failed": failed,
            "summary": f"{body.action} → {applied}/{len(targets)} racks"}


# ── 5-7) SMCI DLC plane ───────────────────────────────────────────────
@router.get("/dlc/cdus")
def dlc_cdus(site: Optional[str] = None):
    ENGINE.tick()
    with STORE.lock:
        cs = list(ENGINE.cdus.values())
        if site:
            cs = [c for c in cs if _site_match(c.site, site, ENGINE.site_names)]
        return [ENGINE.cdu_view(c) for c in cs]


@router.get("/dlc/cdus/{cdu_id}")
def dlc_cdu(cdu_id: str):
    ENGINE.tick()
    with STORE.lock:
        return ENGINE.cdu_detail(_cdu_or_404(cdu_id))


@router.post("/dlc/cdus/{cdu_id}/inject")
def dlc_inject(cdu_id: str, body: CduInject):
    ENGINE.tick()
    with STORE.lock:
        if body.kind not in INJECT_KINDS:
            raise HTTPException(400, f"kind must be one of {list(INJECT_KINDS)}")
        c = _cdu_or_404(cdu_id)
        c.fault, c.fault_at = body.kind, _iso()
        if body.kind == "pump_failure":
            c.pumps[0].update(state="failed", rpm=0, power_w=0)
            c.pumps[1].update(state="duty", rpm=3600, power_w=5900)
        if body.kind == "leak":
            rid = c.rack_ids[0]
            c.leak_detected = True
            c.leak_location = f"{rid} branch manifold"
            c.closed_branches.add(rid)
            c.rack_extra[rid] = max(c.rack_extra.get(rid, 0.0), 4.0)
        STORE.event("critical", "NeoCloudEmulator.1.0.CduFaultInjected",
                    [cdu_id, body.kind])
    ENGINE.tick(force=True)
    with STORE.lock:
        return {"cdu_id": cdu_id, "fault": body.kind,
                "detail": _KIND_DESC[body.kind],
                "cdu": ENGINE.cdu_view(c)}


@router.post("/dlc/cdus/{cdu_id}/recover")
def dlc_recover(cdu_id: str):
    ENGINE.tick()
    with STORE.lock:
        c = _cdu_or_404(cdu_id)
        prev = c.fault
        c.fault, c.fault_at = None, None
        c.leak_detected, c.leak_location = False, None
        c.closed_branches.clear()
        c.pumps[0].update(state="duty", rpm=3450, power_w=5500)
        c.pumps[1].update(state="standby", rpm=0, power_w=40)
        STORE.event("info", "NeoCloudEmulator.1.0.CduRecovered",
                    [cdu_id, prev or "none"])
    ENGINE.tick(force=True)
    with STORE.lock:
        return {"cdu_id": cdu_id, "recovered_from": prev,
                "cdu": ENGINE.cdu_view(c)}


# ── 8) SLA plane ──────────────────────────────────────────────────────
@router.get("/slo")
def slo():
    ENGINE.tick()
    with STORE.lock:
        out = []
        for tenant in sorted(ENGINE._tenants):
            st = ENGINE._tenants[tenant]
            acc = ENGINE.tenant_acc.get(tenant, {
                "cooling_min": 0.0, "throttle_min": 0.0, "unavail_min": 0.0})
            contracted = st["contracted"]
            available = contracted - st["unavail"]
            pct = 100.0 * available / contracted if contracted else 100.0
            out.append({
                "tenant_id": tenant,
                "contracted_gpus": contracted,
                "available_gpus": available,
                "gpu_availability_pct": round(pct, 3),
                "slo_target_pct": SLO_TARGET_PCT,
                "error_budget_remaining_pct": round(max(0.0, 100.0 * (
                    1 - acc["unavail_min"] / ERROR_BUDGET_MIN)), 2),
                "burn_rate": round((100.0 - pct) / (100.0 - SLO_TARGET_PCT), 2),
                "cooling_caused_unavail_min": round(acc["cooling_min"], 2),
                "throttling_min": round(acc["throttle_min"], 2),
            })
        return {"tenants": out}


# ── 9) normalized alerts ──────────────────────────────────────────────
@router.get("/alerts")
def alerts(limit: int = 50):
    ENGINE.tick()
    with STORE.lock:
        return ENGINE.alert_list()[:limit]


# ── 10) GPU↔DLC correlation (RCA view) ───────────────────────────────
@router.get("/correlate/cooling")
def correlate_cooling():
    ENGINE.tick()
    with STORE.lock:
        out = []
        for c in ENGINE.cdus.values():
            if not c.fault and c.temp_offset <= 3.0:
                continue
            aggs = [ENGINE._rack_agg[r] for r in c.rack_ids
                    if r in ENGINE._rack_agg]
            thr = sum(a["thr"] for a in aggs)
            flt = sum(a["flt"] for a in aggs)
            alloc = sum(a["alloc"] for a in aggs)
            tenants = sorted(set().union(*(a["tenants"] for a in aggs))
                             if aggs else set())
            pct = 100.0 * thr / alloc if alloc else 0.0
            kind = c.fault or "residual_thermal"
            desc = _KIND_DESC.get(kind, "냉각 이벤트 후 정상화 진행 중")
            out.append({
                "cdu_id": c.cdu_id,
                "finding": (f"CDU {c.cdu_id} {desc} → {c.su_id} 랙 GPU 평균온도 "
                            f"+{c.temp_offset:.1f}°C, thermal throttle "
                            f"{pct:.0f}% ({thr}/{alloc} GPU, XID {flt}건)"),
                "confidence": 0.94 if c.fault else 0.70,
                "affected_racks": list(c.rack_ids),
                "affected_gpus": thr + flt,
                "tenant_impact": tenants,
                "recommended_action": _KIND_ACTION.get(
                    kind, "정상화 진행 중 — 온도/throttle 지표 모니터링 유지."),
            })
        return out
