"""Security controls (app.security): SSRF allowlist, optional API key and
Redfish Basic auth, CORS lock, input bounds and SSE/subscription caps."""
import base64

import pytest

from app import redfish, security, exchange

RACK = "su-1-rack-00"


# ── SSRF ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/hook", "https://localhost/x", "http://[::1]:8000/h"])
def test_outbound_allowed_by_default(url):
    assert security.check_outbound_url(url) == url


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "gopher://127.0.0.1/", "ftp://127.0.0.1/",
    "http://169.254.169.254/latest/meta-data/", "http://[fe80::1]/",
    "http://0.0.0.0:8000/", "http://10.0.0.5/hook", "http://example.com/",
    "http://user:pw@127.0.0.1/", "http://[::ffff:169.254.169.254]/", ""])
def test_outbound_rejected(url):
    with pytest.raises(security.UnsafeURL):
        security.check_outbound_url(url)


def test_outbound_allowlist_env(monkeypatch):
    monkeypatch.setenv("EMU_OUTBOUND_ALLOWLIST", "10.0.0.0/8")
    assert security.check_outbound_url("http://10.1.2.3/h")
    with pytest.raises(security.UnsafeURL):
        security.check_outbound_url("http://127.0.0.1/h")
    # metadata stays blocked even with a wildcard allowlist
    monkeypatch.setenv("EMU_OUTBOUND_ALLOWLIST", "*")
    with pytest.raises(security.UnsafeURL):
        security.check_outbound_url("http://169.254.169.254/")


def test_subscription_destination_validated(client):
    for bad in ("http://169.254.169.254/", "file:///etc/passwd", "http://10.9.9.9/x"):
        r = client.post("/redfish/v1/EventService/Subscriptions",
                        json={"Destination": bad})
        assert r.status_code == 400, bad
    ok = client.post("/redfish/v1/EventService/Subscriptions",
                     json={"Destination": "http://127.0.0.1:8000/hook"})
    assert ok.status_code == 201
    client.delete(f"/redfish/v1/EventService/Subscriptions/{ok.json()['Id']}")


def test_subscription_cap(client, monkeypatch):
    monkeypatch.setattr(redfish, "MAX_SUBSCRIPTIONS", 2)
    redfish._SUBSCRIPTIONS.clear()
    for _ in range(2):
        assert client.post("/redfish/v1/EventService/Subscriptions",
                           json={"Destination": "http://127.0.0.1:1/x"}).status_code == 201
    assert client.post("/redfish/v1/EventService/Subscriptions",
                       json={"Destination": "http://127.0.0.1:1/x"}).status_code == 503
    redfish._SUBSCRIPTIONS.clear()


# ── auth ──────────────────────────────────────────────────────────────
def test_api_key_off_by_default(client):
    assert client.post("/dps/v1/flex-events", json={
        "domain": "pd-gasan", "reduce_kw": 10, "duration_s": 60}).status_code == 201


def test_api_key_required_on_mutations(client, monkeypatch):
    monkeypatch.setenv("EMU_API_KEY", "s3cret")
    body = {"domain": "pd-gasan", "reduce_kw": 10, "duration_s": 60}
    assert client.post("/dps/v1/flex-events", json=body).status_code == 401
    assert client.post("/dps/v1/flex-events", json=body,
                       headers={"X-API-Key": "nope"}).status_code == 401
    assert client.post("/dps/v1/flex-events", json=body,
                       headers={"X-API-Key": "s3cret"}).status_code == 201
    assert client.post("/emulator/v1/reset",
                       headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert client.post("/emulator/v1/reset").status_code == 401
    assert client.get("/dps/v1/power-domains").status_code == 200   # reads stay open


def _basic(u, p):
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}


