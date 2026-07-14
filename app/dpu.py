"""DPU isolation API + isolation engine — design §5.2/§6/§7/§12.

Implements NeoCloud DPU isolation endpoints, the VF/SF/representor model,
tenant attachments with policy generation + Last-Known-Good rollback, and an
emulated-traffic engine that drives default-deny / spoof / inter-tenant
isolation counters and events."""
from typing import Optional
from fastapi import APIRouter, HTTPException
from .store import STORE, Function, Representor, _iso
from . import models as m

router = APIRouter(prefix="/emulator/v1", tags=["dpu-isolation"])


def _dpu(dpu_id: str):
    d = STORE.dpus.get(dpu_id)
    if not d:
        raise HTTPException(404, f"dpu {dpu_id} not found")
    return d


def _dpu_view(d):
    return {
        "dpu_id": d.dpu_id, "compute_tray_id": d.compute_tray_id,
        "bmc_ip": d.bmc_ip, "operating_mode": d.operating_mode,
        "eswitch_mode": d.eswitch_mode, "arm_os_state": d.arm_os_state,
        "bmc_state": d.bmc_state, "attestation_state": d.attestation_state,
        "health": d.health,
        "current_policy_generation": d.current_policy_generation,
        "last_known_good_generation": d.last_known_good_generation,
        "functions": len(d.functions),
        "tenants": sorted({f.tenant_id for f in d.functions.values() if f.tenant_id}),
    }


@router.get("/dpus")
def list_dpus(rack: Optional[str] = None, site: Optional[str] = None,
              tenant: Optional[str] = None, q: Optional[str] = None,
              offset: int = 0, limit: int = 3000):
    """Cluster-scale DPU list (returns a list for back-compat).
    Filter by rack/site/tenant/search and paginate for the 2,520-DPU fleet."""
    with STORE.lock:
        ds = list(STORE.dpus.values())
        if rack:
            ds = [d for d in ds if d.compute_tray_id.startswith(rack + "-")]
        if site:
            ds = [d for d in ds if (STORE.trays.get(d.compute_tray_id)
                  and STORE.trays[d.compute_tray_id].site == site)]
        if tenant:
            ds = [d for d in ds
                  if any(f.tenant_id == tenant for f in d.functions.values())]
        if q:
            ds = [d for d in ds if q in d.dpu_id]
        return [_dpu_view(d) for d in ds[offset:offset + limit]]


@router.get("/dpus/{dpu_id}")
def get_dpu(dpu_id: str):
    with STORE.lock:
        d = _dpu(dpu_id)
        v = _dpu_view(d)
        v["telemetry"] = dict(d.telemetry)
        v["failure_policy"] = d.failure_policy
        return v


@router.patch("/dpus/{dpu_id}/mode")
def set_mode(dpu_id: str, body: m.DpuModePatch):
    with STORE.lock:
        d = _dpu(dpu_id)
        if d.operating_mode == "DPU" and d.functions and body.operating_mode == "NIC":
            # host cannot flip eswitch mode under DPU/zero-trust (§4.1)
            STORE.event("warning", "NeoCloudEmulator.1.0.EswitchModeChangeDenied",
                        [dpu_id])
        d.operating_mode = body.operating_mode
        d.eswitch_mode = "legacy" if body.operating_mode == "NIC" else "switchdev"
        return _dpu_view(d)


@router.get("/dpus/{dpu_id}/ports")
def list_ports(dpu_id: str):
    with STORE.lock:
        d = _dpu(dpu_id)
        return {
            "functions": [vars(f) for f in d.functions.values()],
            "representors": [vars(r) for r in d.representors.values()],
        }


# ── VF / SF (§6) ──────────────────────────────────────────────────────
def _mk_representor(d, f) -> Representor:
    idx = 1000 + len(d.representors)
    rid = f"{d.dpu_id}-rep-{f.function_id.split('-')[-2]}{f.function_id.split('-')[-1]}"
    nd = ("pf0vf%s" % f.vf_number) if f.function_type == "VF" else ("en3f0pf0sf%s" % f.sf_number)
    r = Representor(representor_id=rid, dpu_id=d.dpu_id, function_id=f.function_id,
                    netdev_name=nd, switch_id=f"eswitch-{d.dpu_id}", port_index=idx)
    d.representors[rid] = r
    return r


