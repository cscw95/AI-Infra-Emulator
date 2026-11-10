"""OpenAI-compatible Dynamo frontend (F01 engine side, upstream wire format).

What the Dynamo HTTP frontend serves in front of the workers, following
ai-dynamo/dynamo v1.6.2 @ 3a524d0d70f9 lib/llm/src/http/service/openai.rs and
lib/llm/src/protocols/openai/{chat_completions,completions,models}.rs, with
bodies shaped by openai/openai-openapi 2.0.0 @ c6522b4e0c48 (MIT):

  GET  /v1/models              ListModelsResponse {object:"list", data:[Model]}
  POST /v1/chat/completions    CreateChatCompletionRequest → CreateChatCompletionResponse
                               stream=true → chat.completion.chunk SSE: role delta,
                               content deltas, finish chunk, usage chunk (choices: [])
                               when stream_options.include_usage, then data: [DONE]
  POST /v1/completions         CreateCompletionRequest → text_completion (same SSE rules)
  POST /clear_kv_blocks        drop every worker's KV cache (Dynamo frontend admin
                               route, lib/llm/src/http/service/clear_kv_blocks.rs;
                               response body is NeoCloud's [unverified upstream shape])

Usage is CompletionUsage: cached_tokens under prompt_tokens_details,
reasoning_tokens under completion_tokens_details. Reasoning models stream
reasoning text in `reasoning_content` (Dynamo/vLLM reasoning parser field).
Errors use the OpenAI ErrorResponse body {error:{message,type,param,code}}.
x-request-id is honoured (idempotent replay, echoed back).

Tokens are counted, not tokenized: ceil(utf-8 bytes / 4) per message plus a
4-token chat-template overhead. The system/earlier turns form the KV prefix,
so a repeated system prompt hits the KV cache (see dynamo.KV_BLOCK_SIZE)."""
import hashlib
import json
import time
from typing import Any, List, Optional, Union

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .store import STORE
from . import dynamo as dyn

router = APIRouter(tags=["dynamo-frontend"])
CREATED = 1767225600            # 2026-01-01T00:00:00Z, fixed model "created"
TEMPLATE_TOKENS = 4


class ChatBody(BaseModel):
    model: str
    messages: List[dict]
    stream: bool = False
    stream_options: Optional[dict] = None
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    user: Optional[str] = None
    service_tier: Optional[str] = None


class CompletionBody(BaseModel):
    model: str
    prompt: Union[str, List[Any]]
    stream: bool = False
    stream_options: Optional[dict] = None
    max_tokens: Optional[int] = 16
    user: Optional[str] = None


def _text(content) -> str:
    if isinstance(content, list):          # content parts
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content if isinstance(content, str) else ""


