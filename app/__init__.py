"""AI Infra Emulator — Vera Rubin NVL72 physical digital twin.

Independent software (not part of nocp/NICo). Emulates the *physical* AI
infrastructure the NICo Emulator drives: Vera Rubin NVL72 compute (DCGM
telemetry), Dell PowerScale storage (VAST-shaped API kept as alias),
Quantum-X800 InfiniBand (UFM), Dell PowerSwitch Spectrum-X Ethernet (NetQ),
Converged network, Dell PowerCool DLC cooling, DPU/BlueField, and iDRAC10
Redfish BMC. Hardware vendor: Dell (PowerEdge XE9812); silicon: NVIDIA. Owns the twin state; NICo Emulator (:9000) drives it over REST.
"""
import os

__version__ = "0.2.0"        # this emulator's own version (DSX OS baseline)

# The physical hardware/platform specs this twin reproduces. Header shows the
# primary vendor reference; override with AI_INFRA_REF_URL for a fork/mirror.
EMULATED_NICO = {
    "name": "Dell PowerEdge XE9812 (NVIDIA Vera Rubin NVL72) · PowerCool · PowerScale · Quantum-X800/Spectrum-X",
    "repo": "NVIDIA GB / Vera Rubin NVL72",
    "github": os.environ.get(
        "AI_INFRA_REF_URL",
        "https://www.nvidia.com/en-us/data-center/vera-rubin/"),
    "ref": "rack-scale spec",
    "as_of": "2026",
    "note": "물리 규격 기반 트윈: Vera Rubin NVL72(72 Rubin GPU/36 Vera CPU), "
            "Dell PowerEdge XE9812 · iDRAC10, Dell PowerCool CDU C7000, "
            "Dell PowerScale F910, Quantum-X800 IB, Dell PowerSwitch SN5610 "
            "(Spectrum-X), ConnectX-9/BlueField-4. HW vendor: Dell.",
}

# DSX OS baseline version matrix — identical across the four NeoCloud repos
# (PLAN.md "기준 버전 매트릭스"). Served at /versions and /emulator/v1/versions.
DSX_VERSIONS = {
    "dsx_os": "2026.05", "nico": "2.3.0", "nvke": "0.23.1", "dps": "0.10",
    "mission_control": "2.5", "doca": "3.x", "gpu_operator": "AICR recipe",
    "k8s": ["1.33", "1.34"], "platform": "Vera Rubin NVL72",
}

HW_VENDOR = "Dell"           # whole AI Infra hardware supply vendor (app/dell.py)
