"""Provisioning emulator — bare-metal DHCP / PXE / iPXE / DNS + boot machine.

Emulates the out-of-band provisioning network for Vera Rubin NVL72 compute
trays: DHCP lease allocation on the BMC/host subnet, PXE → iPXE chainload, and
the host boot lifecycle (Discovered → Provisioning → Ready). Read-mostly views
plus a small state machine keyed on ComputeTray.boot_stage / lifecycle_state in
the shared twin (STORE). STORE.dhcp_leases is owned here (keyed by tray_id)."""
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from .store import STORE, RACK_ID, _iso

router = APIRouter(prefix="/emulator/v1", tags=["provisioning"])

# ── Boot state machine (PXE → DHCP → iPXE → OS → Host Agent) ───────────
BOOT_SEQUENCE = [
    "PXE Selected",
    "DHCP Lease",
    "iPXE Script Fetch",
    "OS Boot",
    "Host Agent Ready",
]
READY_STAGE = "HostReady"        # terminal boot_stage stored on the tray
LEASE_SECONDS = 86400
DNS_DOMAIN = "nico.local"


def _tray(tray_id: str):
    t = STORE.trays.get(tray_id)
    if not t:
        raise HTTPException(404, f"compute tray {tray_id} not found")
    return t


def _subnet(bmc_ip: str):
    o = (bmc_ip.split(".") + ["0", "0", "0", "0"])[:4]
    return o, f"{o[0]}.{o[1]}.{o[2]}"


def _hostname(tray_id: str) -> str:
    return f"{tray_id}.{DNS_DOMAIN}"


def _make_lease(tray) -> dict:
    o, base = _subnet(tray.bmc_ip)
    host = int(o[3]) + 100
    if host > 254:
        host = 100
    return {
        "tray_id": tray.tray_id,
        "mac_address": "52:54:00:%02x:%02x:%02x" % (
            int(o[1]) & 0xff, int(o[2]) & 0xff, int(o[3]) & 0xff),
        "ip_address": f"{base}.{host}",
        "subnet": f"{base}.0/24",
        "gateway": f"{base}.1",
        "dhcp_server": f"{base}.2",
        "lease_s": LEASE_SECONDS,
        "hostname": _hostname(tray.tray_id),
        "boot_source": "Pxe",
        "state": "bound",
        "allocated_at": _iso(),
    }


def _next_stage(stage: str) -> Optional[str]:
    if stage in BOOT_SEQUENCE:
        i = BOOT_SEQUENCE.index(stage)
        if i + 1 < len(BOOT_SEQUENCE):
            return BOOT_SEQUENCE[i + 1]
    return None


def _status(tray) -> dict:
    return {
        "tray_id": tray.tray_id,
        "lifecycle_state": tray.lifecycle_state,
        "boot_source": tray.boot_source,
        "boot_stage": tray.boot_stage,
        "lease": STORE.dhcp_leases.get(tray.tray_id),
    }


# ── DHCP ──────────────────────────────────────────────────────────────
@router.get("/dhcp/leases")
def leases(tray_id: Optional[str] = None):
    """List DHCP leases; optional ?tray_id= filter."""
    with STORE.lock:
        vals = list(STORE.dhcp_leases.values())
        if tray_id:
            vals = [l for l in vals if l.get("tray_id") == tray_id]
        return vals


@router.delete("/dhcp/leases/{tray_id}")
def release_lease(tray_id: str):
    """Release (delete) a compute tray's DHCP lease."""
    with STORE.lock:
        lease = STORE.dhcp_leases.pop(tray_id, None)
        if not lease:
            raise HTTPException(404, f"no dhcp lease for {tray_id}")
        STORE.event("info", "NeoCloudEmulator.1.0.DhcpLeaseReleased", [tray_id])
        return {"released": tray_id, "lease": lease}


# ── Provisioning / boot state machine ─────────────────────────────────
@router.post("/provision/{tray_id}")
def provision(tray_id: str):
    """Start provisioning a compute tray: allocate a DHCP lease and enter the
    PXE boot state machine.

    이미 HostReady까지 도달했던 트레이(Ready/InService)의 재프로비저닝은
    계획되지 않은 장애로 취급한다: health=warning, critical 이벤트 발행,
    STORE.faults 에 unresolved 에피소드 기록 (HostReady 재도달 시 resolved)."""
    with STORE.lock:
        tray = _tray(tray_id)
        prev_lc, prev_stage = tray.lifecycle_state, tray.boot_stage
        reprovision = (prev_stage in (READY_STAGE, "Host Agent Ready")
                       and prev_lc in ("Ready", "InService"))
        lease = _make_lease(tray)
        STORE.dhcp_leases[tray_id] = lease
        tray.boot_source = "Pxe"
        tray.boot_enabled = "Continuous"
        tray.boot_stage = BOOT_SEQUENCE[0]          # "PXE Selected"
        tray.lifecycle_state = "Provisioning"
        if reprovision:
            tray.health = "warning"
            detail = (f"unplanned reprovision — {prev_lc}/{prev_stage} "
                      f"→ Provisioning (IP/OS 재설치)")
            STORE.faults.append({
                "tray_id": tray_id, "kind": "reprovision", "detail": detail,
                "at": _iso(), "resolved": False, "resolved_at": None})
            STORE.event("critical",
                        "NeoCloudEmulator.1.0.UnplannedReprovision",
                        [tray_id, detail])
        STORE.event("info", "NeoCloudEmulator.1.0.ProvisioningStarted",
                    [tray_id, lease["ip_address"]])
        return {
            "tray_id": tray_id,
            "lifecycle_state": tray.lifecycle_state,
            "boot_stage": tray.boot_stage,
            "next_step": _next_stage(tray.boot_stage),
            "lease": lease,
        }