def count_tokens(text: str) -> int:
    return -(-len(text.encode()) // 4)


def _msg_tokens(m: dict) -> int:
    return count_tokens(_text(m.get("content"))) + TEMPLATE_TOKENS


def _err(status: int, message: str, code: Optional[str] = None,
         param: Optional[str] = None) -> JSONResponse:
    etype = ("invalid_request_error" if status < 500 else
             "service_unavailable_error" if status == 503 else "server_error")
    return JSONResponse({"error": {"message": message, "type": etype,
                                   "param": param, "code": code}}, status_code=status)


def _from_http(e: HTTPException) -> JSONResponse:
    msg = str(e.detail)
    code = ("model_not_found" if e.status_code == 404 else
            "context_length_exceeded" if "context" in msg else
            "engine_unavailable" if e.status_code == 503 else None)
    return _err(e.status_code, msg, code)


def _engine(model: str, prompt: int, max_tokens: int, stream: bool,
            prefix: Optional[str], prefix_tokens: int, rid: Optional[str],
            user: Optional[str], endpoint: str) -> dict:
    body = dyn.GenerateBody(model=model, prompt_tokens=prompt, max_tokens=max_tokens,
                            stream=stream, prefix_hash=prefix,
                            prefix_tokens=prefix_tokens, request_id=rid, tenant_id=user)
    with STORE.lock:
        dyn.DYN.sync()
        prev = dyn.DYN.by_request.get(rid) if rid else None
        return {**prev, "replayed": True} if prev else dyn._run(body, endpoint)


def _split(res: dict) -> tuple:
    """Token texts → (reasoning part, answer part)."""
    toks = [dyn._token(res["id"], i) for i in range(res["completion_tokens"])]
    r = res["usage"]["completion_tokens_details"]["reasoning_tokens"]
    return toks[:r], toks[r:]


def _headers(res: dict) -> dict:
    return {"x-request-id": res["id"], "cache-control": "no-cache"}


def _sse(obj) -> str:
    return f"data: {json.dumps(obj, separators=(',', ':'))}\n\n"


def _pace(s: float):
    if dyn.PACE > 0:
        time.sleep(s * dyn.PACE)


def _max(b) -> int:
    v = getattr(b, "max_completion_tokens", None) or b.max_tokens
    return 256 if v is None else v


@router.get("/v1/models")
def list_models():
    with STORE.lock:
        dyn.DYN.sync()
        served = {d["model"] for d in dyn.DYN.deps.values()}
    return {"object": "list",
            "data": [{"id": m, "object": "model", "created": CREATED, "owned_by": "neocloud"}
                     for m in dyn.MODELS if m in served]}


@router.post("/v1/chat/completions")
def chat_completions(body: ChatBody, x_request_id: Optional[str] = Header(None)):
    if not body.messages:
        return _err(400, "messages must not be empty", param="messages")
    prompt = sum(_msg_tokens(m) for m in body.messages)
    head = body.messages[:-1]
    prefix = (hashlib.sha256(json.dumps(head, sort_keys=True).encode()).hexdigest()[:16]
              if head else None)
    try:
        res = _engine(body.model, prompt, _max(body), body.stream, prefix,
                      sum(_msg_tokens(m) for m in head), x_request_id, body.user,
                      "chat_completions")
    except HTTPException as e:
        return _from_http(e)
    cid, created = f"chatcmpl-{res['id']}", int(dyn._now())
    reasoning, answer = _split(res)
    base = {"id": cid, "object": "chat.completion.chunk", "created": created,
            "model": res["model"], "system_fingerprint": f"dynamo-{res['deployment']}"}
    if not body.stream:
        msg = {"role": "assistant", "content": "".join(answer)}
        if reasoning:
            msg["reasoning_content"] = "".join(reasoning)
        return JSONResponse({**base, "object": "chat.completion",
                             "choices": [{"index": 0, "message": msg, "logprobs": None,
                                          "finish_reason": res["finish_reason"]}],
                             "usage": res["usage"]}, headers=_headers(res))
    include_usage = bool((body.stream_options or {}).get("include_usage"))

    def gen():
        _pace(res["ttft_ms"] / 1000)
        yield _sse({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                                         "logprobs": None, "finish_reason": None}]})
        for key, toks in (("reasoning_content", reasoning), ("content", answer)):
            for t in toks:
                _pace(res["tpot_ms"] / 1000)
                yield _sse({**base, "choices": [{"index": 0, "delta": {key: t},
                                                 "logprobs": None, "finish_reason": None}]})
        yield _sse({**base, "choices": [{"index": 0, "delta": {}, "logprobs": None,
                                         "finish_reason": res["finish_reason"]}]})
        if include_usage:
            yield _sse({**base, "choices": [], "usage": res["usage"]})
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream", headers=_headers(res))


@router.post("/v1/completions")
def completions(body: CompletionBody, x_request_id: Optional[str] = Header(None)):
    if not isinstance(body.prompt, str):
        return _err(400, "only a single string prompt is supported", param="prompt")
    try:
        res = _engine(body.model, count_tokens(body.prompt), _max(body), body.stream,
                      None, 0, x_request_id, body.user, "completions")
    except HTTPException as e:
        return _from_http(e)
    base = {"id": f"cmpl-{res['id']}", "object": "text_completion",
            "created": int(dyn._now()), "model": res["model"],
            "system_fingerprint": f"dynamo-{res['deployment']}"}
    toks = sum(_split(res), [])
    if not body.stream:
        return JSONResponse({**base, "choices": [{"index": 0, "text": "".join(toks),
                                                  "logprobs": None,
                                                  "finish_reason": res["finish_reason"]}],
                             "usage": res["usage"]}, headers=_headers(res))
    include_usage = bool((body.stream_options or {}).get("include_usage"))

    def gen():
        for i, t in enumerate(toks):
            _pace((res["ttft_ms"] if i == 0 else res["tpot_ms"]) / 1000)
            last = i == len(toks) - 1
            yield _sse({**base, "choices": [{"index": 0, "text": t, "logprobs": None,
                                             "finish_reason": res["finish_reason"] if last else None}]})
        if include_usage:
            yield _sse({**base, "choices": [], "usage": res["usage"]})
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream", headers=_headers(res))


@router.post("/clear_kv_blocks")
def clear_kv_blocks():
    """Every KV-holding worker publishes Cleared and starts cold."""
    out = []
    with STORE.lock:
        dyn.DYN.sync()
        for d in dyn.DYN.deps.values():
            ws = [w["id"] for w in d["workers"] if w["role"] != "prefill"]
            for w in ws:
                dyn._kv_clear(d, w)
            d["kv"].clear()
            out.append({"deployment": d["id"], "workers": ws})
        nxt = dyn.DYN.kv_events[-1]["seq"] if dyn.DYN.kv_events else 0
    return {"cleared": out, "kv_events_next": nxt}
