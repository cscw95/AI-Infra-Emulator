"""Compute-tray reboot / HW-replace lifecycle + KPI emulator
(prefix /emulator/v1 — trayops/{tray_id}/reboot|replace, obs/tray-ops).

Tick-based state machine (same TICK_SEC cache pattern as the other engines):
advances one stage per 2.5s TICK and records the measured duration per stage.
Each stage is applied to the twin (STORE) for real:
  - tray power/lifecycle/boot_stage (reuses Redfish set_power)
  - DHCP lease (reuses provisioning._make_lease) — reboot re-leases the same IP,
    replace uses a new serial/MAC → new IP + a full PXE OS reinstall
  - tenant_rejoin: at op start, the tenant attachment on that tray's DPU is
    remembered and detached (the reverse of bridge release), then reapplied via
    dpu.create_attachment at the rejoin stage. Trays with no tenant are marked skip.

While a tray is in progress, observability counts it (via busy_trays()) as GPU
idle and SLO unavailable for that tenant, fires one alert
(TRAY_REBOOTING/TRAY_REPLACING) → resolves it when in_service is reached
(obs alerts → propagated to the /emulator/v1/faults feed).
On reset (STORE.seed_gen change), state is cleared + 2 demo history samples are
seeded ("(sample)")."""
import itertools
import time
from collections import deque
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException

from .store import STORE, _iso

router = APIRouter(prefix="/emulator/v1", tags=["trayops"])

TICK_SEC = 2.5                    # stage advance interval (same as other engines)
HISTORY_MAX = 20
READY_STAGE = "HostReady"         # same as provisioning.READY_STAGE (avoid a cycle)

REBOOT_STAGES = ["power_cycle", "post", "nico_discovery", "dhcp_ip",
                 "boot", "attestation", "tenant_rejoin", "in_service"]
REPLACE_STAGES = ["drain", "hw_swap", "post", "nico_discovery", "dhcp_ip",
                  "pxe_os_install", "attestation", "tenant_rejoin",
                  "in_service"]