@router.post("/provision/{tray_id}/step")
def provision_step(tray_id: str):
    """Advance the boot state machine one step toward Host Agent Ready."""
    with STORE.lock:
        tray = _tray(tray_id)
        stage = tray.boot_stage
        if stage in (READY_STAGE, "Host Agent Ready"):
            return {"tray_id": tray_id, "lifecycle_state": tray.lifecycle_state,
                    "boot_stage": READY_STAGE, "stage": "Host Agent Ready",
                    "complete": True, "next_step": None}
        if stage not in BOOT_SEQUENCE:
            raise HTTPException(
                409, f"provisioning not started for {tray_id}; "
                     f"POST /emulator/v1/provision/{tray_id} first")
        new_stage = BOOT_SEQUENCE[min(BOOT_SEQUENCE.index(stage) + 1,
                                      len(BOOT_SEQUENCE) - 1)]
        complete = new_stage == "Host Agent Ready"
        if complete:
            tray.lifecycle_state = "Ready"
            tray.boot_stage = READY_STAGE
            # 재프로비저닝 장애 회복: health 복원 + 에피소드 resolved 마감
            if tray.health == "warning":
                tray.health = "ok"
            for f in reversed(STORE.faults):
                if (f["tray_id"] == tray_id and f["kind"] == "reprovision"
                        and not f["resolved"]):
                    f["resolved"] = True
                    f["resolved_at"] = _iso()
                    STORE.event("info",
                                "NeoCloudEmulator.1.0.ReprovisionResolved",
                                [tray_id])
                    break
        else:
            tray.boot_stage = new_stage
        STORE.event("info", "NeoCloudEmulator.1.0.BootStageAdvanced",
                    [tray_id, new_stage])
        return {
            "tray_id": tray_id,
            "lifecycle_state": tray.lifecycle_state,
            "boot_stage": tray.boot_stage,
            "stage": new_stage,
            "complete": complete,
            "next_step": _next_stage(new_stage),
        }


@router.get("/provision/{tray_id}")
def provision_status(tray_id: str):
    """Current provisioning status: lifecycle_state, boot_stage, lease."""
    with STORE.lock:
        return _status(_tray(tray_id))


def _mac(prefix: str, ip: str) -> str:
    o = (ip.split(".") + ["0", "0", "0", "0"])[:4]
    return "%s:%02x:%02x:%02x" % (prefix, int(o[1]) & 0xff,
                                  int(o[2]) & 0xff, int(o[3]) & 0xff)


