"""F01 Dynamo engine demo — DGD custom resource, OpenAI frontend with KV-cache
reuse, KV router events, Prometheus metrics, then GPU XID and DPS power-cap
fault injection and recovery.

For the NOCP demo runner (WAVE-v2 시연 요건): copy to NOCP
app/demo/scenarios/f01_dynamo.py. Every call goes to AI-Infra (service "ai").
Self-contained (no import of app/demo/engine.py); the step format follows
engine.py. Deterministic: steps 00-02 clear injected GPU faults, the demo
rack's power cap and all KV caches, and delete the demo DGD; the frontend counts tokens from
fixed prompts, and the XID scenario targets the first decode tray.
The same flow runs from the terminal: scripts/demo_dynamo.sh.
"""

API = "/apis/nvidia.com/v1beta1/namespaces/dynamo/dynamographdeployments"
DGD = "dgd-llama-70b"
RACK = "su-1-rack-00"
MODEL = "llama-3.3-70b-instruct"
DEMO_DGD = "demo-kanana-agg"
SYSTEM = ("You are the NeoCloud token-factory assistant. Answer in one sentence "
          "and cite the rack power envelope when relevant. ") * 8


def http(method, path, json=None, params=None):
    spec = {"method": method, "path": path, "as": "operator", "service": "ai"}
    if json is not None:
        spec["json"] = json
    if params is not None:
        spec["params"] = params
    return spec


def link(route, ko, en):
    return {"console": "ops", "route": route, "label_ko": ko, "label_en": en}


def _b(res):
    return res.body if isinstance(res.body, dict) else {}


def _up(res):
    return res.status not in (0, 502, 503, 504) or isinstance(res.body, dict)