@router.post("/dpus/{dpu_id}/vfs")
def create_vfs(dpu_id: str, body: m.VfCreate):
    with STORE.lock:
        d = _dpu(dpu_id)
        if body.pf_id not in d.functions:
            raise HTTPException(400, f"pf {body.pf_id} not on dpu")
        made = []
        base = len([f for f in d.functions.values() if f.function_type == "VF"])
        for i in range(body.count):
            vfn = base + i
            fid = f"{dpu_id}-vf-{vfn}"
            f = Function(function_id=fid, dpu_id=dpu_id, function_type="VF",
                         pf_number=0, vf_number=vfn,
                         pci_address=f"0000:03:00.{vfn % 8}",
                         mac_address="02:00:%02x:%02x:%02x:%02x" % (
                             hash(dpu_id) & 0xff, 0, vfn >> 8, vfn & 0xff),
                         trusted=body.trusted, spoof_check=body.spoof_check,
                         link_state=body.link_state, operational_state="ACTIVE")
            d.functions[fid] = f
            _mk_representor(d, f)
            made.append(fid)
        return {"created": made}


@router.delete("/dpus/{dpu_id}/vfs/{vf_id}")
def delete_vf(dpu_id: str, vf_id: str):
    with STORE.lock:
        d = _dpu(dpu_id)
        d.functions.pop(vf_id, None)
        for rid in [r for r, rr in d.representors.items() if rr.function_id == vf_id]:
            d.representors.pop(rid, None)
        return {"deleted": vf_id}


@router.post("/dpus/{dpu_id}/sfs")
def create_sf(dpu_id: str, body: m.SfCreate):
    with STORE.lock:
        d = _dpu(dpu_id)
        fid = f"{dpu_id}-sf-{body.sf_number}"
        f = Function(function_id=fid, dpu_id=dpu_id, function_type="SF",
                     sf_number=body.sf_number, controller_number=body.controller,
                     tenant_id=body.tenant_id, mac_address=body.mac_address,
                     operational_state="ACTIVE")
        d.functions[fid] = f
        _mk_representor(d, f)
        return vars(f)


# ── Tenant attachment (§7.2) + policy generation ──────────────────────
@router.post("/dpus/{dpu_id}/tenant-attachments")
def create_attachment(dpu_id: str, body: m.AttachmentCreate):
    with STORE.lock:
        d = _dpu(dpu_id)
        # 1) allocate policy generation (begin transaction)
        gen = d.current_policy_generation + 1
        # 2) function
        if body.function_id and body.function_id in d.functions:
            f = d.functions[body.function_id]
        else:
            vfn = len([x for x in d.functions.values() if x.function_type == "VF"])
            f = Function(function_id=f"{dpu_id}-vf-{vfn}", dpu_id=dpu_id,
                         function_type="VF", vf_number=vfn, pf_number=0,
                         mac_address=(body.security.allowed_macs or
                                      ["02:00:00:00:%02x:%02x" % (vfn >> 8, vfn & 0xff)])[0],
                         operational_state="ACTIVE")
            d.functions[f.function_id] = f
        f.tenant_id = body.tenant_id
        rep = _mk_representor(d, f)
        # 3) network
        net = body.network.model_dump()
        STORE.tenant_networks[net["network_id"]] = net
        # 4) security policy (default deny) + flow rules
        pid = STORE.nid("secpol")
        STORE.security_policies[pid] = {
            "policy_id": pid, "tenant_id": body.tenant_id,
            "default_action": body.security.default_action,
            "spoof_check": body.security.spoof_check,
            "allowed_macs": body.security.allowed_macs or ([f.mac_address] if f.mac_address else []),
            "allowed_source_cidrs": body.security.allowed_source_cidrs or (
                [net["subnet"]] if net.get("subnet") else []),
            "generation": gen, "policy_status": "applied",
        }
        d.counter("dpu_flow_entries", 3)   # default-deny + anti-spoof + allow
        # 5) commit generation → LKG
        d.current_policy_generation = gen
        d.last_known_good_generation = gen
        d.telemetry["dpu_policy_generation"] = gen
        d.telemetry["dpu_last_known_good_generation"] = gen
        aid = STORE.nid("att")
        STORE.attachments[aid] = {
            "attachment_id": aid, "tenant_id": body.tenant_id, "dpu_id": dpu_id,
            "network_id": net["network_id"], "function_id": f.function_id,
            "representor_id": rep.representor_id, "security_policy_id": pid,
            "policy_generation": gen, "desired_state": "attached",
            "observed_state": "attached", "created_at": _iso(),
        }
        STORE.event("info", "NeoCloudEmulator.1.0.TenantAttachmentReady",
                    [body.tenant_id, dpu_id, f.function_id])
        return {"attachment_id": aid, "function_id": f.function_id,
                "representor_id": rep.representor_id, "generation": gen,
                "ready": True}