@router.get("/provision/{tray_id}/detail")
def provision_detail(tray_id: str):
    """Tray provisioning drill-down for the dashboard popup: DPU-based IP
    allocation (DHCP lease / DNS / MACs), boot-stage stepper state and a
    current-status summary — all derived from the shared twin."""
    with STORE.lock:
        tray = _tray(tray_id)
        d = STORE.dpus.get(tray.dpu_id)
        lease = STORE.dhcp_leases.get(tray_id)
        _o, base = _subnet(tray.bmc_ip)
        host_mac = lease["mac_address"] if lease else _mac("52:54:00", tray.bmc_ip)
        tenants = sorted({f.tenant_id for f in d.functions.values()
                          if f.tenant_id}) if d else []
        dns = [{"hostname": _hostname(tray_id), "type": "A",
                "address": tray.bmc_ip, "ttl": 3600, "scope": "bmc"}]
        if lease:
            dns.append({"hostname": f"host-{tray_id}.{DNS_DOMAIN}", "type": "A",
                        "address": lease["ip_address"], "ttl": 3600,
                        "scope": "host"})
        stage = tray.boot_stage
        complete = stage in (READY_STAGE, "Host Agent Ready")
        idx = (len(BOOT_SEQUENCE) if complete
               else BOOT_SEQUENCE.index(stage) if stage in BOOT_SEQUENCE else -1)
        return {
            "tray": {
                "tray_id": tray.tray_id, "rack_id": tray.rack_id,
                "site": tray.site, "gpus": tray.gpus,
                "power_state": STORE.power_state(tray), "health": tray.health,
                "lifecycle_state": tray.lifecycle_state, "bmc_ip": tray.bmc_ip,
                "serial": tray.serial, "mac": tray.mac_address,
                "tenants": tenants,
            },
            "dpu": {
                "dpu_id": tray.dpu_id,
                "operating_mode": d.operating_mode if d else None,
                "arm_os_state": d.arm_os_state if d else None,
                "health": d.health if d else None,
                "bmc_ip": d.bmc_ip if d else None,
                "oob_mac": _mac("b8:3f:d2", d.bmc_ip) if d else None,
            },
            "ip_allocation": {
                "host_mac": host_mac,
                "dhcp_server": f"{base}.2",
                "gateway": f"{base}.1",
                "subnet": f"{base}.0/24",
                "lease": lease,
                "domain": DNS_DOMAIN,
                "dns_records": dns,
            },
            "boot": {
                "sequence": BOOT_SEQUENCE,
                "boot_stage": stage,
                "boot_source": tray.boot_source,
                "boot_enabled": tray.boot_enabled,
                "stage_index": idx,
                "complete": complete,
                "bootfile_url": f"http://{base}.2:8080/vr/{tray_id}/boot.ipxe",
                "image_url": f"http://{base}.2:8080/vr/{tray_id}/vmlinuz",
            },
        }


# ── PXE / iPXE ────────────────────────────────────────────────────────
@router.get("/pxe/boot.ipxe", response_class=PlainTextResponse)
def ipxe_script(tray_id: str):
    """Synthetic iPXE chainload script (plain text) for a compute tray."""
    with STORE.lock:
        tray = _tray(tray_id)
        lease = STORE.dhcp_leases.get(tray_id)
        _o, base = _subnet(tray.bmc_ip)
        http = f"http://{base}.2:8080"
        ip = lease["ip_address"] if lease else tray.bmc_ip
    return (
        "#!ipxe\n"
        f"# NICo synthetic iPXE chainload for {tray_id} ({RACK_ID})\n"
        f"set base-url {http}/vr/{tray_id}\n"
        f"set hostname {tray_id}\n"
        f"echo Provisioning {tray_id} :: NeoCloud OS bare-metal boot\n"
        "kernel ${base-url}/vmlinuz "
        f"ip={ip} console=ttyS0 nico.tray={tray_id} nico.rack={RACK_ID} "
        "nico.stage=provision\n"
        "initrd ${base-url}/initrd.img\n"
        "boot\n"
    )


# ── Faults (전 도메인 장애 피드 — NOCP /emu/faults가 merge) ───────────
@router.get("/faults")
def faults(limit: int = 30):
    """트윈 전 도메인 장애 이력 — NOCP·포털 상단 알림의 원천 피드.

    재프로비저닝 에피소드 + 통합 obs 알림(랙 제어/냉각/패브릭/스토리지)을
    동일 shape로 합친다: [{tray_id, kind, detail, at, resolved, ...}]."""
    with STORE.lock:
        items = list(STORE.faults)
        try:                          # obs 알림 merge (지연 import — 순환 방지)
            from .observability import ENGINE as _OBS
            _OBS.tick()
            for a in _OBS.alert_list():
                if a.get("domain") == "provisioning":
                    continue          # STORE.faults가 원본 — 중복 방지
                items.append({
                    "tray_id": a.get("resource", "-"),
                    "kind": a.get("domain", "obs"),
                    "severity": a.get("severity"),
                    "detail": a.get("summary", ""),
                    "at": a.get("at"),
                    "resolved": a.get("state") != "firing",
                })
        except Exception:
            pass
        items.sort(key=lambda f: f.get("at") or "")
        return {
            "count": len(items),
            "open": sum(1 for f in items if not f["resolved"]),
            "recent": items[-max(1, min(limit, 200)):][::-1],
        }


# ── DNS ───────────────────────────────────────────────────────────────
@router.get("/dns/records")
def dns_records():
    """Synthetic forward (A) records for compute trays: hostname → bmc_ip
    (plus the leased host IP where a lease is active)."""
    with STORE.lock:
        recs = []
        for t in STORE.trays.values():
            recs.append({"hostname": _hostname(t.tray_id), "type": "A",
                         "address": t.bmc_ip, "ttl": 3600})
            lease = STORE.dhcp_leases.get(t.tray_id)
            if lease:
                recs.append({"hostname": f"host-{t.tray_id}.{DNS_DOMAIN}",
                             "type": "A", "address": lease["ip_address"],
                             "ttl": 3600})
        return {"domain": DNS_DOMAIN, "count": len(recs), "records": recs}
