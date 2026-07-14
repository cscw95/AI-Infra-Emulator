"""NICo Emulator — standalone NVIDIA Infra Controller + Vera Rubin NVL72 digital twin.

Independent software (not part of nocp). Emulates the NICo control plane surface
that NeoCloud OS (NOCP) integrates with: Redfish BMC, provisioning (DHCP/PXE),
fabric (NVLink/IB/Ethernet), and DPU-enforced tenant isolation with a fault engine.
"""
import os

__version__ = "0.1.0"        # this emulator's own version

# The upstream NVIDIA Infra Controller (NICo) this emulator reproduces.
# Override the GitHub URL with NICO_GITHUB_URL if you host a fork/mirror.
EMULATED_NICO = {
    "name": "NVIDIA Infra Controller (NICo)",
    "repo": "NVIDIA/infra-controller",
    "github": os.environ.get("NICO_GITHUB_URL",
                             "https://github.com/NVIDIA/infra-controller"),
    "ref": "main",
    "as_of": "2026-06",       # infra-controller-rest archived 2026-06-02
    "note": "Behavioral contract of infra-controller main "
            "(Redfish/DPU/PXE/site-workflow); rest-api archived 2026-06-02.",
}
