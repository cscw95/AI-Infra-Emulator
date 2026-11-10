# Upstream contract schemas (F01 Dynamo)

Extracted by `scripts/upstream/extract_contracts.py` from the spec copies in
`<neocloud>/ref/upstream/` (see `ref/upstream/UPSTREAM-REFS.md`). Descriptions
are dropped and `$ref`s inlined; validation keywords are kept.

| File | Source | License |
|------|--------|---------|
| `openai_schemas.json` | openai/openai-openapi 2.0.0 @ `c6522b4e0c48` `openapi.yaml` (CompletionUsage, CreateChatCompletionResponse, CreateChatCompletionStreamResponse, CreateCompletionResponse, ListModelsResponse, Model, ErrorResponse) | MIT |
| `dynamo_dgd_v1beta1.json` | ai-dynamo/dynamo v1.6.2 @ `3a524d0d70f9` `deploy/operator/config/crd/bases/nvidia.com_dynamographdeployments.yaml`, v1beta1, pruned to emitted fields | Apache-2.0 |
| `dynamo_metric_names.json` | same repo, `lib/runtime/src/metrics/prometheus_names.rs` (frontend_service → `dynamo_frontend_*`, router → `dynamo_component_*`) | Apache-2.0 |
| `dynamo_kv_event.json` | same repo, `lib/kv-router/src/protocols.rs` RouterEvent / KvCacheEvent, hand-transcribed to JSON Schema | Apache-2.0 |

`schema_check.py` is a small validator (no extra dependency).
