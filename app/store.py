"""In-memory state store + Vera Rubin NVL72 digital twin.

Thread-safe (RLock). Mirrors the design's DB schema as dataclasses:
dpu / dpu_function / dpu_representor / tenant_network / dpu_tenant_attachment /
dpu_security_policy / dpu_flow_pipe / dpu_flow_entry / dpu_qos_policy /
dpu_ipsec_sa / dpu_policy_transaction  (+ Redfish BMC + fabric state)."""
import itertools
import os
import time
from collections import deque
from dataclasses import dataclass, field
from threading import RLock
from typing import Dict, List, Optional, Any

# ── VR NVL72 rack constants (NCP RD) ──────────────────────────────────
COMPUTE_TRAYS = 18          # per rack — NVL72: 18 compute trays × 4 Rubin GPU = 72 GPU
GPU_PER_TRAY = 4
NVLINK_SWITCH_TRAYS = 9
POWER_SHELVES = 6
CDU_COUNT = 1

# ── Cluster topology — mirrors the NeoCloud OS (NOCP) Phase-1 fleet ────
#   140 racks · 2,520 trays · 10,080 GPU · 11 SUs · 2 sites
#   host_id = "nh-{tray_id}", tray_id = "su-{S}-rack-{RR}-tray-{TT}"
CLUSTER = [
    {"id": "gasan", "name": "STT 가산", "region": "Seoul",
     "sus": [("su-1", 16), ("su-2", 8), ("su-3", 12)]},                 # 36 racks
    {"id": "ansan", "name": "IGIS 안산", "region": "Ansan",
     "sus": [("su-4", 16), ("su-5", 16), ("su-6", 16), ("su-7", 6),
             ("su-8", 16), ("su-9", 16), ("su-10", 16), ("su-11", 2)]}, # 104 racks
]
# Optional dev cap: NICO_RACKS_LIMIT=N seeds only the first N racks (faster tests).
# Read at seed() time so test fixtures can cap the cluster before a reset.
def _rack_limit() -> int:
    return int(os.environ.get("NICO_RACKS_LIMIT", "0") or 0)
RACK_ID = "su-1-rack-00"    # first rack (back-compat handle for scenarios/tests)


def _now() -> float:
    return time.monotonic()


