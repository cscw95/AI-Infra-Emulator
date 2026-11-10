#!/usr/bin/env bash
# F01 Dynamo engine demo (terminal). Same flow as app/demo/scenarios/f01_dynamo.py:
# DGD custom resource (nvidia.com/v1beta1) → OpenAI chat completions with KV-cache
# reuse (usage.prompt_tokens_details.cached_tokens) → SSE stream → KV router
# events → Prometheus metrics → XID 79 + 100 kW power-cap injection → recovery.
# Each step prints the exact curl command, the response status/headers and the
# raw body (clipped).
#
#   scripts/demo_dynamo.sh                 # emulator already on $BASE
#   START=1 scripts/demo_dynamo.sh         # boot a temporary emulator on $BASE, stop at the end
#   BASE=http://127.0.0.1:9100 scripts/demo_dynamo.sh
set -u
BASE=${BASE:-http://127.0.0.1:20171}
API=/apis/nvidia.com/v1beta1/namespaces/dynamo/dynamographdeployments
DGD=dgd-llama-70b RACK=su-1-rack-00 MODEL=llama-3.3-70b-instruct
TMP=$(mktemp -d "${TMPDIR:-/tmp}/demo_dynamo.XXXX")
PY=${PY:-python3}
PID=
cleanup() { [ -n "$PID" ] && kill "$PID" 2>/dev/null; rm -rf "$TMP"; }
trap cleanup EXIT

if [ "${START:-0}" = 1 ]; then
  cd "$(dirname "$0")/.." || exit 1
  port=${BASE##*:}
  VENV=${VENV:-.venv/bin/python}
  [ -x "$VENV" ] || VENV=$PY
  "$VENV" -m uvicorn app.main:app --host 127.0.0.1 --port "$port" >"$TMP/server.log" 2>&1 &
  PID=$!
  for _ in $(seq 1 60); do curl -s "$BASE/dynamo/v1/models" >/dev/null 2>&1 && break; sleep 0.5; done
fi
if ! curl -s "$BASE/dynamo/v1/models" >/dev/null 2>&1; then
  echo "AI-Infra emulator not reachable at $BASE (start it, or run with START=1)" >&2
  exit 1
fi

SYSTEM="You are the NeoCloud token-factory assistant. Answer in one sentence and cite the rack power envelope when relevant. "
SYSTEM=$(printf '%.0s'"$SYSTEM" 1 2 3 4 5 6 7 8)

chat() {  # chat <question> [stream]
  $PY - "$MODEL" "$SYSTEM" "$1" "${2:-}" <<'EOF'
import json, sys
m, sysmsg, q, stream = sys.argv[1:5]
b = {"model": m, "max_tokens": 24,
     "messages": [{"role": "system", "content": sysmsg}, {"role": "user", "content": q}]}
if stream:
    b.update(stream=True, stream_options={"include_usage": True})
print(json.dumps(b))
EOF
}

step() { printf '\n\033[1m━━ [%s] %s\n   %s\033[0m\n' "$1" "$2" "$3"; }

call() {  # call METHOD PATH [JSON]
  local m=$1 p=$2 d=${3:-}
  local args=(-sS -X "$m" "$BASE$p" -D "$TMP/h" -o "$TMP/b")
  if [ -n "$d" ]; then
    args+=(-H 'content-type: application/json' --data-binary "$d")
    local shown=$d; [ ${#shown} -gt 240 ] && shown="${shown:0:240}…"
    printf "\$ curl -X %s '%s%s' -H 'content-type: application/json' -d '%s'\n" "$m" "$BASE" "$p" "$shown"
  else
    printf "\$ curl -X %s '%s%s'\n" "$m" "$BASE" "$p"
  fi
  curl "${args[@]}"
  $PY - "$TMP/h" "$TMP/b" "${SHOW:-}" <<'EOF'
import json, sys
h = open(sys.argv[1]).read().splitlines()
body = open(sys.argv[2]).read()
show = sys.argv[3]
print("< " + h[0])
for ln in h[1:]:
    k = ln.split(":", 1)[0].lower()
    if k in ("content-type", "x-request-id"):
        print("< " + ln)
if body.startswith("data: "):
    lines = [l for l in body.splitlines() if l.startswith("data: ")]
    keep = lines[:3] + (["  … %d more chunks …" % (len(lines) - 6)] if len(lines) > 6 else []) + lines[-3:]
    print("\n".join(keep if len(lines) > 6 else lines))
elif show:
    print("\n".join(l for l in body.splitlines() if any(s in l for s in show.split("|")))[:2500])
else:
    try:
        txt = json.dumps(json.loads(body), indent=1, ensure_ascii=False)
    except ValueError:
        txt = body
    print(txt[:1800] + ("\n  … (clipped)" if len(txt) > 1800 else ""))
EOF
}

jget() { $PY -c "import json,sys; d=json.load(open('$TMP/b')); print(eval(sys.argv[1]))" "$1"; }

step 00 "초기화: 장애·상한·KV 캐시 정리" "Reset: clear faults, cap and KV caches"
call POST /emulator/v1/gpu-faults/recover '{}'
call POST "/emulator/v1/obs/racks/$RACK/control" '{"action":"power_uncap"}' >/dev/null
call POST /clear_kv_blocks >/dev/null
KV0=$(jget 'd["kv_events_next"]')
call DELETE "$API/demo-kanana-agg" >/dev/null

step 01 "DGD 조회 (Dynamo operator CRD nvidia.com/v1beta1)" "Get a DGD (Dynamo operator CRD nvidia.com/v1beta1)"
call GET "$API/$DGD"

step 02 "DGD 매니페스트로 집약형 배포 생성" "Create an aggregated DGD from a manifest"
call POST "$API" '{"apiVersion":"nvidia.com/v1beta1","kind":"DynamoGraphDeployment","metadata":{"name":"demo-kanana-agg","annotations":{"neocloud.io/racks":"su-1-rack-03"}},"spec":{"backendFramework":"vllm","components":[{"name":"Frontend","type":"frontend","replicas":1},{"name":"VllmWorker","type":"worker","replicas":2,"modelRef":{"name":"kanana-1.5-8b-instruct"}}]}}'

step 03 "채팅 1회차: 캐시 없음" "Chat, first turn: cold cache"
call POST /v1/chat/completions "$(chat 'How much power does one NVL72 rack draw?')"
echo "→ usage: $(jget 'd["usage"]')"

step 04 "같은 시스템 프롬프트: KV 캐시 적중" "Same system prompt: KV cache hit"
call POST /v1/chat/completions "$(chat 'And how many GPUs does it hold?')" >/dev/null
echo "→ usage: $(jget 'd["usage"]')"
echo "→ cached_tokens = $(jget 'd["usage"]["prompt_tokens_details"]["cached_tokens"]') (OpenAI CompletionUsage.prompt_tokens_details)"

step 05 "스트리밍 SSE (include_usage)" "Streaming SSE (include_usage)"
call POST /v1/chat/completions "$(chat 'Summarise in five words.' stream)"

step 06 "KV 라우터 이벤트 (RouterEvent / KvCacheEvent)" "KV router events (RouterEvent / KvCacheEvent)"
call GET "/dynamo/v1/kv-events?deployment=$DGD&since=$KV0&limit=2"
call GET "/dynamo/v1/kv-events?since=$KV0" >/dev/null
KV1=$(jget 'd["next"]')

step 07 "Prometheus 지표 (dynamo_frontend_*)" "Prometheus metrics (dynamo_frontend_*)"
SHOW='dynamo_frontend_requests_total{dynamo_namespace="dgd-llama|dynamo_frontend_time_to_first_token_seconds_count|dynamo_component_router_kv_hit_rate{dynamo_namespace="dgd-llama' \
  call GET /dynamo/v1/metrics

step 08 "장애 주입: XID 79 (decode 워커 GPU)" "Inject fault: XID 79 (decode worker GPU)"
call POST /emulator/v1/gpu-faults/scenarios/xid-fatal

step 09 "DGD 상태 → pending / Ready=False" "DGD status → pending / Ready=False"
call GET "$API/$DGD" >/dev/null
echo "→ status.state=$(jget 'd["status"]["state"]')  Ready=$(jget 'd["status"]["conditions"][0]["status"]') ($(jget 'd["status"]["conditions"][0]["message"]'))"

step 10 "KV Cleared 이벤트" "KV Cleared event"
call GET "/dynamo/v1/kv-events?since=$KV1"

step 11 "장애 중에도 호출 성공 (라우팅 회피)" "Calls still succeed (routed around)"
call POST /v1/chat/completions "$(chat 'Still there?')" >/dev/null
echo "→ HTTP 200, usage $(jget 'd["usage"]["total_tokens"]') tokens"

step 12 "전력 상한 100 kW 주입 → TPOT 상승" "Inject 100 kW power cap → TPOT rises"
call GET "/dynamo/v1/stats?deployment=$DGD" >/dev/null
T0=$(jget 'd["deployments"][0]["expected"]["tpot_ms"]')
call POST "/emulator/v1/obs/racks/$RACK/control" '{"action":"power_cap","params":{"cap_kw":100}}' >/dev/null
call GET "/dynamo/v1/stats?deployment=$DGD" >/dev/null
echo "→ expected TPOT ${T0} ms → $(jget 'd["deployments"][0]["expected"]["tpot_ms"]') ms, clock $(jget 'd["deployments"][0]["power"][0]["clock"]'), tokens/s/MW $(jget 'd["deployments"][0]["tokens_per_s_per_mw"]')"
call GET "$API/$DGD" >/dev/null
echo "→ DGD status.components.VllmDecodeWorker.gpuPowerLimitWatts = $(jget 'd["status"]["components"]["VllmDecodeWorker"].get("gpuPowerLimitWatts")')"

step 13 "복구: 장애·상한 해제 → successful" "Recover: clear fault and cap → successful"
call POST /emulator/v1/gpu-faults/recover '{}'
call POST "/emulator/v1/obs/racks/$RACK/control" '{"action":"power_uncap"}' >/dev/null
call GET "$API/$DGD" >/dev/null
echo "→ status.state=$(jget 'd["status"]["state"]')"
call DELETE "$API/demo-kanana-agg" >/dev/null
echo
echo "done."
