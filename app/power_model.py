"""Vera Rubin NVL72 rack power model — NCP reference design numbers.

The rack is the reference: NOCP `app/spec.py` (vr-nvl72) fixes
    MaxP = 227 kW (= rack TDP), MaxQ = 187 kW, 4 power shelves × 110 kW.
Every per-tray / per-GPU value used by the twin (DCGM power, DPS profiles,
Redfish PowerLimit ranges, PSU capacity, OOB cap_kw) is derived from those
two rack numbers here, so the emulator and NOCP cannot drift apart.

Derivation (GPU_MAXP_W and NVSWITCH_TRAY_W are the only assumptions):
    rack_gpu_kw      = 72 GPU × GPU_MAXP_W                  = 72 × 2.3   = 165.6 kW
    RACK_OVERHEAD_KW = RACK_MAXP_KW − rack_gpu_kw           = 227 − 165.6 = 61.4 kW
                       (9 NVLink switch trays + per-tray Vera CPU/LPDDR/NIC/DPU/fans)
    TRAY_HOST_W      = (overhead − 9 × NVSWITCH_TRAY_W) / 18 = (61.4 − 21.6) / 18 = 2211.1 W
    TRAY_MAXP_W      = 4 × GPU_MAXP_W + TRAY_HOST_W          = 11 411.1 W
    GPU_MAXQ_W       = (RACK_MAXQ_KW − overhead) / 72        = 125.6 / 72 = 1744.4 W
    MAXQ_POWER_FRAC  = GPU_MAXQ_W / GPU_MAXP_W               = 0.7585
    rack(profile)    = 72 × GPU_MAXP_W × power_frac + overhead   → max-p 227, max-q 187
    PSU_RATED_W      = POWER_SHELF_KW / PSU_PER_SHELF        = 110 / 6 = 18 333.3 W
    rack PSU capacity = 4 shelves × 110 kW = 440 kW (N+1 shelf: 3 shelves = 330 kW ≥ MaxP)
    TRAY_ENVELOPE_W  = RACK_MAXP_KW / 18 (tray share incl. NVSwitch) = 12 611.1 W
                       → Redfish tray PowerLimit AllowableMax; rack AllowableMax = 227 000 W
    GPU_POWER_SHARE  = rack_gpu_kw / RACK_MAXP_KW            = 0.7295
                       → per-GPU cap = tray cap × share / 4 (2300 W at a 227 kW rack cap)
"""

# ── reference rack (NOCP spec.py vr-nvl72, NCP VR NVL72 reference design) ──
RACK_MAXP_KW = 227.0
RACK_MAXQ_KW = 187.0
COMPUTE_TRAYS = 18
GPU_PER_TRAY = 4
GPUS_PER_RACK = COMPUTE_TRAYS * GPU_PER_TRAY          # 72
NVLINK_SWITCH_TRAYS = 9
POWER_SHELVES = 4
POWER_SHELF_KW = 110.0
PSU_PER_SHELF = 6

# ── assumptions ────────────────────────────────────────────────────────────
GPU_MAXP_W = 2300.0           # Rubin GPU board power at Max-P
NVSWITCH_TRAY_W = 2400.0      # NVLink6 switch tray (4 ASICs) at full load

# ── derived ────────────────────────────────────────────────────────────────
RACK_GPU_KW = GPUS_PER_RACK * GPU_MAXP_W / 1000.0                       # 165.6
RACK_OVERHEAD_KW = round(RACK_MAXP_KW - RACK_GPU_KW, 3)                 # 61.4
TRAY_HOST_W = (RACK_OVERHEAD_KW * 1000.0
               - NVLINK_SWITCH_TRAYS * NVSWITCH_TRAY_W) / COMPUTE_TRAYS  # 2211.1
TRAY_MAXP_W = GPU_PER_TRAY * GPU_MAXP_W + TRAY_HOST_W                   # 11411.1
GPU_MAXQ_W = (RACK_MAXQ_KW - RACK_OVERHEAD_KW) * 1000.0 / GPUS_PER_RACK  # 1744.4
MAXQ_POWER_FRAC = GPU_MAXQ_W / GPU_MAXP_W                               # 0.7585
PSU_RATED_W = POWER_SHELF_KW * 1000.0 / PSU_PER_SHELF                   # 18333.3
RACK_PSU_CAPACITY_KW = POWER_SHELVES * POWER_SHELF_KW                   # 440
TRAY_ENVELOPE_W = RACK_MAXP_KW * 1000.0 / COMPUTE_TRAYS                 # 12611.1
GPU_POWER_SHARE = RACK_GPU_KW / RACK_MAXP_KW                            # 0.7295


def rack_kw(n_gpu: int = GPUS_PER_RACK, power_frac: float = 1.0) -> float:
    """Rack peak power for `n_gpu` GPUs at a GPU power fraction of Max-P.
    Overhead scales with the rack size (a partial rack carries its share)."""
    share = n_gpu / GPUS_PER_RACK if GPUS_PER_RACK else 0.0
    return n_gpu * GPU_MAXP_W * power_frac / 1000.0 + RACK_OVERHEAD_KW * share


def tray_kw(power_frac: float = 1.0) -> float:
    """Compute-tray peak power (4 GPUs + tray host), without NVSwitch share."""
    return (GPU_PER_TRAY * GPU_MAXP_W * power_frac + TRAY_HOST_W) / 1000.0


# WPPS profile → fraction of rack MaxP (same table as NOCP power.WPPS_PROFILES
# cap_factor; max-q is exactly MaxQ/MaxP). The GPU power fraction follows:
#   gpu_frac = (rack_frac × RACK_MAXP_KW − RACK_OVERHEAD_KW) / RACK_GPU_KW
PROFILE_RACK_FRAC = {
    "max-p": 1.0,
    "training": 0.92,
    "balanced": 0.87,
    "max-q": RACK_MAXQ_KW / RACK_MAXP_KW,
    "inference": 0.75,
}


def gpu_power_frac(rack_frac: float) -> float:
    return (rack_frac * RACK_MAXP_KW - RACK_OVERHEAD_KW) / RACK_GPU_KW