def _iso(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── DPU domain records ────────────────────────────────────────────────
@dataclass
class Function:
    function_id: str
    dpu_id: str
    function_type: str          # PF | VF | SF
    pf_number: Optional[int] = None
    vf_number: Optional[int] = None
    sf_number: Optional[int] = None
    controller_number: Optional[int] = None
    pci_address: Optional[str] = None
    mac_address: Optional[str] = None
    tenant_id: Optional[str] = None
    trusted: bool = False
    spoof_check: bool = True
    link_state: str = "auto"
    operational_state: str = "INACTIVE"   # design §6.3 state machine
    rx_packets: int = 0
    tx_packets: int = 0
    tx_forwarded_packets: int = 0


@dataclass
class Representor:
    representor_id: str
    dpu_id: str
    function_id: str
    netdev_name: str
    switch_id: str
    port_index: int
    admin_state: str = "up"
    operational_state: str = "up"


@dataclass
class Dpu:
    dpu_id: str
    compute_tray_id: str
    bmc_ip: str
    operating_mode: str = "DPU"          # NIC | DPU | Restricted | ZeroTrust
    eswitch_mode: str = "switchdev"
    arm_os_state: str = "ready"
    bmc_state: str = "ok"
    secure_boot_enabled: bool = True
    attestation_state: str = "VALID"
    current_policy_generation: int = 0
    last_known_good_generation: int = 0
    health: str = "ok"
    failure_policy: Dict[str, str] = field(default_factory=lambda: {
        "datapath_controller_failure": "failClosed",
        "arm_os_failure": "failClosed",
        "control_plane_disconnect": "preserveLastKnownGood",
    })
    functions: Dict[str, Function] = field(default_factory=dict)
    representors: Dict[str, Representor] = field(default_factory=dict)
    telemetry: Dict[str, int] = field(default_factory=dict)  # counter name -> value

    def counter(self, name: str, inc: int = 0) -> int:
        self.telemetry[name] = self.telemetry.get(name, 0) + inc
        return self.telemetry[name]


@dataclass
class ComputeTray:
    tray_id: str
    bmc_ip: str
    rack_id: str = ""
    site: str = ""
    gpus: int = GPU_PER_TRAY
    power_state: str = "On"              # Off | PoweringOn | On
    power_target: str = "On"
    power_changed_at: float = field(default_factory=_now)
    boot_source: str = "None"
    boot_enabled: str = "Disabled"
    boot_stage: str = "HostReady"        # boot progression state
    lifecycle_state: str = "Ready"       # Discovered|Provisioning|Ready|Degraded
    health: str = "ok"
    dpu_id: str = ""
    serial: str = ""                     # HW 시리얼 — HW 교체(trayops) 시 갱신
    mac_address: str = ""                # host NIC MAC — HW 교체 시 갱신


@dataclass
class Rack:
    rack_id: str                          # su-{S}-rack-{RR}
    su_id: str
    site: str                             # site display name
    site_id: str
    model: str = "Vera Rubin NVL72"
    trays: List[str] = field(default_factory=list)   # tray_ids
    dpus: List[str] = field(default_factory=list)     # dpu_ids
    nvlink_switch_trays: int = NVLINK_SWITCH_TRAYS
    power_shelves: int = POWER_SHELVES
    cdus: int = CDU_COUNT


# ── Store ─────────────────────────────────────────────────────────────
class Store:
    def __init__(self):
        self.lock = RLock()
        self.started = _iso()
        self.racks: Dict[str, Rack] = {}
        self.trays: Dict[str, ComputeTray] = {}
        self.dpus: Dict[str, Dpu] = {}
        self.tenant_networks: Dict[str, dict] = {}
        self.attachments: Dict[str, dict] = {}
        self.security_policies: Dict[str, dict] = {}
        self.flow_pipes: Dict[str, dict] = {}
        self.flow_entries: Dict[str, dict] = {}
        self.qos_policies: Dict[str, dict] = {}
        self.ipsec_sas: Dict[str, dict] = {}
        self.policy_txns: Dict[str, dict] = {}
        self.dhcp_leases: Dict[str, dict] = {}
        self.events = deque(maxlen=1000)
        # 프로비저닝 계열 장애 이력 — 재프로비저닝(=장애) 에피소드 등
        # [{tray_id, kind, detail, at, resolved, resolved_at}]
        self.faults = deque(maxlen=200)
        self._ids = itertools.count(1)
        self.seed()

    def nid(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids):04d}"

    def event(self, severity: str, message_id: str, args=None, **extra):
        e = {"at": _iso(), "severity": severity, "message_id": message_id,
             "args": args or [], **extra}
        self.events.append(e)
        return e

    # ── VR NVL72 cluster twin ─────────────────────────────────────────
    def seed(self):
        with self.lock:
            # reseed generation — derived caches (e.g. observability engine)
            # rebuild their topology when this changes
            self.seed_gen = getattr(self, "seed_gen", 0) + 1
            self.racks.clear(); self.trays.clear(); self.dpus.clear()
            lim = _rack_limit()
            n_racks = 0
            for site in CLUSTER:
                oct2 = 60
                for su_id, rack_n in site["sus"]:
                    for r in range(rack_n):
                        if lim and n_racks >= lim:
                            break
                        rack_id = f"{su_id}-rack-{r:02d}"
                        rack = Rack(rack_id=rack_id, su_id=su_id,
                                    site=site["name"], site_id=site["id"])
                        oct3 = n_racks % 250
                        for t in range(COMPUTE_TRAYS):
                            tid = f"{rack_id}-tray-{t:02d}"
                            did = f"{tid}-dpu-0"
                            self.trays[tid] = ComputeTray(
                                tray_id=tid, rack_id=rack_id, site=site["name"],
                                bmc_ip=f"10.{oct2}.{oct3}.{10 + t}", dpu_id=did,
                                serial=f"SN-{tid}",
                                mac_address="52:54:00:%02x:%02x:%02x" % (
                                    oct2 & 0xff, oct3 & 0xff, (10 + t) & 0xff))
                            dpu = Dpu(dpu_id=did, compute_tray_id=tid,
                                      bmc_ip=f"10.{oct2}.{oct3}.{40 + t}")
                            for pf in (0, 1):
                                fid = f"{did}-pf-{pf}"
                                dpu.functions[fid] = Function(
                                    function_id=fid, dpu_id=did,
                                    function_type="PF", pf_number=pf,
                                    pci_address=f"0000:03:00.{pf}",
                                    operational_state="ACTIVE")
                            _init_dpu_telemetry(dpu)
                            self.dpus[did] = dpu
                            rack.trays.append(tid); rack.dpus.append(did)
                        self.racks[rack_id] = rack
                        n_racks += 1
            self.event("info", "NeoCloudEmulator.1.0.ClusterSeeded",
                       [str(n_racks), str(len(self.trays)),
                        str(len(self.trays) * GPU_PER_TRAY)])
            self._seed_sample_faults()

    def _seed_sample_faults(self):
        """리셋/시드 직후 장애 이력 메뉴가 비지 않도록 샘플 1건 시드."""
        self.faults.clear()
        first = next(iter(self.trays), None)
        if first:
            self.faults.append({
                "tray_id": first, "kind": "reprovision",
                "detail": "unplanned reprovision — Ready/HostReady 트레이 "
                          "재프로비저닝 후 HostReady 복귀 (sample)",
                "at": _iso(), "resolved": True, "resolved_at": _iso()})

    # ── cluster aggregation (light — no per-tray heavy data) ──────────
    def rack_summary(self, rack: Rack) -> dict:
        pw = {"On": 0, "PoweringOn": 0, "Off": 0}
        health = {"ok": 0, "warning": 0, "critical": 0}
        tenants = set()
        for tid in rack.trays:
            tr = self.trays[tid]
            pw[self.power_state(tr)] = pw.get(self.power_state(tr), 0) + 1
            health[tr.health] = health.get(tr.health, 0) + 1
            d = self.dpus.get(tr.dpu_id)
            if d:
                tenants.update(f.tenant_id for f in d.functions.values()
                               if f.tenant_id)
        return {"rack_id": rack.rack_id, "su_id": rack.su_id,
                "site": rack.site, "site_id": rack.site_id, "model": rack.model,
                "trays": len(rack.trays), "gpus": len(rack.trays) * GPU_PER_TRAY,
                "dpus": len(rack.dpus), "power": pw, "health": health,
                "tenants": sorted(tenants),
                "state": ("degraded" if health.get("critical") else
                          "attention" if health.get("warning") else "ready")}

    def reset(self):
        with self.lock:
            self.tenant_networks.clear(); self.attachments.clear()
            self.security_policies.clear(); self.flow_pipes.clear()
            self.flow_entries.clear(); self.qos_policies.clear()
            self.ipsec_sas.clear(); self.policy_txns.clear()
            self.dhcp_leases.clear(); self.events.clear()
            self.seed()

    # ── Redfish power state machine (lazy time-based) ─────────────────
    def power_state(self, tray: ComputeTray) -> str:
        if tray.power_target == "On" and tray.power_state != "On":
            if _now() - tray.power_changed_at > 1.0:
                tray.power_state = "On"
            else:
                tray.power_state = "PoweringOn"
        return tray.power_state

    def set_power(self, tray: ComputeTray, reset_type: str):
        tray.power_changed_at = _now()
        if reset_type in ("On", "ForceRestart", "GracefulRestart"):
            tray.power_target = "On"; tray.power_state = "PoweringOn"
            if reset_type == "ForceRestart":
                tray.boot_stage = "PXE Selected" if tray.boot_source == "Pxe" else "HostReady"
        elif reset_type in ("ForceOff", "GracefulShutdown"):
            tray.power_target = "Off"; tray.power_state = "Off"


def _init_dpu_telemetry(dpu: Dpu):
    for k in ("dpu_flow_entries", "dpu_flow_entry_packets_total",
              "dpu_flow_entry_drops_total", "dpu_acl_drops_total",
              "dpu_spoof_drops_total", "dpu_vlan_violation_drops_total",
              "dpu_vni_violation_drops_total", "dpu_intertenant_drops_total",
              "dpu_default_deny_drops_total", "dpu_policy_apply_failures_total",
              "dpu_ipsec_auth_failures_total"):
        dpu.telemetry[k] = 0
    dpu.telemetry["dpu_arm_up"] = 1
    dpu.telemetry["dpu_up"] = 1
    dpu.telemetry["dpu_policy_generation"] = 0
    dpu.telemetry["dpu_last_known_good_generation"] = 0


STORE = Store()