def _chat(question, stream=False):
    body = {"model": MODEL, "max_tokens": 24, "stream": stream,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": question}]}
    if stream:
        body["stream_options"] = {"include_usage": True}
    return http("POST", "/v1/chat/completions", body)


def _sse(text):
    import json as _j
    out = []
    for ln in (text or "").split("\n"):
        if ln.startswith("data: "):
            v = ln[6:]
            out.append(v if v == "[DONE]" else _j.loads(v))
    return out


# ── expectations (bool, ko, en) ───────────────────────────────────────
def _reset_ok(res, ctx):
    if not _up(res):
        return None, "AI-Infra 에뮬레이터 꺼짐 — 건너뜀", "AI-Infra emulator offline — skipped"
    n = len(_b(res).get("cleared") or [])
    return res.status == 200, f"주입된 GPU 장애 해제 ({n}건)", f"injected GPU faults cleared ({n})"


def _list_ok(res, ctx):
    items = _b(res).get("items") or []
    st = {o["metadata"]["name"]: o["status"]["state"] for o in items}
    ok = res.status == 200 and st.get(DGD) == "successful"
    return ok, f"DGD {len(items)}개 · {DGD} 상태 {st.get(DGD)}", \
        f"{len(items)} DGDs · {DGD} state {st.get(DGD)}"


def _created(res, ctx):
    b = _b(res)
    comps = {c["type"]: c["replicas"] for c in (b.get("spec") or {}).get("components", [])}
    ok = res.status == 201 and comps.get("worker") == 2
    return ok, f"DGD {b.get('metadata', {}).get('name')} 생성 · 구성요소 {comps}", \
        f"DGD {b.get('metadata', {}).get('name')} created · components {comps}"


def _usage(res):
    return _b(res).get("usage") or {}


def _first_chat(res, ctx):
    u = _usage(res)
    c = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
    ok = res.status == 200 and c == 0 and u.get("completion_tokens") == 24
    return ok, f"입력 {u.get('prompt_tokens')} · 캐시 {c} · 출력 {u.get('completion_tokens')} 토큰", \
        f"prompt {u.get('prompt_tokens')} · cached {c} · completion {u.get('completion_tokens')} tokens"


def _second_chat(res, ctx):
    u = _usage(res)
    c = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    ok = res.status == 200 and 0 < c < (u.get("prompt_tokens") or 0)
    return ok, (f"같은 시스템 프롬프트 → KV 캐시 적중 {c}/{u.get('prompt_tokens')} 토큰 "
                "(usage.prompt_tokens_details.cached_tokens)"), \
        (f"same system prompt → KV cache hit {c}/{u.get('prompt_tokens')} tokens "
         "(usage.prompt_tokens_details.cached_tokens)")


def _stream_ok(res, ctx):
    cs = _sse(res.text)
    objs = [c for c in cs if isinstance(c, dict)]
    last = objs[-1] if objs else {}
    ok = (res.status == 200 and cs[-1:] == ["[DONE]"] and last.get("choices") == []
          and "usage" in last)
    return ok, f"chat.completion.chunk {len(objs)}개 · 마지막 usage 청크 · [DONE]", \
        f"{len(objs)} chat.completion.chunk events · final usage chunk · [DONE]"


def _kv_ok(res, ctx):
    ev = _b(res).get("events") or []
    kinds = [next(iter(e["router_event"]["event"]["data"]))
             if isinstance(e["router_event"]["event"]["data"], dict)
             else e["router_event"]["event"]["data"] for e in ev]
    ok = res.status == 200 and "stored" in kinds
    return ok, f"KvCacheEvent {len(ev)}개: {kinds[-5:]}", f"{len(ev)} KvCacheEvents: {kinds[-5:]}"


def _kv_save(res, ctx):
    ctx["kv_next"] = _b(res).get("next", 0)


def _metrics_ok(res, ctx):
    t = res.text or ""
    names = ("dynamo_frontend_time_to_first_token_seconds_bucket",
             "dynamo_frontend_inter_token_latency_seconds_bucket",
             'endpoint="chat_completions"', "dynamo_component_router_kv_hit_rate")
    miss = [n for n in names if n not in t]
    return res.status == 200 and not miss, \
        f"Prometheus 지표 확인 (누락 {miss or '없음'})", f"Prometheus metrics present (missing {miss or 'none'})"


def _xid_ok(res, ctx):
    f = (_b(res).get("faults") or [{}])[0]
    return res.status == 201, f"XID {f.get('xid')} 주입 · 트레이 {f.get('tray_id')} GPU {f.get('gpu')}", \
        f"XID {f.get('xid')} injected · tray {f.get('tray_id')} GPU {f.get('gpu')}"


def _degraded(res, ctx):
    s = _b(res).get("status") or {}
    c = (s.get("conditions") or [{}])[0]
    d = (s.get("components") or {}).get("VllmDecodeWorker", {})
    ok = s.get("state") == "pending" and c.get("status") == "False"
    return ok, (f"DGD 상태 {s.get('state')} · Ready={c.get('status')} ({c.get('reason')}) · "
                f"decode {d.get('readyReplicas')}/{d.get('replicas')}"), \
        (f"DGD state {s.get('state')} · Ready={c.get('status')} ({c.get('reason')}) · "
         f"decode {d.get('readyReplicas')}/{d.get('replicas')}")


def _cleared(res, ctx):
    ev = _b(res).get("events") or []
    cl = [e["worker"] for e in ev if e["router_event"]["event"]["data"] == "cleared"]
    return bool(cl), f"KV Cleared 이벤트: {cl}", f"KV Cleared events: {cl}"


def _routed_around(res, ctx):
    return res.status == 200, "장애 워커를 피해 응답 계속 (HTTP 200)", \
        "requests keep succeeding around the failed worker (HTTP 200)"


def _capped(res, ctx):
    return res.status == 200, f"{RACK} 전력 상한 100 kW 적용", f"{RACK} power cap 100 kW applied"


def _slow(res, ctx):
    d = (_b(res).get("deployments") or [{}])[0]
    p = (d.get("power") or [{}])[0]
    ok = p.get("throttled") is True
    tpot = (d.get("expected") or {}).get("tpot_ms")
    return ok, (f"클럭 {p.get('clock')} · 예상 TPOT {tpot} ms · "
                f"토큰/s/MW {d.get('tokens_per_s_per_mw')}"), \
        (f"clock {p.get('clock')} · expected TPOT {tpot} ms · "
         f"tokens/s/MW {d.get('tokens_per_s_per_mw')}")


def _ready_again(res, ctx):
    s = _b(res).get("status") or {}
    return s.get("state") == "successful", f"복구 후 DGD 상태 {s.get('state')}", \
        f"DGD state after recovery {s.get('state')}"


def _status_any(*codes):
    def f(res, ctx):
        return res.status in codes, f"HTTP {res.status}", f"HTTP {res.status}"
    return f


def step(sid, ko, en, call, expect, **kw):
    return {"id": sid, "title_ko": ko, "title_en": en, "call": call, "expect": expect,
            "feature": "F01", **kw}


SCENARIO = {
    "id": "f01_dynamo", "feature": "F01", "order": 11,
    "title_ko": "Dynamo 추론 엔진: DGD·KV 캐시·장애 주입",
    "title_en": "Dynamo inference engine: DGD, KV cache, fault injection",
    "summary_ko": "DGD 커스텀 리소스로 모델을 띄우고 OpenAI 호환 프런트엔드로 호출한다. 시스템 프롬프트가 "
                  "같으면 KV 캐시가 적중하고, XID 장애와 전력 상한이 지연·상태에 바로 반영된다.",
    "summary_en": "Serve a model from a DGD custom resource and call the OpenAI-compatible frontend. "
                  "A repeated system prompt hits the KV cache; an XID fault and a power cap show up "
                  "in latency and status right away.",
    "steps": [
        step("00-reset", "초기화: 장애·전력 상한 해제", "Reset: clear faults and power cap",
             http("POST", "/emulator/v1/gpu-faults/recover", {}), _reset_ok,
             narration_ko="같은 결과가 나오도록 주입된 장애를 지우고 데모 랙 상한을 푼다.",
             narration_en="Clear injected faults and the demo rack cap so every run is identical."),
        step("01-uncap", "데모 랙 상한 해제", "Release the demo rack cap",
             http("POST", f"/emulator/v1/obs/racks/{RACK}/control", {"action": "power_uncap"}),
             _status_any(200)),
        step("01b-kv", "KV 캐시 비우기 (/clear_kv_blocks)", "Clear KV caches (/clear_kv_blocks)",
             http("POST", "/clear_kv_blocks"), _status_any(200),
             save=lambda res, ctx: ctx.update(kv_start=_b(res).get("kv_events_next", 0))),
        step("02-cleanup", "이전 데모 DGD 삭제", "Delete the previous demo DGD",
             http("DELETE", f"{API}/{DEMO_DGD}"),
             lambda res, ctx: (res.status in (200, 404), f"{DEMO_DGD} 없음 (정리됨)",
                               f"{DEMO_DGD} absent (cleaned up)")),
        step("03-dgd-list", "DGD 목록 (nvidia.com/v1beta1)", "List DGDs (nvidia.com/v1beta1)",
             http("GET", API), _list_ok,
             narration_ko="배포는 Dynamo operator CRD 형식(spec.components, status.state)으로 보인다.",
             narration_en="Deployments are shown in the Dynamo operator CRD shape.",
             links=[link("mk8s", "운영 · Dynamo", "Ops · Dynamo")]),
        step("04-dgd-create", "DGD 매니페스트로 집약형 배포 생성", "Create an aggregated DGD from a manifest",
             http("POST", API, {
                 "apiVersion": "nvidia.com/v1beta1", "kind": "DynamoGraphDeployment",
                 "metadata": {"name": DEMO_DGD,
                              "annotations": {"neocloud.io/racks": "su-1-rack-03"}},
                 "spec": {"backendFramework": "vllm", "components": [
                     {"name": "Frontend", "type": "frontend", "replicas": 1},
                     {"name": "VllmWorker", "type": "worker", "replicas": 2,
                      "modelRef": {"name": "kanana-1.5-8b-instruct"}}]}}), _created),
        step("05-chat", "채팅 완성 1회차 (캐시 없음)", "Chat completion, first turn (cold cache)",
             _chat("How much power does one NVL72 rack draw?"), _first_chat,
             narration_ko="usage는 OpenAI 원본 위치(prompt_tokens_details.cached_tokens)에 담긴다.",
             narration_en="Usage sits where OpenAI puts it (prompt_tokens_details.cached_tokens)."),
        step("06-chat-cached", "같은 시스템 프롬프트로 다시 호출", "Call again with the same system prompt",
             _chat("And how many GPUs does it hold?"), _second_chat),
        step("07-stream", "스트리밍 (SSE, include_usage)", "Streaming (SSE, include_usage)",
             _chat("Summarise in five words.", stream=True), _stream_ok),
        step("08-kv-events", "KV 라우터 이벤트 (Stored)", "KV router events (Stored)",
             http("GET", "/dynamo/v1/kv-events",
                  params=lambda c: {"deployment": DGD, "since": c["kv_start"]}), _kv_ok, save=_kv_save,
             narration_ko="워커가 KV 블록을 저장하면 RouterEvent{worker_id, event:{event_id, data:{stored}}}가 나간다.",
             narration_en="Workers publish RouterEvent{worker_id, event:{event_id, data:{stored}}} per stored block."),
        step("09-metrics", "Prometheus 지표", "Prometheus metrics",
             http("GET", "/dynamo/v1/metrics"), _metrics_ok),
        step("10-xid", "장애 주입: XID 79 (복구 불가)", "Inject fault: XID 79 (fatal)",
             http("POST", "/emulator/v1/gpu-faults/scenarios/xid-fatal"), _xid_ok,
             narration_ko="decode 워커의 GPU가 버스에서 빠진다. 운영자 권한 주입.",
             narration_en="A decode worker's GPU falls off the bus. Operator-only injection."),
        step("11-dgd-degraded", "DGD 상태: pending / Ready=False", "DGD status: pending / Ready=False",
             http("GET", f"{API}/{DGD}"), _degraded),
        step("12-kv-cleared", "KV Cleared 이벤트", "KV Cleared event",
             http("GET", "/dynamo/v1/kv-events", params=lambda c: {"since": c["kv_next"]}),
             _cleared),
        step("13-route-around", "장애 중에도 호출 성공", "Calls still succeed during the fault",
             _chat("Still there?"), _routed_around),
        step("14-cap", "전력 상한 100 kW 주입", "Inject a 100 kW power cap",
             http("POST", f"/emulator/v1/obs/racks/{RACK}/control",
                  {"action": "power_cap", "params": {"cap_kw": 100}}), _capped),
        step("15-slow", "상한 → 클럭·TPOT 변화", "Cap → clock and TPOT change",
             http("GET", "/dynamo/v1/stats", params={"deployment": DGD}), _slow),
        step("16-recover", "복구: 장애 해제", "Recover: clear the fault",
             http("POST", "/emulator/v1/gpu-faults/recover", {}), _status_any(200)),
        step("17-uncap", "복구: 상한 해제", "Recover: release the cap",
             http("POST", f"/emulator/v1/obs/racks/{RACK}/control", {"action": "power_uncap"}),
             _status_any(200)),
        step("18-dgd-ready", "DGD 상태: successful", "DGD status: successful",
             http("GET", f"{API}/{DGD}"), _ready_again),
    ],
}
