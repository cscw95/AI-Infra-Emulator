"""Cluster twin invariants — size-agnostic (tests run with a capped cluster;
the real server seeds the full 140-rack / 2,520-tray / 10,080-GPU fleet)."""
from app.store import GPU_PER_TRAY, STORE


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "ok"
    assert d["compute_trays"] == len(STORE.trays)
    assert d["dpus"] == len(STORE.dpus)
    # invariant: one DPU per compute tray
    assert d["compute_trays"] == d["dpus"]


def test_twin_summary(client):
    d = client.get("/emulator/v1/twin").json()
    assert d["model"] == "Vera Rubin NVL72"
    n = len(STORE.trays)
    assert d["compute_trays"] == n
    assert d["dpus"] == n
    assert d["gpu_per_tray"] == GPU_PER_TRAY == 4
    assert d["gpus"] == n * 4                       # 4 Rubin GPU / tray
    assert d["racks"] == len(STORE.racks)
    assert d["compute_trays"] == d["racks"] * 18    # NVL72 = 18 trays / rack


def test_dpus_collection(client):
    dpus = client.get("/emulator/v1/dpus").json()
    assert len(dpus) == len(STORE.dpus)
    ids = {d["dpu_id"] for d in dpus}
    assert "su-1-rack-00-tray-00-dpu-0" in ids
    assert "su-1-rack-00-tray-17-dpu-0" in ids
    for d in dpus:
        assert d["operating_mode"] == "DPU"
        assert d["health"] == "ok"


def test_single_dpu_has_telemetry(client):
    d = client.get("/emulator/v1/dpus/su-1-rack-00-tray-00-dpu-0").json()
    assert d["dpu_id"] == "su-1-rack-00-tray-00-dpu-0"
    assert d["telemetry"]["dpu_arm_up"] == 1
    assert d["telemetry"]["dpu_intertenant_drops_total"] == 0


def test_cluster(client):
    """Cluster overview + per-site NICo controllers are consistent with the twin."""
    cl = client.get("/emulator/v1/cluster").json()
    assert cl["racks"] == len(STORE.racks)
    assert sum(s["racks"] for s in cl["sites"]) == len(STORE.racks)
    # 사이트 컨트롤러(/emulator/v1/sites)는 NICo Emulator로 이관됨
