"""Extract the upstream schemas the F01 contract tests check against.

Reads the spec copies under <neocloud>/ref/upstream (see UPSTREAM-REFS.md) and
writes small, self-contained JSON files to tests/contracts/ so the tests run
without the 4 MB OpenAI spec or the 3 MB DGD CRD:

  openai_schemas.json   openai/openai-openapi 2.0.0 @ c6522b4e0c48 (MIT),
                        components/schemas with $ref inlined, descriptions dropped
  dynamo_dgd_v1beta1.json  ai-dynamo/dynamo v1.6.2 @ 3a524d0d70f9 (Apache-2.0),
                        deploy/operator/config/crd/bases/nvidia.com_dynamographdeployments.yaml
                        v1beta1 openAPIV3Schema, pruned to the fields the emulator emits
  dynamo_metric_names.json  lib/runtime/src/metrics/prometheus_names.rs consts
                        (frontend_service → dynamo_frontend_*, router → dynamo_component_*)

Usage: python scripts/upstream/extract_contracts.py [<ref/upstream dir>]
"""
import json
import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "..", "tests", "contracts")
REF = (sys.argv[1] if len(sys.argv) > 1 else
       os.path.join(HERE, "..", "..", "..", "..", "..", "ref", "upstream"))

OPENAI = ["CompletionUsage", "CreateChatCompletionResponse",
          "CreateChatCompletionStreamResponse", "CreateCompletionResponse",
          "ListModelsResponse", "Model", "ErrorResponse"]
DROP = {"description", "example", "examples", "title", "x-oaiMeta", "x-oaiTypeLabel",
        "x-stainless-const", "deprecated", "default", "x-oaiExpandable"}


def _strip(node, schemas=None, seen=()):
    if isinstance(node, list):
        return [_strip(n, schemas, seen) for n in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node and schemas is not None:
        name = node["$ref"].rsplit("/", 1)[-1]
        if name in seen:                      # recursive type: accept any object
            return {}
        return _strip(schemas[name], schemas, seen + (name,))
    return {k: _strip(v, schemas, seen) for k, v in node.items()
            if k not in DROP and not k.startswith("x-kubernetes")}


def openai():
    spec = yaml.safe_load(open(os.path.join(REF, "openai", "openapi.yaml")))
    schemas = spec["components"]["schemas"]
    return {"source": "openai/openai-openapi 2.0.0 @ c6522b4e0c48 openapi.yaml (MIT)",
            "schemas": {n: _strip(schemas[n], schemas, (n,)) for n in OPENAI}}


def _prune(node: dict, keep: dict) -> dict:
    """Keep only `keep` properties (value True = whole subtree, dict = recurse,
    None = shallow object)."""
    out = {k: v for k, v in node.items() if k != "properties"}
    props = node.get("properties", {})
    out["properties"] = {}
    for k, sub in keep.items():
        if k not in props:
            continue
        p = props[k]
        if sub is True:
            out["properties"][k] = p
        elif sub is None:
            out["properties"][k] = {"type": p.get("type", "object")}
        else:
            if p.get("type") == "array":
                out["properties"][k] = {**p, "items": _prune(p["items"], sub)}
            elif "additionalProperties" in p:
                out["properties"][k] = {**p, "additionalProperties":
                                        _prune(p["additionalProperties"], sub)}
            else:
                out["properties"][k] = _prune(p, sub)
    if "required" in out:
        out["required"] = [r for r in out["required"] if r in out["properties"]]
    return out


def dgd():
    crd = yaml.safe_load(open(os.path.join(
        REF, "dynamo", "deploy", "operator", "config", "crd", "bases",
        "nvidia.com_dynamographdeployments.yaml")))
    v = next(x for x in crd["spec"]["versions"] if x["name"] == "v1beta1")
    s = v["schema"]["openAPIV3Schema"]
    comp = {"name": True, "type": True, "replicas": True, "modelRef": True,
            "podTemplate": None}
    status_comp = {k: True for k in (
        "componentKind", "componentNames", "replicas", "updatedReplicas",
        "readyReplicas", "availableReplicas", "gpusPerEngine", "gpusPerReplica",
        "gpuPowerLimitWatts", "servedModelName")}
    pruned = _prune(s, {
        "apiVersion": True, "kind": True, "metadata": None,
        "spec": {"backendFramework": True, "annotations": True, "labels": True,
                 "components": comp},
        "status": {"state": True, "observedGeneration": True, "conditions": True,
                   "components": status_comp}})
    return {"source": "ai-dynamo/dynamo v1.6.2 @ 3a524d0d70f9 nvidia.com_dynamographdeployments.yaml "
                      "v1beta1 (Apache-2.0), pruned",
            "group": crd["spec"]["group"], "kind": crd["spec"]["names"]["kind"],
            "plural": crd["spec"]["names"]["plural"], "version": "v1beta1",
            "schema": _strip(pruned)}


def metric_names():
    src = open(os.path.join(REF, "dynamo", "lib", "runtime", "src", "metrics",
                            "prometheus_names.rs")).read()

    def mod(name):
        m = re.search(r"pub mod %s \{(.*?)\n\}" % name, src, re.S)
        return re.findall(r'pub const \w+: &str =\s*"([a-z0-9_]+)"', m.group(1))
    names = (["dynamo_frontend_" + n for n in mod("frontend_service")] +
             ["dynamo_component_" + n for n in mod("router")])
    return {"source": "ai-dynamo/dynamo v1.6.2 @ 3a524d0d70f9 "
                      "lib/runtime/src/metrics/prometheus_names.rs (Apache-2.0)",
            "names": sorted(set(names))}


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for fn, data in (("openai_schemas.json", openai()),
                     ("dynamo_dgd_v1beta1.json", dgd()),
                     ("dynamo_metric_names.json", metric_names())):
        with open(os.path.join(OUT, fn), "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        print(fn, os.path.getsize(os.path.join(OUT, fn)), "bytes")