@router.delete("/dpus/{dpu_id}/tenant-attachments/{att_id}")
def delete_attachment(dpu_id: str, att_id: str):
    with STORE.lock:
        a = STORE.attachments.pop(att_id, None)
        if not a:
            raise HTTPException(404, "attachment not found")
        d = _dpu(dpu_id)
        d.functions.pop(a["function_id"], None)
        d.representors.pop(a["representor_id"], None)
        STORE.security_policies.pop(a["security_policy_id"], None)
        return {"deleted": att_id}


# ── policy sub-resources ──────────────────────────────────────────────
@router.post("/dpus/{dpu_id}/flow-pipes")
def create_pipe(dpu_id: str, body: m.FlowPipeCreate):
    with STORE.lock:
        _dpu(dpu_id)
        pid = STORE.nid("pipe")
        STORE.flow_pipes[pid] = {"pipe_id": pid, "dpu_id": dpu_id,
                                 **body.model_dump(), "status": "active"}
        return STORE.flow_pipes[pid]


@router.post("/dpus/{dpu_id}/flow-entries")
def create_entry(dpu_id: str, body: m.FlowEntryCreate):
    with STORE.lock:
        d = _dpu(dpu_id)
        eid = STORE.nid("flow")
        STORE.flow_entries[eid] = {"flow_entry_id": eid, "dpu_id": dpu_id,
                                   **body.model_dump(), "packet_count": 0,
                                   "entry_status": "installed"}
        d.counter("dpu_flow_entries", 1)
        return STORE.flow_entries[eid]


@router.delete("/dpus/{dpu_id}/flow-entries/{entry_id}")
def delete_entry(dpu_id: str, entry_id: str):
    with STORE.lock:
        STORE.flow_entries.pop(entry_id, None)
        return {"deleted": entry_id}


@router.post("/dpus/{dpu_id}/security-policies")
def create_secpol(dpu_id: str, body: m.SecurityPolicyCreate):
    with STORE.lock:
        _dpu(dpu_id)
        pid = STORE.nid("secpol")
        STORE.security_policies[pid] = {"policy_id": pid, **body.model_dump(),
                                        "generation": 1, "policy_status": "applied"}
        return STORE.security_policies[pid]


@router.post("/dpus/{dpu_id}/qos-policies")
def create_qos(dpu_id: str, body: m.QosPolicyCreate):
    with STORE.lock:
        _dpu(dpu_id)
        qid = STORE.nid("qos")
        STORE.qos_policies[qid] = {"qos_policy_id": qid, **body.model_dump(),
                                   "policy_status": "applied"}
        return STORE.qos_policies[qid]


@router.post("/dpus/{dpu_id}/ipsec-tunnels")
def create_ipsec(dpu_id: str, body: m.IpsecTunnelCreate):
    with STORE.lock:
        _dpu(dpu_id)
        sid = STORE.nid("sa")
        STORE.ipsec_sas[sid] = {"sa_id": sid, "dpu_id": dpu_id,
                                **body.model_dump(), "state": "active"}
        return STORE.ipsec_sas[sid]


@router.post("/dpus/{dpu_id}/attest")
def attest(dpu_id: str):
    with STORE.lock:
        d = _dpu(dpu_id)
        d.attestation_state = "VALID" if d.secure_boot_enabled else "FIRMWARE_UNTRUSTED"
        return {"dpu_id": dpu_id, "attestation_state": d.attestation_state}


@router.get("/dpus/{dpu_id}/telemetry")
def telemetry(dpu_id: str):
    with STORE.lock:
        d = _dpu(dpu_id)
        return {"dpu_id": dpu_id, "metrics": dict(d.telemetry),
                "health": d.health, "arm_os_state": d.arm_os_state}


