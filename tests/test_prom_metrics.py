"""F26 AI-Infra /metrics — DCGM (dcgm-exporter 4.9.0 names) · rack power · CDU · DPU."""
import csv
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app import power_model as pm
from app import prom

CSV = Path(__file__).parent / "contracts" / "dcgm-default-counters.csv"


def _csv_fields():
    out = {}
    for row in csv.reader(l for l in CSV.read_text().splitlines() if l and not l.startswith("#")):
        out[row[0].strip()] = row[1].strip()
    return out


def test_default_metrics_lint_and_families(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"] == prom.CONTENT_TYPE_TEXT
    assert prom.lint(r.text) == []
    s = prom.parse(r.text)
    # rack power from the DPS allocator, rating from power_model (227 / 187 kW)
    rated = {lab["profile"]: v for lab, v in s["neocloud_rack_rated_power_watts"]}
    assert rated == {"max-p": pm.RACK_MAXP_KW * 1000, "max-q": pm.RACK_MAXQ_KW * 1000}
    meas = s["neocloud_rack_power_watts"]
    assert meas and all(0 < v <= pm.RACK_MAXP_KW * 1000 for _, v in meas)
    assert {"neocloud_cdu_heat_watts", "neocloud_cdu_leak_detected", "neocloud_gpus",
            "neocloud_power_domain_limit_watts", "dpu_up"} <= set(s)
    # per-GPU DCGM series are opt-in on the fleet endpoint
    assert not any(k.startswith("DCGM_FI_") for k in s)


def test_dcgm_rack_filter_uses_upstream_names(client):
    rid = "su-1-rack-00"
    s = prom.parse(client.get("/metrics", params={"rack": rid}).text)
    fields = _csv_fields()
    dcgm = {k for k in s if k.startswith("DCGM_FI_")}
    assert dcgm and dcgm <= set(fields), dcgm - set(fields)
    assert {"DCGM_FI_DEV_XID_ERROR", "DCGM_FI_DEV_BOARD_POWER_WATTS",
            "DCGM_FI_DEV_GPU_TEMP_CELSIUS"} <= dcgm
    pw = s["DCGM_FI_DEV_BOARD_POWER_WATTS"]
    assert len({lab["uuid"] for lab, _ in pw}) <= 72
    assert all(lab["rack"] == rid and lab["model_name"] == "NVIDIA Rubin" for lab, _ in pw)
    assert all(0 <= v <= 2300 for _, v in pw)


def test_dcgm_types_match_csv(client):
    text = client.get("/metrics", params={"rack": "su-1-rack-00"}).text
    fields = _csv_fields()
    for line in text.splitlines():
        if line.startswith("# TYPE DCGM_FI_"):
            _, _, name, t = line.split()
            want = fields[name]
            # upper-case _TOTAL counters are exposed untyped (promtool counter lint)
            assert t == want or (want == "counter" and t == "untyped" and name.endswith("_TOTAL"))


def test_node_exporter_keeps_upstream_labels(client):
    r = client.get("/dcgm-exporter/su-1-rack-00-tray-00/metrics")
    assert r.status_code == 200
    s = prom.parse(r.text)
    labs = s["DCGM_FI_DEV_GPU_TEMP_CELSIUS"]
    assert len(labs) == 4
    assert {"gpu", "UUID", "pci_bus_id", "device", "modelName", "Hostname"} <= set(labs[0][0])
    assert client.get("/dcgm-exporter/nope/metrics").status_code == 404


def test_openmetrics(client):
    r = client.get("/metrics", params={"rack": "su-1-rack-00"},
                   headers={"accept": "application/openmetrics-text"})
    assert r.headers["content-type"] == prom.CONTENT_TYPE_OM
    assert prom.lint(r.text, openmetrics=True) == []


def test_unknown_filter_404(client):
    assert client.get("/metrics", params={"rack": "no-such-rack"}).status_code == 404


def test_promtool_when_available(client):
    tool = os.environ.get("PROMTOOL") or shutil.which("promtool")
    if not tool:
        pytest.skip("promtool not installed (set PROMTOOL=/path/to/promtool)")
    for params in ({}, {"rack": "su-1-rack-00"}):
        out = subprocess.run([tool, "check", "metrics"], capture_output=True, text=True,
                             input=client.get("/metrics", params=params).text)
        assert out.returncode == 0, out.stdout[:500]
