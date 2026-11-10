"""The F01 demo scenario (app/demo/scenarios/f01_dynamo.py) passes end to end
against the emulator, twice in a row (deterministic reset)."""
import json
from types import SimpleNamespace

from app.demo.scenarios.f01_dynamo import SCENARIO


def _v(x, ctx):
    return x(ctx) if callable(x) else x


def _run(client):
    ctx, out = {}, []
    for st in SCENARIO["steps"]:
        c = {k: _v(v, ctx) for k, v in st["call"].items()}
        assert c["service"] == "ai"
        r = client.request(c["method"], c["path"], json=c.get("json"), params=c.get("params"))
        try:
            body = r.json()
        except ValueError:
            body = None
        res = SimpleNamespace(status=r.status_code, body=body, text=r.text, headers=r.headers)
        ok, ko, en = st["expect"](res, ctx)
        assert ok, (st["id"], ko, r.text[:300])
        assert ko and en
        if st.get("save"):
            st["save"](res, ctx)
        out.append(en)
    return out


def test_scenario_format():
    assert {"id", "title_ko", "title_en", "feature", "steps"} <= set(SCENARIO)
    for st in SCENARIO["steps"]:
        assert {"id", "title_ko", "title_en", "call", "expect"} <= set(st)
    json.dumps({k: v for k, v in SCENARIO.items() if k != "steps"})


def test_scenario_runs_and_repeats(client):
    a = _run(client)
    b = _run(client)
    assert a == b
