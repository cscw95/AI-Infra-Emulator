"""Pydantic request/response models — DPU isolation API + provisioning.

Field names follow the design handoff (NeoCloud_VeraRubin_NVL72_DPU_Isolation).
Python 3.9 compatible (Optional[...] rather than X | None)."""
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


# ── DPU mode ──────────────────────────────────────────────────────────
class DpuModePatch(BaseModel):
    operating_mode: str = Field(..., description="NIC | DPU | Restricted | ZeroTrust | SeparatedHost")


# ── SR-IOV Function (VF / SF) — design §6 ─────────────────────────────
class VfCreate(BaseModel):
    pf_id: str
    count: int = 1
    mode: str = "switchdev"
    trusted: bool = False
    spoof_check: bool = True
    link_state: str = "auto"
    max_tx_rate_mbps: Optional[int] = None
    min_tx_rate_mbps: Optional[int] = None


class SfCreate(BaseModel):
    parent_pf_id: str
    sf_number: int
    controller: int = 0
    tenant_id: Optional[str] = None
    network_id: Optional[str] = None
    mac_address: Optional[str] = None
    state: str = "active"


# ── Tenant network attachment — design §7.2 ───────────────────────────
class TenantNetwork(BaseModel):
    network_id: str
    tenant_id: str
    network_type: str = "vxlan"      # vlan | vxlan
    vlan_id: Optional[int] = None
    vni: Optional[int] = None
    vrf: Optional[str] = None
    subnet: Optional[str] = None
    encryption_required: bool = False


class SecuritySpec(BaseModel):
    default_action: str = "deny"
    spoof_check: bool = True
    allowed_macs: List[str] = []
    allowed_source_cidrs: List[str] = []
    allowed_destination_cidrs: List[str] = []
    allowed_services: List[str] = []


class QosSpec(BaseModel):
    minimum_bandwidth_gbps: Optional[int] = None
    maximum_bandwidth_gbps: Optional[int] = None
    priority: str = "normal"


class EncryptionSpec(BaseModel):
    enabled: bool = False
    protocol: str = "ipsec"
    profile: Optional[str] = None


class AttachmentCreate(BaseModel):
    tenant_id: str
    network: TenantNetwork
    function_type: str = "VF"        # VF | SF
    function_id: Optional[str] = None  # existing function; created if absent
    security: SecuritySpec = SecuritySpec()
    qos: QosSpec = QosSpec()
    encryption: EncryptionSpec = EncryptionSpec()


# ── Flow / policy ─────────────────────────────────────────────────────
class FlowPipeCreate(BaseModel):
    pipe_name: str
    pipe_type: str = "basic"
    domain: str = "egress"
    root_pipe: bool = False


class FlowEntryCreate(BaseModel):
    pipe_id: str
    priority: int = 1000
    tenant_id: Optional[str] = None
    match: Dict[str, Any] = {}
    actions: Dict[str, Any] = {}
    monitor: Dict[str, Any] = {}


class SecurityPolicyCreate(BaseModel):
    tenant_id: str
    policy_name: str
    default_action: str = "deny"
    spoof_check: bool = True
    allowed_macs: List[str] = []
    allowed_source_cidrs: List[str] = []


class QosPolicyCreate(BaseModel):
    tenant_id: str
    minimum_rate_bps: Optional[int] = None
    maximum_rate_bps: Optional[int] = None
    priority: int = 1000


class IpsecTunnelCreate(BaseModel):
    tenant_id: str
    direction: str = "out"
    spi: int
    peer_address: str
    local_address: str
    crypto_suite: str = "aes-gcm-256"


class FaultInject(BaseModel):
    type: str                        # DPU_ARM_OS_CRASH, DPU_SOURCE_MAC_SPOOF, ...
    severity: Optional[str] = None
    parameters: Dict[str, Any] = {}


class TrafficGen(BaseModel):
    """Emulated traffic — drives isolation counters."""
    source_function: str
    destination_function: Optional[str] = None
    source_mac: Optional[str] = None
    source_ip: Optional[str] = None
    destination_ip: Optional[str] = None
    protocol: str = "tcp"
    destination_port: Optional[int] = None
    packet_count: int = 1000


# ── Redfish / provisioning ────────────────────────────────────────────
class ResetAction(BaseModel):
    ResetType: str = "On"            # On | ForceOff | ForceRestart | GracefulShutdown


class BootOverride(BaseModel):
    boot_source: str = "Pxe"         # Pxe | Hdd | ...
    enabled: str = "Once"            # Once | Continuous | Disabled


class ScenarioRun(BaseModel):
    name: str