def test_redfish_basic_auth(client, monkeypatch):
    monkeypatch.setenv("EMU_REDFISH_USER", "admin")
    monkeypatch.setenv("EMU_REDFISH_PASSWORD", "pw")
    assert client.get("/redfish/v1").status_code == 200          # service root open
    r = client.get("/redfish/v1/Systems")
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    assert client.get("/redfish/v1/Systems", headers=_basic("admin", "bad")).status_code == 401
    assert client.get("/redfish/v1/Systems", headers=_basic("admin", "pw")).status_code == 200
    # Basic also satisfies the API key on Redfish mutations
    monkeypatch.setenv("EMU_API_KEY", "k")
    assert client.patch(f"/redfish/v1/Chassis/{RACK}/Controls/PowerLimit",
                        json={"SetPoint": 150000},
                        headers=_basic("admin", "pw")).status_code == 200


# ── CORS ──────────────────────────────────────────────────────────────
def test_cors_locked_to_configured_origins(client):
    pre = {"Access-Control-Request-Method": "POST"}
    ok = client.options("/dps/v1/flex-events",
                        headers={"Origin": "http://localhost:8090", **pre})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:8090"
    evil = client.options("/dps/v1/flex-events",
                          headers={"Origin": "https://evil.example", **pre})
    assert "access-control-allow-origin" not in evil.headers


# ── bounds / DoS ──────────────────────────────────────────────────────
def test_simulate_bounds(client):
    s = "/dps/v1/simulate"
    assert client.post(s, json={"operating_limit_kw": 1e12}).status_code == 422
    assert client.post(s, json={"operating_limit_kw": 1000,
                                "gpus_per_rack": 10**7}).status_code == 422
    assert client.post(s, json={"operating_limit_kw": 1000,
                                "gpus_per_rack": 72}).status_code == 200


def test_flex_and_domain_bounds(client):
    assert client.post("/dps/v1/flex-events", json={
        "domain": "pd-gasan", "reduce_kw": -5}).status_code == 422
    assert client.post("/dps/v1/power-domains", json={
        "name": "x", "operating_limit_kw": 1e15}).status_code == 422
    assert client.patch("/dps/v1/power-domains/pd-gasan",
                        json={"operating_limit_kw": 1e15}).status_code == 422


def test_setpoint_bounds(client):
    url = f"/redfish/v1/Chassis/{RACK}/Controls/PowerLimit"
    assert client.patch(url, json={"SetPoint": 1e12}).status_code == 400
    assert client.patch(url, json={"SetPoint": -1}).status_code == 400
    assert client.patch(url, json={"SetPoint": 150000}).status_code == 200


def test_pagination_and_body_caps(client):
    assert client.get("/emulator/v1/cluster/racks?limit=100000000").status_code == 422
    assert client.get("/emulator/v1/cluster/racks?offset=-5").status_code == 422
    assert client.get("/emulator/v1/events?limit=-1").status_code == 422
    big = "x" * (1024 * 1024 + 10)
    assert client.post("/dps/v1/simulate", content=big,
                       headers={"Content-Type": "application/json"}).status_code == 413


def test_sse_timeout_and_subscriber_cap(client, monkeypatch):
    assert client.get("/exchange/v1/events?timeout_s=999999").status_code == 422
    monkeypatch.setattr(exchange, "SSE_MAX_CLIENTS", 0)
    assert client.get("/exchange/v1/events?timeout_s=1").status_code == 503
    # backlog-only reads are not counted against the cap
    assert client.get("/exchange/v1/events?stream=false").status_code == 200


def test_host_header_cannot_bypass_redfish_auth(client, monkeypatch):
    monkeypatch.setenv("EMU_REDFISH_USER", "admin")
    monkeypatch.setenv("EMU_REDFISH_PASSWORD", "pw")
    r = client.get("/redfish/v1/Systems", headers={"Host": "x/redfish/../emulator"})
    assert r.status_code in (400, 401)


def test_range_header_cap(client):
    r = client.get("/", headers={"Range": "bytes=" + ",".join(f"{i}-{i}" for i in range(50))})
    assert r.status_code == 416