# ── isolation engine: emulated traffic (§12.1) ────────────────────────
@router.post("/dpus/{dpu_id}/traffic")
def send_traffic(dpu_id: str, body: m.TrafficGen):
    with STORE.lock:
        d = _dpu(dpu_id)
        src = d.functions.get(body.source_function)
        if not src:
            raise HTTPException(400, "source function not found")
        n = body.packet_count
        result = {"forwarded": 0, "dropped": 0, "reason": None}
        # 1) source MAC spoof
        if src.spoof_check and body.source_mac and src.mac_address \
                and body.source_mac != src.mac_address:
            d.counter("dpu_spoof_drops_total", n)
            d.counter("dpu_flow_entry_drops_total", n)
            result.update(dropped=n, reason="SOURCE_MAC_SPOOF")
            STORE.event("critical", "NeoCloudEmulator.1.0.SourceMacSpoofDetected",
                        [src.function_id, body.source_mac])
            return result
        # 2) inter-tenant default-deny
        dst = d.functions.get(body.destination_function) if body.destination_function else None
        if dst and src.tenant_id and dst.tenant_id and src.tenant_id != dst.tenant_id:
            d.counter("dpu_intertenant_drops_total", n)
            d.counter("dpu_default_deny_drops_total", n)
            d.counter("dpu_flow_entry_drops_total", n)
            result.update(dropped=n, reason="INTER_TENANT_DENY")
            STORE.event("warning", "NeoCloudEmulator.1.0.InterTenantTrafficBlocked",
                        [src.tenant_id, dst.tenant_id, src.function_id,
                         body.destination_function])
            return result
        # 3) forward (allowed intra-tenant / uplink)
        src.tx_packets += n; src.tx_forwarded_packets += n
        if dst:
            dst.rx_packets += n
        d.counter("dpu_flow_entry_packets_total", n)
        result.update(forwarded=n, reason="FORWARDED")
        return result


# ── fault injection (§14) ─────────────────────────────────────────────
@router.post("/dpus/{dpu_id}/faults")
def inject_fault(dpu_id: str, body: m.FaultInject):
    with STORE.lock:
        d = _dpu(dpu_id)
        t = body.type
        if t == "DPU_ARM_OS_CRASH":
            d.arm_os_state = "failed"; d.health = "critical"
            d.telemetry["dpu_arm_up"] = 0
            # fail-closed: disable tenant functions
            for f in d.functions.values():
                if f.function_type in ("VF", "SF"):
                    f.link_state = "disabled"; f.operational_state = "QUARANTINED"
            tray = STORE.trays.get(d.compute_tray_id)
            if tray:
                tray.lifecycle_state = "Degraded"; tray.health = "critical"
            STORE.event("critical", "NeoCloudEmulator.1.0.DpuArmOsFailure", [dpu_id])
        elif t == "DPU_FLOW_PROGRAMMING_FAILURE":
            d.counter("dpu_policy_apply_failures_total", 1)
            prev = d.last_known_good_generation
            d.current_policy_generation = prev
            d.telemetry["dpu_policy_generation"] = prev
            d.health = "warning"
            STORE.event("warning", "NeoCloudEmulator.1.0.DpuPolicyRollbackCompleted",
                        [str(prev + 1), str(prev)])
        elif t in ("DPU_SOURCE_MAC_SPOOF", "DPU_SOURCE_IP_SPOOF"):
            d.counter("dpu_spoof_drops_total", int(body.parameters.get("packets", 100)))
            STORE.event("critical", "NeoCloudEmulator.1.0.SourceMacSpoofDetected", [dpu_id])
        elif t == "IPSEC_REKEY_FAILURE":
            d.counter("dpu_ipsec_auth_failures_total", 1)
            STORE.event("warning", "NeoCloudEmulator.1.0.IpsecRekeyFailure", [dpu_id])
        elif t == "DPU_ESWITCH_FAILURE":
            d.health = "critical"; d.arm_os_state = "degraded"
            STORE.event("critical", "NeoCloudEmulator.1.0.DpuEswitchFailure", [dpu_id])
        else:
            d.counter("dpu_flow_entry_drops_total", 0)
            STORE.event(body.severity or "warning",
                        "NeoCloudEmulator.1.0.FaultInjected", [dpu_id, t])
        return {"dpu_id": dpu_id, "fault": t, "health": d.health,
                "arm_os_state": d.arm_os_state}


@router.post("/dpus/{dpu_id}/recover")
def recover(dpu_id: str):
    """Restore a DPU to healthy + re-apply last-known-good."""
    with STORE.lock:
        d = _dpu(dpu_id)
        d.arm_os_state = "ready"; d.bmc_state = "ok"; d.health = "ok"
        d.telemetry["dpu_arm_up"] = 1; d.telemetry["dpu_up"] = 1
        d.current_policy_generation = d.last_known_good_generation
        for f in d.functions.values():
            if f.operational_state == "QUARANTINED":
                f.operational_state = "ACTIVE"; f.link_state = "auto"
        tray = STORE.trays.get(d.compute_tray_id)
        if tray:
            tray.lifecycle_state = "Ready"; tray.health = "ok"
        STORE.event("info", "NeoCloudEmulator.1.0.DpuRecovered", [dpu_id])
        return _dpu_view(d)
