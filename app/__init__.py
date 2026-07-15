"""AI Infra Emulator — Vera Rubin NVL72 physical digital twin.

Independent software (not part of nocp/NICo). Emulates the *physical* AI
infrastructure the NICo Emulator drives: Vera Rubin NVL72 compute (DCGM
telemetry), VAST Data storage, Quantum-X800 InfiniBand (UFM), Spectrum-X
Ethernet (NetQ), Converged network, SMCI DLC cooling, DPU/BlueField, and
Redfish BMC. Owns the twin state; NICo Emulator (:9000) drives it over REST.
"""
import os

__version__ = "0.1.0"        # this emulator's own version

# The physical hardware/platform specs this twin reproduces. Header shows the
# primary vendor reference; override with AI_INFRA_REF_URL for a fork/mirror.
EMULATED_NICO = {
    "name": "NVIDIA Vera Rubin NVL72 · SMCI DLC · VAST · Quantum-X800/Spectrum-X",
    "repo": "NVIDIA GB / Vera Rubin NVL72",
    "github": os.environ.get(
        "AI_INFRA_REF_URL",
        "https://www.nvidia.com/en-us/data-center/vera-rubin/"),
    "ref": "rack-scale spec",
    "as_of": "2026",
    "note": "물리 규격 기반 트윈: Vera Rubin NVL72(72 Rubin GPU/36 Vera CPU), "
            "SMCI in-row DLC-2 CDU, VAST DASE, Quantum-X800 IB, Spectrum-X "
            "Ethernet, ConnectX-9/BlueField-4.",
}