class TrayOpsEngine:
    def __init__(self):
        self.seed_gen = -1
        self.tick_no = 0
        self.last = 0.0
        self.inflight: Dict[str, dict] = {}       # tray_id -> op
        self.history: deque = deque(maxlen=HISTORY_MAX)
        self.alerts: Dict[tuple, dict] = {}
        self._alert_seq = itertools.count(1)
        self._swap_seq = itertools.count(1)

    # ── topology (re)build — detect STORE reseed (reset) ───────────────────
    def _ensure_topology(self) -> bool:
        cur = getattr(STORE, "seed_gen", 0)
        if cur == self.seed_gen:
            return False
        self.seed_gen = cur
        self.inflight = {}
        self.history = deque(maxlen=HISTORY_MAX)
        self.alerts = {}
        self.tick_no, self.last = 0, 0.0
        self._seed_samples()
        return True

    def _seed_samples(self):
        """Seed 2 demo samples so the KPI/history panels are not empty right after reset."""
        trays = list(STORE.trays)
        if not trays:
            return
        now = _iso()
        self.history.append({
            "tray_id": trays[0], "op": "reboot", "tenant_id": "tenant-demo",
            "total_s": 17.6,
            "stage_durations": {"power_cycle": 2.5, "post": 2.5,
                                "nico_discovery": 2.5, "dhcp_ip": 2.4,
                                "boot": 2.6, "attestation": 2.5,
                                "tenant_rejoin": 2.6},
            "skipped": [], "succeeded": True, "at": now, "note": "(sample)"})
        t2 = trays[1] if len(trays) > 1 else trays[0]
        self.history.append({
            "tray_id": t2, "op": "replace", "tenant_id": None,
            "total_s": 20.1,
            "stage_durations": {"drain": 2.5, "hw_swap": 2.5, "post": 2.5,
                                "nico_discovery": 2.6, "dhcp_ip": 2.5,
                                "pxe_os_install": 5.0, "attestation": 2.5},
            "skipped": ["tenant_rejoin"], "succeeded": True, "at": now,
            "note": "(sample)"})

    # ── alert helpers (ObsEngine _fire/_resolve pattern) ──────────────────
    def _fire(self, key, severity, resource, summary):
        a = self.alerts.get(key)
        if a and a["state"] == "firing":
            a["severity"], a["summary"] = severity, summary
            return
        self.alerts[key] = {"alert_id": f"top-{next(self._alert_seq):04d}",
                            "domain": "trayops", "severity": severity,
                            "resource": resource, "summary": summary,
                            "at": _iso(), "state": "firing",
                            "source": "trayops"}

    def _resolve(self, key):
        a = self.alerts.get(key)
        if a and a["state"] == "firing":
            a["state"], a["at"] = "resolved", _iso()

    # ── obs integration public helpers ────────────────────────────────────────────
    def busy_trays(self) -> Dict[str, Optional[str]]:
        """In-progress tray → its original tenant. Used by obs for GPU idle / SLO unavail counting."""
        return {tid: o["tenant_id"] for tid, o in self.inflight.items()}

    def alerts_for_obs(self) -> List[dict]:
        """For obs alerts() merge — all 'trayops' domain alarms."""
        self.tick()
        return [dict(a) for a in self.alerts.values()]

    # ── remember/detach & reapply tenant attachment (reuses bridge/dpu logic) ──
    def _detach(self, d) -> List[dict]:
        """Detach the DPU's tenant attachment and store what is needed to reapply it."""
        saved = []
        for aid, a in [(k, v) for k, v in STORE.attachments.items()
                       if v["dpu_id"] == d.dpu_id]:
            net = STORE.tenant_networks.get(a["network_id"])
            saved.append({"tenant_id": a["tenant_id"],
                          "network": dict(net) if net else None})
            STORE.attachments.pop(aid, None)
            d.functions.pop(a["function_id"], None)
            d.representors.pop(a["representor_id"], None)
            STORE.security_policies.pop(a["security_policy_id"], None)
        # release the network of a tenant whose last attachment was reclaimed (same as bridge)
        for s in saved:
            t = s["tenant_id"]
            if not any(x["tenant_id"] == t for x in STORE.attachments.values()):
                for nid in [k for k, n in STORE.tenant_networks.items()
                            if n.get("tenant_id") == t]:
                    STORE.tenant_networks.pop(nid, None)
        return saved

    def _rejoin(self, o, tray):
        from . import dpu as dpu_mod
        from . import models as m
        for s in o["saved"]:
            net = s.get("network") or {
                "network_id": f"net-{s['tenant_id']}-{tray.dpu_id}",
                "tenant_id": s["tenant_id"], "network_type": "vxlan",
                "vni": 10000 + (hash(s["tenant_id"]) % 6000),
                "vrf": s["tenant_id"], "subnet": "10.200.0.0/16"}
            try:
                dpu_mod.create_attachment(tray.dpu_id, m.AttachmentCreate(
                    tenant_id=s["tenant_id"], network=m.TenantNetwork(**net)))
            except Exception:
                o["rejoin_ok"] = False
                STORE.event("critical",
                            "NeoCloudEmulator.1.0.TrayTenantRejoinFailed",
                            [o["tray_id"], s["tenant_id"]])

    # ── apply stage to the twin ──────────────────────────────────────────────────
    def _apply_stage(self, o: dict, name: str):
        tray = STORE.trays.get(o["tray_id"])
        if not tray:
            return
        d = STORE.dpus.get(tray.dpu_id)
        if name != "in_service":
            tray.boot_stage = name
        if name in ("power_cycle", "drain"):
            tray.health = "warning"
            tray.lifecycle_state = "Provisioning"
            if name == "power_cycle":
                STORE.set_power(tray, "ForceOff")
        elif name == "hw_swap":
            STORE.set_power(tray, "ForceOff")
            STORE.dhcp_leases.pop(o["tray_id"], None)   # reclaim the old lease
            n = next(self._swap_seq)
            tray.serial = f"SN-{o['tray_id']}-R{n}"     # assume a new serial
            tray.mac_address = "52:54:01:%02x:%02x:%02x" % (
                (n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff)  # new MAC
            STORE.event("info", "NeoCloudEmulator.1.0.TrayHwSwapped",
                        [o["tray_id"], tray.serial, tray.mac_address])
        elif name == "post":
            STORE.set_power(tray, "On")
        elif name == "nico_discovery":
            if o["op"] == "replace":                    # register the new serial
                STORE.event("info",
                            "NeoCloudEmulator.1.0.TraySerialRegistered",
                            [o["tray_id"], tray.serial])
        elif name == "dhcp_ip":
            from . import provisioning as prov
            lease = prov._make_lease(tray)
            if o["op"] == "reboot":                     # re-lease the same IP
                old = o.get("old_lease")
                if old:
                    lease["ip_address"] = old["ip_address"]
                    lease["mac_address"] = old["mac_address"]
            else:                                       # new MAC → new IP
                lease["mac_address"] = tray.mac_address or lease["mac_address"]
                o1, o2, o3, o4 = lease["ip_address"].split(".")
                lease["ip_address"] = f"{o1}.{o2}.{o3}.{180 + int(o4) % 60}"
            STORE.dhcp_leases[o["tray_id"]] = lease
        elif name == "pxe_os_install":                  # full OS reinstall
            tray.boot_source = "Pxe"
            tray.boot_enabled = "Continuous"
        elif name == "attestation":
            if d:
                d.attestation_state = ("VALID" if d.secure_boot_enabled
                                       else "FIRMWARE_UNTRUSTED")
        elif name == "tenant_rejoin":
            self._rejoin(o, tray)
        elif name == "in_service":
            tray.health = "ok"
            tray.lifecycle_state = "InService" if o["tenant_id"] else "Ready"
            tray.boot_stage = READY_STAGE
            STORE.set_power(tray, "On")
            tray.power_state = "On"

    # ── state machine advance (one stage per tick) ──────────────────────────────────
    def _advance(self, o: dict):
        now = time.monotonic()
        cur = o["stages"][o["stage_idx"]]
        cur["status"] = "done"
        cur["duration_s"] = round(now - o["stage_started"], 2)
        while True:
            o["stage_idx"] += 1
            nxt = o["stages"][o["stage_idx"]]
            if nxt["name"] == "tenant_rejoin" and not o["saved"]:
                nxt["status"], nxt["duration_s"] = "skipped", 0.0
                continue                                # skip does not consume a tick
            break
        o["stage_started"] = now
        if nxt["name"] == "in_service":
            nxt["status"], nxt["duration_s"] = "done", 0.0
            self._apply_stage(o, "in_service")
            self._finish(o)
        else:
            nxt["status"] = "running"
            self._apply_stage(o, nxt["name"])

    def _finish(self, o: dict):
        total = round(time.monotonic() - o["started"], 2)
        self.history.append({
            "tray_id": o["tray_id"], "op": o["op"],
            "tenant_id": o["tenant_id"], "total_s": total,
            "stage_durations": {s["name"]: s["duration_s"]
                                for s in o["stages"]
                                if s["status"] == "done"
                                and s["name"] != "in_service"},
            "skipped": [s["name"] for s in o["stages"]
                        if s["status"] == "skipped"],
            "succeeded": bool(o.get("rejoin_ok", True)),
            "at": _iso()})
        self._resolve(("trayop", o["tray_id"]))
        self.inflight.pop(o["tray_id"], None)
        STORE.event("info", "NeoCloudEmulator.1.0.TrayOpCompleted",
                    [o["tray_id"], o["op"], f"{total}s"])

    def tick(self, force: bool = False):
        with STORE.lock:
            if self._ensure_topology():
                force = True
            now = time.time()
            if not force and self.last and now - self.last < TICK_SEC:
                return
            self.tick_no += 1
            self.last = now
            for o in list(self.inflight.values()):
                self._advance(o)

    # ── op start ───────────────────────────────────────────────────────
    def start(self, tray_id: str, op: str) -> dict:
        with STORE.lock:
            self._ensure_topology()
            tray = STORE.trays.get(tray_id)
            if not tray:
                raise HTTPException(404, f"compute tray {tray_id} not found")
            if tray_id in self.inflight:
                raise HTTPException(
                    409, f"tray op already in progress for {tray_id} "
                         f"({self.inflight[tray_id]['op']})")
            d = STORE.dpus.get(tray.dpu_id)
            old_lease = STORE.dhcp_leases.get(tray_id)
            saved = self._detach(d) if d else []        # remember/detach original tenant
            tenant = saved[0]["tenant_id"] if saved else None
            names = REBOOT_STAGES if op == "reboot" else REPLACE_STAGES
            now = time.monotonic()
            o = {"op_id": STORE.nid("top"), "tray_id": tray_id, "op": op,
                 "tenant_id": tenant, "saved": saved, "rejoin_ok": True,
                 "old_lease": dict(old_lease) if old_lease else None,
                 "stages": [{"name": n, "status": "pending",
                             "duration_s": None} for n in names],
                 "stage_idx": 0, "started": now, "stage_started": now,
                 "at": _iso()}
            o["stages"][0]["status"] = "running"
            self.inflight[tray_id] = o
            self.last = time.time()      # so the first stage lasts at least 1 tick
            kind = "TRAY_REBOOTING" if op == "reboot" else "TRAY_REPLACING"
            label = "재기동" if op == "reboot" else "HW 교체"
            self._fire(("trayop", tray_id),
                       "major" if tenant else "warning", tray_id,
                       f"{kind}: {tray_id} {label} 진행 중"
                       + (f" — tenant {tenant} 영향 (GPU unavail)"
                          if tenant else " (미할당)"))
            self._apply_stage(o, names[0])
            STORE.event("warning", "NeoCloudEmulator.1.0.TrayOpStarted",
                        [tray_id, op, tenant or "-"])
            return self._op_view(o)

    # ── views / KPI ───────────────────────────────────────────────────
    def _op_view(self, o: dict) -> dict:
        return {"op_id": o["op_id"], "tray_id": o["tray_id"], "op": o["op"],
                "tenant_id": o["tenant_id"],
                "stage": o["stages"][o["stage_idx"]]["name"],
                "stage_idx": o["stage_idx"],
                "stages": [dict(s) for s in o["stages"]],
                "elapsed_s": round(time.monotonic() - o["started"], 1),
                "started_at": o["at"]}

    def kpi(self) -> dict:
        hist = list(self.history)
        reboots = sum(1 for h in hist if h["op"] == "reboot")

        def avg(name, rows=None):
            rows = hist if rows is None else rows
            vals = [h["stage_durations"][name] for h in rows
                    if name in h.get("stage_durations", {})]
            return round(sum(vals) / len(vals), 2) if vals else 0.0

        def avg_total(rows):
            t = [h["total_s"] for h in rows]
            return round(sum(t) / len(t), 2) if t else 0.0

        totals = [h["total_s"] for h in hist]
        rejoin = [h for h in hist
                  if "tenant_rejoin" in h.get("stage_durations", {})]
        ok = sum(1 for h in rejoin if h.get("succeeded"))
        # replace-specific KPI — HW swap / OS reinstall / rejoin
        rep = [h for h in hist if h["op"] == "replace"]
        rep_ok = sum(1 for h in rep if h.get("succeeded"))
        reb = [h for h in hist if h["op"] == "reboot"]
        return {
            "ops_24h": len(hist),
            "reboots": reboots,
            "replacements": len(rep),
            "avg_discovery_s": avg("nico_discovery"),
            "avg_ip_s": avg("dhcp_ip"),
            "avg_os_install_s": avg("pxe_os_install"),
            "avg_rejoin_s": avg("tenant_rejoin"),
            "avg_total_s": (round(sum(totals) / len(totals), 2)
                            if totals else 0.0),
            "rejoin_success_pct": (round(100.0 * ok / len(rejoin), 1)
                                   if rejoin else 100.0),
            # replace-op-specific metrics
            "replace": {
                "count": len(rep),
                "avg_drain_s": avg("drain", rep),
                "avg_hw_swap_s": avg("hw_swap", rep),
                "avg_discovery_s": avg("nico_discovery", rep),
                "avg_os_install_s": avg("pxe_os_install", rep),
                "avg_rejoin_s": avg("tenant_rejoin", rep),
                "avg_total_s": avg_total(rep),      # replace MTTR
                "success_pct": (round(100.0 * rep_ok / len(rep), 1)
                                if rep else 100.0),
            },
            "reboot": {
                "count": len(reb),
                "avg_total_s": avg_total(reb),      # reboot MTTR
            },
        }

    def view(self) -> dict:
        return {"inflight": [self._op_view(o)
                             for o in self.inflight.values()],
                "history": list(self.history)[::-1],
                "kpi": self.kpi()}


ENGINE = TrayOpsEngine()


# ── endpoints ─────────────────────────────────────────────────────────
@router.post("/trayops/{tray_id}/reboot")
def tray_reboot(tray_id: str):
    """Start the tray reboot lifecycle — power_cycle → … → tenant_rejoin →
    in_service (one stage per tick, re-lease the same IP)."""
    return ENGINE.start(tray_id, "reboot")


@router.post("/trayops/{tray_id}/replace")
def tray_replace(tray_id: str):
    """Start the tray HW-replace lifecycle — drain → hw_swap (new serial/MAC) →
    … → pxe_os_install (full OS reinstall) → tenant_rejoin → in_service."""
    return ENGINE.start(tray_id, "replace")


@router.get("/obs/tray-ops")
def tray_ops():
    """In-progress tray ops + recent history (20) + KPI (MTTR / per-stage average / rejoin success rate)."""
    ENGINE.tick()
    with STORE.lock:
        return ENGINE.view()
