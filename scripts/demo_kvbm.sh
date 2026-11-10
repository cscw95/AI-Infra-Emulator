#!/usr/bin/env bash
# F33 KV cache tiers demo (terminal): G1 HBM → G2 LPDDR5X → G3 NVMe → G4 VAST.
# Shrinks G1/G2 with the KVBM config (num_gpu_blocks_override,
# DYN_KVBM_CPU_CACHE_OVERRIDE_NUM_BLOCKS) so a few 2000-token prefixes walk
# down the tiers, then shows each re-request onboarding from G2/G3/G4 with
# usage.prompt_tokens_details.cached_tokens = 2000 and the onboard cost in TTFT.
# Same steps as NOCP app/demo/scenarios/f33_kvbm.py.
#
#   BASE=http://127.0.0.1:20151 scripts/demo_kvbm.sh
set -u
BASE=${BASE:-http://127.0.0.1:20151}
DEP=dgd-llama-70b
PY=${PY:-python3}
curl -s "$BASE/dynamo/v1/models" >/dev/null 2>&1 || { echo "AI-Infra not reachable at $BASE" >&2; exit 1; }

step() { echo; echo "── $1"; }
call() {  # call METHOD PATH [JSON]; the curl line goes to stderr, the body to stdout
  echo "\$ curl -s -X $1 '$BASE$2'${3:+ -H 'Content-Type: application/json' -d '$3'}" >&2
  if [ -n "${3:-}" ]; then curl -s -X "$1" "$BASE$2" -H 'Content-Type: application/json' -d "$3"
  else curl -s -X "$1" "$BASE$2"; fi
}
read -r -d '' GEN_PY <<'EOF2'
import json, sys
r = json.load(sys.stdin)
u = r["usage"]["prompt_tokens_details"]["cached_tokens"]
print(f"  <- tier={r['kv_hit_tier']} cached_tokens={u} onboard_ms={r['kv_onboard_ms']} "
      f"prefill_ms={r['prefill_ms']} ttft_ms={r['ttft_ms']}")
EOF2
read -r -d '' TIERS_PY <<'EOF2'
import json, sys
d = json.load(sys.stdin)["deployments"][0]
for t in d["tiers"]:
    print(f"  {t['tier']} {t['medium']:<9} {t['storage_tier']:<11} entries={t['entries']} "
          f"used={t['used_blocks']}/{t['capacity_blocks']} blocks")
print(f"  cached_ratio={d['cached_ratio']} counters={d['counters']}")
EOF2
read -r -d '' EV_PY <<'EOF2'
import collections, json, sys
e = json.load(sys.stdin)["events"]
print("  ", dict(collections.Counter(x["router_event"]["storage_tier"] for x in e)))
EOF2
gen() {  # gen PREFIX → one line summary
  call POST /dynamo/v1/generate "{\"deployment\":\"$DEP\",\"prompt_tokens\":2000,\"max_tokens\":8,\"prefix_hash\":\"$1\"}" | $PY -c "$GEN_PY"
}
tiers() { call GET "/dynamo/v1/kv/tiers?deployment=$DEP" | $PY -c "$TIERS_PY"; }

step "0. reset: clear every tier, default (physical) capacities"
call POST /clear_kv_blocks >/dev/null; echo
call PUT /dynamo/v1/kv/tiers/config "{\"deployment\":\"$DEP\"}" >/dev/null; echo
tiers
step "1. small tiers for the demo: G1 = 150 blocks, G2 = 150 blocks (16 tokens/block)"
call PUT /dynamo/v1/kv/tiers/config "{\"deployment\":\"$DEP\",\"num_gpu_blocks_override\":150,\"DYN_KVBM_CPU_CACHE_OVERRIDE_NUM_BLOCKS\":150}"; echo
step "2. three 2000-token prefixes: p1 → G1 → G2 → G3 (G4 write-through)"
for p in p1 p2 p3; do gen "$p"; done
tiers
step "3. re-request p2 (in G2): onboard host → HBM"
gen p2
step "4. re-request p1 (in G3): onboard NVMe → HBM"
gen p1
step "5. disable G2/G3: re-request p3 from the shared VAST G4 view"
call PUT /dynamo/v1/kv/tiers/config "{\"deployment\":\"$DEP\",\"num_gpu_blocks_override\":150,\"DYN_KVBM_CPU_CACHE_OVERRIDE_NUM_BLOCKS\":0,\"DYN_KVBM_DISK_CACHE_OVERRIDE_NUM_BLOCKS\":0}" >/dev/null; echo
gen p3
step "6. kvbm_* metrics (prometheus_names.rs mod kvbm)"
curl -s "$BASE/dynamo/v1/metrics" | grep "^kvbm_.*$DEP" | head -12
step "7. KV router events with storage_tier"
call GET "/dynamo/v1/kv-events?deployment=$DEP&limit=500" | $PY -c "$EV_PY"
call PUT /dynamo/v1/kv/tiers/config "{\"deployment\":\"$DEP\"}" >/dev/null; echo
