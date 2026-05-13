"""
GitHub Copilot → Anthropic API Proxy

Exposes GitHub Copilot as a standard Anthropic API, enabling tools like Claude Code to use it.

Key findings:
  - Claude models support direct passthrough via /v1/messages (no format conversion needed)
  - Model name format: claude-opus-4-6 → claude-opus-4.6 (hyphen → dot)
  - API base is read from endpoints.api in api-key.json (may be an enterprise domain)

Authentication flow:
  1. GitHub OAuth Device Flow → access_token
  2. access_token → Copilot API key (https://api.github.com/copilot_internal/v2/token)
  3. Copilot API key + specific headers → request GitHub Copilot API
"""

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

# ============================================================
# CLI Arguments (parsed early so all modules can read them)
# ============================================================

_parser = argparse.ArgumentParser(
    description="GitHub Copilot → Anthropic API Proxy (cp4cc)",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog="""
modes:
  default     listen on 127.0.0.1 (local only), UI + audit enabled
  --share     listen on 0.0.0.0   (LAN accessible)
  --fast      listen on 127.0.0.1, UI and audit endpoints disabled
""",
)
_parser.add_argument(
    "--share",
    action="store_true",
    default=False,
    help="bind to 0.0.0.0 so others on the LAN can use the proxy",
)
_parser.add_argument(
    "--fast",
    action="store_true",
    default=False,
    help="disable UI and audit endpoints for lower overhead",
)
_parser.add_argument(
    "--port",
    type=int,
    default=8082,
    help="port to listen on (default: 8082)",
)
ARGS = _parser.parse_args()

# ============================================================
# Constants
# ============================================================

GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"
GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API_KEY_URL = "https://api.github.com/copilot_internal/v2/token"
GITHUB_COPILOT_API_BASE = "https://api.githubcopilot.com"
COPILOT_VERSION = "0.26.7"

TOKEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".github_copilot_token")
ACCESS_TOKEN_FILE = os.path.join(TOKEN_DIR, "access-token")
API_KEY_FILE = os.path.join(TOKEN_DIR, "api-key.json")

# ============================================================
# Session & Directory Initialization
# ============================================================

SESSION_ID = str(uuid4())
SESSION_START = datetime.now(timezone.utc)

LOGS_DIR = Path("logs")
AUDIT_DIR = LOGS_DIR / "audit"
AUDIT_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# Logging System
# ============================================================

LOG_FILE = LOGS_DIR / "app.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("copilot-proxy")

# ============================================================
# Audit Log System (one JSON file per session)
# ============================================================

AUDIT_FILE = AUDIT_DIR / f"session_{SESSION_START.strftime('%Y%m%d_%H%M%S')}_{SESSION_ID[:8]}.json"

_audit_data: dict = {
    "session_id": SESSION_ID,
    "started_at": SESSION_START.isoformat(),
    "requests": [],
}


def _write_audit() -> None:
    if ARGS.fast:
        return
    with open(AUDIT_FILE, "w", encoding="utf-8") as f:
        json.dump(_audit_data, f, indent=2, ensure_ascii=False)


def audit_log(
    req_id: str,
    request_body: dict,
    copilot_model: str,
    endpoint: str,
    response_body: dict | str | None,
    status_code: int,
    duration_ms: float,
    error: str | None = None,
) -> None:
    if ARGS.fast:
        logger.info(
            "request id=%s model=%s→%s endpoint=%s status=%s duration=%.0fms%s",
            req_id[:8], request_body.get("model",""), copilot_model, endpoint,
            status_code, duration_ms, f" ERROR={error}" if error else "",
        )
        return
    messages = request_body.get("messages", [])

    # Per-message type breakdown: classify each message
    msg_summaries = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if isinstance(content, list):
            types_in_content = [b.get("type", "") for b in content]
            if "tool_use" in types_in_content:
                kind = "tool_use"
                parts = []
                for b in content:
                    if b.get("type") == "tool_use":
                        inp = json.dumps(b.get("input", {}), ensure_ascii=False)
                        parts.append(f"[tool: {b.get('name','')}]\n{inp[:800]}")
                body_text = "\n\n".join(parts)
            elif "tool_result" in types_in_content:
                kind = "tool_result"
                parts = []
                for b in content:
                    if b.get("type") == "tool_result":
                        rc = b.get("content", "")
                        if isinstance(rc, list):
                            rc = " ".join(x.get("text","") for x in rc if x.get("type")=="text")
                        parts.append(f"[tool_result id={b.get('tool_use_id','')}]\n{str(rc)[:800]}")
                body_text = "\n\n".join(parts)
            else:
                kind = "message"
                body_text = " ".join(
                    b.get("text", "") for b in content if b.get("type") == "text"
                )[:1000]
        else:
            kind = "message"
            body_text = str(content)[:1000]

        # Short preview for the list column (first non-empty line, max 80 chars)
        preview = next((ln.strip() for ln in body_text.splitlines() if ln.strip()), "")[:80]
        msg_summaries.append({"role": role, "kind": kind, "preview": preview, "body": body_text})

    entry = {
        "id": req_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "original_model": request_body.get("model", ""),
        "copilot_model": copilot_model,
        "endpoint": endpoint,
        "stream": request_body.get("stream", False),
        "messages_count": len(messages),
        "messages": msg_summaries,
        "request_preview": {
            "model": request_body.get("model"),
            "system": (request_body.get("system") or "")[:500],
            "last_user_msg": next(
                (m["content"][:200] if isinstance(m["content"], str) else str(m["content"])[:200]
                 for m in reversed(messages)
                 if m.get("role") == "user"),
                "",
            ),
        },
        "response": {
            "status_code": status_code,
            "body": response_body if isinstance(response_body, dict) else str(response_body)[:500] if response_body else None,
        },
        "duration_ms": round(duration_ms, 1),
        "error": error,
    }
    _audit_data["requests"].append(entry)
    _write_audit()
    logger.info(
        "request id=%s model=%s→%s endpoint=%s status=%s duration=%.0fms%s",
        req_id[:8], entry["original_model"], copilot_model, endpoint,
        status_code, duration_ms, f" ERROR={error}" if error else "",
    )


# ============================================================
# GitHub Copilot Authentication
# ============================================================

def _ensure_token_dir() -> None:
    os.makedirs(TOKEN_DIR, exist_ok=True)


def _get_github_request_headers(access_token: str | None = None) -> dict:
    headers = {
        "accept": "application/json",
        "editor-version": "vscode/1.85.1",
        "editor-plugin-version": "copilot/1.155.0",
        "user-agent": "GithubCopilot/1.155.0",
        "accept-encoding": "gzip,deflate,br",
    }
    if access_token:
        headers["authorization"] = f"token {access_token}"
    return headers


def _device_flow_login() -> str:
    """Obtain access_token via GitHub OAuth Device Flow"""
    client = httpx.Client()
    resp = client.post(
        GITHUB_DEVICE_CODE_URL,
        headers=_get_github_request_headers(),
        json={"client_id": GITHUB_CLIENT_ID, "scope": "read:user"},
    )
    resp.raise_for_status()
    data = resp.json()
    device_code = data["device_code"]
    user_code = data["user_code"]
    verification_uri = data["verification_uri"]

    print("\n" + "=" * 50, flush=True)
    print(f"  Visit: {verification_uri}", flush=True)
    print(f"  Auth code: >>>  {user_code}  <<<", flush=True)
    print("=" * 50, flush=True)
    print("Polling started, please complete authorization in your browser...\n", flush=True)
    logger.info("Device Flow started, waiting for user authorization code=%s", user_code)

    for attempt in range(36):
        time.sleep(5)
        resp = client.post(
            GITHUB_ACCESS_TOKEN_URL,
            headers=_get_github_request_headers(),
            json={
                "client_id": GITHUB_CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        result = resp.json()
        if "access_token" in result:
            logger.info("GitHub OAuth authentication successful")
            return result["access_token"]
        elif result.get("error") == "authorization_pending":
            remaining = (36 - attempt - 1) * 5
            if attempt % 6 == 0:
                print(f"  [{remaining}s remaining] Waiting for authorization... code: {user_code}  |  {verification_uri}", flush=True)
            else:
                print(f"  [{remaining}s remaining] Waiting for authorization...", flush=True)
        else:
            logger.warning("Device Flow unexpected response: %s", result)

    raise RuntimeError("Timed out waiting for user authorization (180 seconds)")


def get_access_token() -> str:
    _ensure_token_dir()
    try:
        with open(ACCESS_TOKEN_FILE) as f:
            token = f.read().strip()
            if token:
                return token
    except IOError:
        pass
    token = _device_flow_login()
    with open(ACCESS_TOKEN_FILE, "w") as f:
        f.write(token)
    return token


def get_api_key() -> str:
    """Get Copilot API key, auto-refresh on expiry"""
    _ensure_token_dir()
    try:
        with open(API_KEY_FILE) as f:
            info = json.load(f)
            if info.get("expires_at", 0) > datetime.now().timestamp():
                return info["token"]
    except (IOError, json.JSONDecodeError, KeyError):
        pass

    access_token = get_access_token()
    headers = _get_github_request_headers(access_token)
    client = httpx.Client()
    resp = client.get(GITHUB_API_KEY_URL, headers=headers)

    if resp.status_code == 401:
        logger.warning("access_token has expired, re-authenticating")
        try:
            os.remove(ACCESS_TOKEN_FILE)
        except OSError:
            pass
        access_token = get_access_token()
        headers = _get_github_request_headers(access_token)
        resp = client.get(GITHUB_API_KEY_URL, headers=headers)

    resp.raise_for_status()
    info = resp.json()
    with open(API_KEY_FILE, "w") as f:
        json.dump(info, f)
    logger.info("Copilot API key refreshed, expires_at=%s", info.get("expires_at"))
    return info["token"]


def get_api_base() -> str:
    try:
        with open(API_KEY_FILE) as f:
            info = json.load(f)
            return info.get("endpoints", {}).get("api", GITHUB_COPILOT_API_BASE)
    except (IOError, json.JSONDecodeError):
        return GITHUB_COPILOT_API_BASE


def get_copilot_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "content-type": "application/json",
        "copilot-integration-id": "vscode-chat",
        "editor-version": "vscode/1.95.0",
        "editor-plugin-version": f"copilot-chat/{COPILOT_VERSION}",
        "user-agent": f"GitHubCopilotChat/{COPILOT_VERSION}",
        "openai-intent": "conversation-panel",
        "x-github-api-version": "2025-04-01",
        "x-request-id": str(uuid4()),
        "x-vscode-user-agent-library-version": "electron-fetch",
        "X-Initiator": "user",
    }


# ============================================================
# Model List Cache
# ============================================================

_models_cache: list = []
_models_cache_time: float = 0.0


def get_models(force: bool = False) -> list:
    global _models_cache, _models_cache_time
    if not force and _models_cache and time.time() - _models_cache_time < 300:
        return _models_cache
    try:
        api_key = get_api_key()
        api_base = get_api_base()
        headers = get_copilot_headers(api_key)
        resp = httpx.get(f"{api_base}/models", headers=headers, timeout=10)
        if resp.status_code == 200:
            _models_cache = resp.json().get("data", [])
            _models_cache_time = time.time()
            logger.info("Model list refreshed, total %d models", len(_models_cache))
    except Exception as e:
        logger.error("Failed to fetch model list: %s", e)
    return _models_cache


def get_model_info(model_id: str) -> dict | None:
    for m in get_models():
        if m["id"] == model_id:
            return m
    return None


def map_model_name(model: str) -> str:
    """
    Convert Claude Code model name to GitHub Copilot model name format
    claude-opus-4-6          → claude-opus-4.6
    claude-opus-4-6-20250514 → claude-opus-4.6  (strip date suffix)
    claude-haiku-4-5         → claude-haiku-4.5
    gpt-4o                   → gpt-4o (unchanged)
    """
    original = model
    model = re.sub(r"-\d{8}$", "", model)         # strip YYYYMMDD date suffix
    model = re.sub(r"(\d)-(\d+)$", r"\1.\2", model)  # 4-6 → 4.6
    if model != original:
        logger.debug("Model name mapped: %s → %s", original, model)
    return model


# ============================================================
# Format Conversion (only for non-Claude models via /chat/completions)
# ============================================================

def anthropic_to_openai(body: dict, mapped_model: str) -> dict:
    """Anthropic /v1/messages format → OpenAI /chat/completions format"""
    messages = []

    if system := body.get("system"):
        if isinstance(system, str):
            messages.append({"role": "system", "content": system})
        elif isinstance(system, list):
            text = " ".join(
                b.get("text", "") for b in system if b.get("type") == "text"
            )
            messages.append({"role": "system", "content": text})

    for msg in body.get("messages", []):
        role = msg["role"]
        content = msg["content"]
        if isinstance(content, list):
            content = "".join(
                b.get("text", "") for b in content if b.get("type") == "text"
            )
        messages.append({"role": role, "content": content})

    result: dict = {"model": mapped_model, "messages": messages}

    # Claude Code speaks Anthropic and sends max_tokens. Newer Copilot GPT/o-series
    # chat-completions models reject max_tokens and require max_completion_tokens.
    # Keep Claude models on /v1/messages untouched; this conversion is only for
    # non-Claude models routed to /chat/completions.
    if "max_tokens" in body:
        result["max_completion_tokens"] = body["max_tokens"]

    for key in ("stream", "temperature", "top_p", "stop"):
        if key in body:
            result[key] = body[key]
    # top_k is Anthropic-specific, do not pass to OpenAI
    return result


def _responses_content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                typ = item.get("type")
                if typ in ("input_text", "output_text", "text"):
                    parts.append(item.get("text", ""))
        return "".join(parts)
    return ""


def responses_to_openai_chat(body: dict, mapped_model: str) -> dict:
    messages = []

    if instructions := body.get("instructions"):
        messages.append({"role": "system", "content": str(instructions)})

    input_value = body.get("input", "")
    if isinstance(input_value, str):
        messages.append({"role": "user", "content": input_value})
    elif isinstance(input_value, list):
        for item in input_value:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                typ = item.get("type")
                if typ == "message" or "role" in item:
                    role = item.get("role", "user")
                    if role not in ("system", "user", "assistant", "tool"):
                        role = "user"
                    messages.append({
                        "role": role,
                        "content": _responses_content_to_text(item.get("content", "")),
                    })
                elif typ in ("input_text", "text"):
                    messages.append({"role": "user", "content": item.get("text", "")})
                elif typ == "function_call_output":
                    messages.append({
                        "role": "tool",
                        "tool_call_id": item.get("call_id", "call_0"),
                        "content": _responses_content_to_text(item.get("output", "")),
                    })

    result: dict = {"model": mapped_model, "messages": messages}
    if "max_output_tokens" in body:
        result["max_completion_tokens"] = body["max_output_tokens"]
    if "max_completion_tokens" in body:
        result["max_completion_tokens"] = body["max_completion_tokens"]
    for key in ("stream", "temperature", "top_p", "stop"):
        if key in body:
            result[key] = body[key]
    if "tools" in body:
        result["tools"] = body["tools"]
    if "tool_choice" in body:
        result["tool_choice"] = body["tool_choice"]
    return result


def _responses_usage(openai_usage: dict) -> dict:
    input_tokens = openai_usage.get("prompt_tokens", openai_usage.get("input_tokens", 0)) or 0
    output_tokens = openai_usage.get("completion_tokens", openai_usage.get("output_tokens", 0)) or 0
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": openai_usage.get("total_tokens", input_tokens + output_tokens) or 0,
    }


def openai_chat_to_responses(openai_resp: dict, response_id: str | None = None) -> dict:
    choice = openai_resp.get("choices", [{}])[0]
    message = choice.get("message", {}) or {}
    text = message.get("content") or ""
    rid = response_id or openai_resp.get("id") or f"resp_{uuid4().hex[:24]}"
    output_item = {
        "id": f"msg_{uuid4().hex[:24]}",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }
    return {
        "id": rid,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "error": None,
        "model": openai_resp.get("model", "unknown"),
        "output": [output_item],
        "output_text": text,
        "usage": _responses_usage(openai_resp.get("usage", {})),
    }


async def stream_openai_to_responses(openai_stream: httpx.Response, response_id: str, model: str) -> AsyncIterator[str]:
    yield f"event: response.created\ndata: {json.dumps({'type':'response.created','response':{'id':response_id,'model':model,'status':'in_progress'}})}\n\n"
    item_id = f"msg_{uuid4().hex[:24]}"
    yield f"event: response.output_item.added\ndata: {json.dumps({'type':'response.output_item.added','output_index':0,'item':{'id':item_id,'type':'message','status':'in_progress','role':'assistant','content':[]}})}\n\n"
    text_parts = []
    usage = {}
    async for line in openai_stream.aiter_lines():
        if not line.startswith("data: "):
            continue
        data_str = line[6:]
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        if chunk.get("usage"):
            usage = chunk["usage"]
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta", {}) or {}
        text = delta.get("content") or ""
        if text:
            text_parts.append(text)
            yield f"event: response.output_text.delta\ndata: {json.dumps({'type':'response.output_text.delta','delta':text})}\n\n"
    full_text = "".join(text_parts)
    yield f"event: response.output_item.done\ndata: {json.dumps({'type':'response.output_item.done','output_index':0,'item':{'id':item_id,'type':'message','status':'completed','role':'assistant','content':[{'type':'output_text','text':full_text}]}})}\n\n"
    completed = {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "model": model,
            "status": "completed",
            "output": [{"id": item_id, "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": full_text}]}],
            "output_text": full_text,
            "usage": _responses_usage(usage),
        },
    }
    yield f"event: response.completed\ndata: {json.dumps(completed)}\n\n"


def openai_to_anthropic(openai_resp: dict) -> dict:
    """OpenAI /chat/completions response → Anthropic /v1/messages format"""
    choice = openai_resp["choices"][0]
    message = choice["message"]
    usage = openai_resp.get("usage", {})
    finish_map = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}
    return {
        "id": openai_resp.get("id", f"msg_{uuid4().hex[:24]}"),
        "type": "message",
        "role": "assistant",
        "model": openai_resp.get("model", "unknown"),
        "content": [{"type": "text", "text": message.get("content") or ""}],
        "stop_reason": finish_map.get(choice.get("finish_reason", "stop"), "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


async def stream_openai_to_anthropic(
    openai_stream: httpx.Response, msg_id: str, model: str
) -> AsyncIterator[str]:
    """OpenAI SSE → Anthropic SSE format conversion (for non-Claude models)"""
    yield f"event: message_start\ndata: {json.dumps({'type':'message_start','message':{'id':msg_id,'type':'message','role':'assistant','model':model,'content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':0,'output_tokens':0}}})}\n\n"
    yield f"event: content_block_start\ndata: {json.dumps({'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}})}\n\n"
    yield 'event: ping\ndata: {"type": "ping"}\n\n'

    finish_reason = "end_turn"
    output_tokens = 0
    finish_map = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}

    async for line in openai_stream.aiter_lines():
        if not line.startswith("data: "):
            continue
        data_str = line[6:]
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        choice = chunk.get("choices", [{}])[0]
        delta = choice.get("delta", {})
        text = delta.get("content") or ""
        if text:
            output_tokens += 1
            yield f"event: content_block_delta\ndata: {json.dumps({'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':text}})}\n\n"
        if fr := choice.get("finish_reason"):
            finish_reason = finish_map.get(fr, "end_turn")

    yield f"event: content_block_stop\ndata: {json.dumps({'type':'content_block_stop','index':0})}\n\n"
    yield f"event: message_delta\ndata: {json.dumps({'type':'message_delta','delta':{'stop_reason':finish_reason,'stop_sequence':None},'usage':{'output_tokens':output_tokens}})}\n\n"
    yield f"event: message_stop\ndata: {json.dumps({'type':'message_stop'})}\n\n"


# ============================================================
# FastAPI Application
# ============================================================

app = FastAPI(title="GitHub Copilot → Anthropic API Proxy", docs_url=None, redoc_url=None)


@app.get("/health")
def health():
    return {"status": "ok", "session_id": SESSION_ID}


@app.get("/v1/models")
async def list_models():
    """Return model list in Anthropic format"""
    models = get_models()
    return {
        "data": [
            {
                "id": m["id"],
                "display_name": m.get("name", m["id"]),
                "created_at": SESSION_START.isoformat(),
                "object": "model",
            }
            for m in models
        ]
    }


@app.post("/v1/responses")
async def responses(request: Request):
    req_id = str(uuid4())
    t_start = time.monotonic()
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        logger.warning("Invalid Responses JSON request req=%s: %s", req_id[:8], e)
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    original_model: str = body.get("model", "")
    copilot_model = map_model_name(original_model)
    forward_body = {**body, "model": copilot_model}

    try:
        api_key = get_api_key()
    except Exception as e:
        logger.error("Authentication failed req=%s: %s", req_id[:8], e)
        audit_log(req_id, body, copilot_model, "responses-auth", None, 401, 0, str(e))
        raise HTTPException(status_code=401, detail=f"GitHub Copilot authentication failed: {e}")

    api_base = get_api_base()
    copilot_headers = get_copilot_headers(api_key)
    endpoint = "/v1/responses"
    is_stream = forward_body.get("stream", False)
    url = f"{api_base}{endpoint}"

    logger.debug(
        "responses req=%s model=%s→%s endpoint=%s stream=%s",
        req_id[:8], original_model, copilot_model, endpoint, is_stream,
    )

    if is_stream:
        async def generate():
            nonlocal t_start
            error_msg = None
            status = 200
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    async with client.stream("POST", url, headers=copilot_headers, json=forward_body) as resp:
                        status = resp.status_code
                        if status != 200:
                            err = await resp.aread()
                            error_msg = err.decode()
                            logger.warning("Upstream %s returned %s: %s", endpoint, status, error_msg[:200])
                            yield f"event: error\ndata: {json.dumps({'type':'error','error':{'message':error_msg}})}\n\n"
                        else:
                            async for line in resp.aiter_lines():
                                if line:
                                    yield line + "\n"
                                else:
                                    yield "\n"
            except Exception as e:
                error_msg = str(e)
                logger.error("Responses streaming request error req=%s: %s", req_id[:8], e)
            duration = (time.monotonic() - t_start) * 1000
            audit_log(req_id, body, copilot_model, "/v1/responses", {"stream": True}, status, duration, error_msg)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, headers=copilot_headers, json=forward_body)
    except Exception as e:
        duration = (time.monotonic() - t_start) * 1000
        audit_log(req_id, body, copilot_model, "/v1/responses", None, 500, duration, str(e))
        raise HTTPException(status_code=500, detail=str(e))

    duration = (time.monotonic() - t_start) * 1000
    if resp.status_code != 200:
        logger.warning("Upstream %s returned %s: %s", endpoint, resp.status_code, resp.text[:300])
        audit_log(req_id, body, copilot_model, "/v1/responses", resp.text[:500], resp.status_code, duration, f"upstream {resp.status_code}")
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    result = resp.json()
    audit_log(req_id, body, copilot_model, "/v1/responses", result, 200, duration)
    return JSONResponse(content=result)


@app.post("/v1/messages")
async def messages(request: Request):
    """
    Anthropic /v1/messages compatible endpoint

    Routing strategy:
    - Claude models → direct passthrough to {api_base}/v1/messages (no format conversion)
    - Other models  → convert to OpenAI format, send to {api_base}/chat/completions
    """
    req_id = str(uuid4())
    t_start = time.monotonic()
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        logger.warning("Invalid JSON request req=%s: %s", req_id[:8], e)
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid JSON body. If you are using Windows cmd.exe, do not wrap "
                "JSON with single quotes; use escaped double quotes or --data-binary @body.json."
            ),
        )

    original_model: str = body.get("model", "")
    copilot_model = map_model_name(original_model)

    try:
        api_key = get_api_key()
    except Exception as e:
        logger.error("Authentication failed req=%s: %s", req_id[:8], e)
        audit_log(req_id, body, copilot_model, "auth", None, 401, 0, str(e))
        raise HTTPException(status_code=401, detail=f"GitHub Copilot authentication failed: {e}")

    api_base = get_api_base()
    copilot_headers = get_copilot_headers(api_key)

    # Determine which endpoint to use
    model_info = get_model_info(copilot_model)
    supported = model_info.get("supported_endpoints", []) if model_info else []
    use_messages_api = "/v1/messages" in supported

    if use_messages_api:
        # ── Claude models: direct passthrough /v1/messages ──────────────────
        endpoint = "/v1/messages"
        # Update model in body to Copilot format
        forward_body = {**body, "model": copilot_model}
        # Remove fields sent by Anthropic/Claude Code that Copilot does not support
        forward_body.pop("betas", None)
        forward_body.pop("context_management", None)
        forward_body.pop("output_config", None)
    else:
        # ── Non-Claude models: convert to OpenAI format ─────────────────
        endpoint = "/chat/completions"
        forward_body = anthropic_to_openai(body, copilot_model)

    is_stream = forward_body.get("stream", False)
    logger.debug(
        "req=%s model=%s→%s endpoint=%s stream=%s",
        req_id[:8], original_model, copilot_model, endpoint, is_stream,
    )

    url = f"{api_base}{endpoint}"

    if is_stream:
        # ── Streaming response ────────────────────────────────────────────
        async def generate():
            nonlocal t_start
            error_msg = None
            collected_text = []
            status = 200
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    async with client.stream("POST", url, headers=copilot_headers, json=forward_body) as resp:
                        status = resp.status_code
                        if status != 200:
                            err = await resp.aread()
                            error_msg = err.decode()
                            logger.warning("Upstream %s returned %s: %s", endpoint, status, error_msg[:200])
                            yield f"event: error\ndata: {json.dumps({'type':'error','error':{'type':'api_error','message':error_msg}})}\n\n"
                        elif use_messages_api:
                            # Claude model: direct SSE passthrough
                            async for line in resp.aiter_lines():
                                if line:
                                    yield line + "\n"
                                    if line.startswith("data:"):
                                        try:
                                            d = json.loads(line[5:])
                                            if d.get("type") == "content_block_delta":
                                                collected_text.append(d.get("delta", {}).get("text", ""))
                                        except Exception:
                                            pass
                                else:
                                    yield "\n"
                        else:
                            # Non-Claude: OpenAI SSE → Anthropic SSE conversion
                            msg_id = f"msg_{uuid4().hex[:24]}"
                            async for chunk in stream_openai_to_anthropic(resp, msg_id, original_model):
                                yield chunk
                                if '"text_delta"' in chunk:
                                    try:
                                        d = json.loads(chunk.split("data: ", 1)[1])
                                        collected_text.append(d.get("delta", {}).get("text", ""))
                                    except Exception:
                                        pass
            except Exception as e:
                error_msg = str(e)
                logger.error("Streaming request error req=%s: %s", req_id[:8], e)

            duration = (time.monotonic() - t_start) * 1000
            audit_log(req_id, body, copilot_model, endpoint,
                      {"streamed_text": "".join(collected_text)[:2000]},
                      status, duration, error_msg)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    else:
        # ── Non-streaming response ───────────────────────────────────────────
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(url, headers=copilot_headers, json=forward_body)
        except Exception as e:
            duration = (time.monotonic() - t_start) * 1000
            audit_log(req_id, body, copilot_model, endpoint, None, 500, duration, str(e))
            raise HTTPException(status_code=500, detail=str(e))

        duration = (time.monotonic() - t_start) * 1000

        if resp.status_code != 200:
            logger.warning("Upstream %s returned %s: %s", endpoint, resp.status_code, resp.text[:300])
            audit_log(req_id, body, copilot_model, endpoint,
                      resp.text[:500], resp.status_code, duration,
                      f"upstream {resp.status_code}")
            raise HTTPException(status_code=resp.status_code, detail=resp.text)

        resp_json = resp.json()
        if use_messages_api:
            # Return directly (already in Anthropic format)
            result = resp_json
        else:
            result = openai_to_anthropic(resp_json)

        audit_log(req_id, body, copilot_model, endpoint, result, 200, duration)
        return JSONResponse(content=result)


# ============================================================
# Admin Endpoints
# ============================================================

@app.get("/v1/models/refresh")
async def refresh_models():
    """Force refresh model list"""
    models = get_models(force=True)
    return {"count": len(models), "models": [m["id"] for m in models]}


if not ARGS.fast:
    @app.get("/audit/sessions")
    def audit_sessions():
        """List all audit session files"""
        files = sorted(AUDIT_DIR.glob("session_*.json"), reverse=True)
        result = []
        for f in files[:20]:
            try:
                data = json.loads(f.read_text())
                result.append({
                    "file": f.name,
                    "session_id": data.get("session_id", ""),
                    "started_at": data.get("started_at", ""),
                    "request_count": len(data.get("requests", [])),
                })
            except Exception:
                pass
        return result

    @app.get("/audit/current")
    def audit_current():
        """Return current session audit log"""
        return _audit_data





# ============================================================
# Dashboard UI
# ============================================================

if not ARGS.fast:
    @app.get("/ui", response_class=HTMLResponse)
    async def dashboard():
        models = get_models()
        api_base = get_api_base()
        uptime = datetime.now(timezone.utc) - SESSION_START
        h, m = int(uptime.total_seconds() // 3600), int((uptime.total_seconds() % 3600) // 60)
        uptime_str = f"{h}h {m}m" if h else f"{m}m {int(uptime.total_seconds() % 60)}s"
    
        req_count = len(_audit_data["requests"])
        ok_count = sum(1 for r in _audit_data["requests"] if r["response"]["status_code"] == 200)
        err_count = req_count - ok_count
    
        claude_models = [m for m in models if m["id"].startswith("claude")]
        other_models  = [m for m in models if not m["id"].startswith("claude")]
    
        def ep_tag(ep):
            cls = "tag-green" if "/v1/messages" in ep else "tag-blue"
            return f'<span class="tag {cls}">{ep}</span>'
    
        def model_rows(mlist):
            rows = []
            for m in mlist:
                eps = "".join(ep_tag(e) for e in (m.get("supported_endpoints") or []))
                star = '<span class="star">★</span>' if m.get("model_picker_enabled") else ""
                rows.append(
                    f'<tr><td class="mono">{m["id"]}</td>'
                    f'<td class="muted">{m.get("name","")}</td>'
                    f'<td>{eps or "<span class=muted>—</span>"}</td>'
                    f'<td>{star}</td></tr>'
                )
            return "".join(rows)
    
        html = f"""<!DOCTYPE html>
    <html lang="en" data-theme="dark">
    <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Copilot Proxy</title>
    <style>
    :root[data-theme="dark"] {{
      --bg:#0f1117; --surface:#1a1d27; --border:#2a2d3a; --text:#e2e4ed;
      --muted:#6b7280; --accent:#60a5fa; --green:#34d399; --red:#f87171;
      --code-bg:#12151f; --hover:#21242f;
      --tag-g-bg:#064e3b; --tag-g-fg:#6ee7b7;
      --tag-b-bg:#1e3a5f; --tag-b-fg:#93c5fd;
      --btn-bg:#1d4ed8; --btn-hover:#2563eb; --star:#fbbf24;
    }}
    :root[data-theme="light"] {{
      --bg:#f8f9fb; --surface:#ffffff; --border:#e5e7eb; --text:#111827;
      --muted:#6b7280; --accent:#2563eb; --green:#059669; --red:#dc2626;
      --code-bg:#f1f3f7; --hover:#f3f4f6;
      --tag-g-bg:#d1fae5; --tag-g-fg:#065f46;
      --tag-b-bg:#dbeafe; --tag-b-fg:#1e40af;
      --btn-bg:#2563eb; --btn-hover:#1d4ed8; --star:#d97706;
    }}
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;line-height:1.5;background:var(--bg);color:var(--text);transition:background .2s,color .2s}}
    /* header */
    .header{{display:flex;align-items:center;justify-content:space-between;padding:10px 20px;border-bottom:1px solid var(--border);background:var(--surface);position:sticky;top:0;z-index:10;gap:10px;flex-wrap:wrap}}
    .header-left{{display:flex;align-items:center;gap:10px}}
    .header-title{{font-size:15px;font-weight:600}}
    .pulse{{width:8px;height:8px;border-radius:50%;background:var(--green);animation:pulse 2s ease-in-out infinite;flex-shrink:0}}
    @keyframes pulse{{0%,100%{{opacity:1;transform:scale(1)}}50%{{opacity:.5;transform:scale(.85)}}}}
    .header-meta{{font-size:12px;color:var(--muted)}}
    .header-right{{display:flex;align-items:center;gap:8px}}
    .pill{{font-size:12px;color:var(--muted);background:var(--bg);border:1px solid var(--border);border-radius:99px;padding:3px 10px}}
    .theme-btn{{cursor:pointer;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--text);padding:5px 10px;font-size:13px;transition:background .15s}}
    .theme-btn:hover{{background:var(--hover)}}
    /* config strip */
    .config-strip{{display:flex;align-items:center;gap:10px;padding:8px 20px;background:var(--surface);border-bottom:1px solid var(--border);flex-wrap:wrap}}
    .os-tabs{{display:flex;gap:2px;flex-shrink:0}}
    .os-tab{{cursor:pointer;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--muted);padding:3px 9px;font-size:11px;transition:background .15s,color .15s}}
    .os-tab:hover{{background:var(--hover)}}
    .os-tab.active{{background:var(--btn-bg);color:#fff;border-color:var(--btn-bg)}}
    .code-inline{{font-family:'SF Mono','Fira Code',monospace;font-size:12px;background:var(--code-bg);border:1px solid var(--border);border-radius:5px;padding:4px 10px;color:var(--accent);flex:1;min-width:180px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
    .copy-btn{{background:var(--btn-bg);color:#fff;border:none;border-radius:4px;padding:4px 10px;font-size:11px;cursor:pointer;transition:background .15s;white-space:nowrap;flex-shrink:0}}
    .copy-btn:hover{{background:var(--btn-hover)}}
    .config-sep{{width:1px;height:24px;background:var(--border);flex-shrink:0}}
    /* stats */
    .stats-inline{{display:flex;gap:16px;align-items:center;margin-left:auto}}
    .stat-item{{text-align:center}}
    .stat-num{{font-size:17px;font-weight:700;line-height:1}}
    .stat-label{{font-size:10px;color:var(--muted)}}
    .num-green{{color:var(--green)}} .num-red{{color:var(--red)}} .num-blue{{color:var(--accent)}}
    /* page grid */
    .page{{display:grid;grid-template-columns:minmax(260px,30%) 1fr;height:calc(100vh - 88px);overflow:hidden}}
    @media(max-width:800px){{.page{{grid-template-columns:1fr;height:auto}}}}
    /* panels */
    .panel{{display:flex;flex-direction:column;overflow:hidden;border-right:1px solid var(--border)}}
    .panel:last-child{{border-right:none}}
    .panel-header{{display:flex;align-items:center;justify-content:space-between;padding:8px 14px;border-bottom:1px solid var(--border);background:var(--surface);flex-shrink:0}}
    .panel-title{{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.4px;color:var(--muted)}}
    .panel-body{{flex:1;overflow-y:auto}}
    /* tables */
    table{{width:100%;border-collapse:collapse;font-size:12.5px}}
    th{{text-align:left;padding:7px 12px;font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--surface);z-index:1}}
    td{{padding:7px 12px;border-bottom:1px solid var(--border);vertical-align:middle}}
    tr:last-child td{{border-bottom:none}}
    tr:hover td{{background:var(--hover)}}
    .mono{{font-family:'SF Mono','Fira Code',monospace;font-size:12px}}
    .muted{{color:var(--muted)}}
    .trunc{{overflow:hidden;white-space:nowrap;text-overflow:ellipsis}}
    .empty{{text-align:center;color:var(--muted);padding:24px}}
    /* tags */
    .tag{{display:inline-block;font-size:10px;border-radius:3px;padding:1px 5px;margin:1px;font-family:monospace;white-space:nowrap}}
    .tag-green{{background:var(--tag-g-bg);color:var(--tag-g-fg)}}
    .tag-blue{{background:var(--tag-b-bg);color:var(--tag-b-fg)}}
    .tag-orange{{background:#431407;color:#fdba74}}
    .tag-purple{{background:#2e1065;color:#c4b5fd}}
    .status-ok{{color:var(--green);font-weight:600}}
    .status-err{{color:var(--red);font-weight:600}}
    .star{{color:var(--star)}}
    .req-row{{cursor:default}}
    </style>
    </head>
    <body>
    <div class="header">
      <div class="header-left">
        <div class="pulse"></div>
        <span class="header-title">Copilot Proxy</span>
        <span class="header-meta">session {SESSION_ID[:8]} · {api_base}</span>
      </div>
      <div class="header-right">
        <span class="pill">⏱ {uptime_str}</span>
        <button class="theme-btn" onclick="toggleTheme()" id="themeBtn">☀ Light</button>
      </div>
    </div>
    
    <div class="config-strip">
      <div class="os-tabs">
        <button class="os-tab active" onclick="switchOS('mac')">macOS/Linux</button>
        <button class="os-tab" onclick="switchOS('ps')">PowerShell</button>
      </div>
      <code class="code-inline" id="cfg-mac">export ANTHROPIC_BASE_URL=http://localhost:8082 ANTHROPIC_AUTH_TOKEN=dummy &amp;&amp; claude</code>
      <code class="code-inline" id="cfg-ps" style="display:none">$env:ANTHROPIC_BASE_URL="http://localhost:8082"; $env:ANTHROPIC_AUTH_TOKEN="dummy"; claude</code>
      <button class="copy-btn" onclick="copyActive()">Copy</button>
      <div class="stats-inline">
        <div class="stat-item"><div class="stat-num num-blue" id="s-total">{req_count}</div><div class="stat-label">Total</div></div>
        <div class="stat-item"><div class="stat-num num-green" id="s-ok">{ok_count}</div><div class="stat-label">OK</div></div>
        <div class="stat-item"><div class="stat-num num-red" id="s-err">{err_count}</div><div class="stat-label">Err</div></div>
        <div class="stat-item"><div class="stat-num">{len(models)}</div><div class="stat-label">Models</div></div>
      </div>
    </div>
    
    <div class="page">
      <!-- Models -->
      <div class="panel">
        <div class="panel-header"><span class="panel-title">Models ({len(models)})</span></div>
        <div class="panel-body">
          <table>
            <thead><tr><th>Model ID</th><th>Name</th><th>Endpoint</th><th></th></tr></thead>
            <tbody>
              <tr><td colspan="4" style="padding:5px 12px;font-size:10px;font-weight:600;color:var(--muted);background:var(--bg)">CLAUDE — /v1/messages passthrough</td></tr>
              {model_rows(claude_models)}
              <tr><td colspan="4" style="padding:5px 12px;font-size:10px;font-weight:600;color:var(--muted);background:var(--bg)">Other Models</td></tr>
              {model_rows(other_models)}
            </tbody>
          </table>
        </div>
      </div>
    
      <!-- Requests -->
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">Requests (current session)</span>
          <span id="req-count-label" style="font-size:11px;color:var(--muted)"></span>
        </div>
        <div class="panel-body">
          <table>
            <thead>
              <tr>
                <th style="width:60px">Time</th>
                <th style="width:170px">Model</th>
                <th style="width:40px">St</th>
                <th style="width:48px">ms</th>
                <th style="width:90px">Type</th>
                <th>Preview</th>
              </tr>
            </thead>
            <tbody id="req-tbody"><tr><td colspan="6" class="empty">No requests yet</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>
    
    <script>
    // ── Theme ──
    const html = document.documentElement;
    const themeBtn = document.getElementById('themeBtn');
    applyTheme(localStorage.getItem('theme') || 'dark');
    function applyTheme(t) {{
      html.dataset.theme = t;
      themeBtn.textContent = t === 'dark' ? '☀ Light' : '☾ Dark';
      localStorage.setItem('theme', t);
    }}
    function toggleTheme() {{ applyTheme(html.dataset.theme === 'dark' ? 'light' : 'dark'); }}

    // ── OS tab + copy ──
    let _activeOS = 'mac';
    function switchOS(os) {{
      _activeOS = os;
      ['mac','ps'].forEach(k => {{
        document.getElementById('cfg-'+k).style.display = k===os ? '' : 'none';
      }});
      document.querySelectorAll('.os-tab').forEach((b,i) => {{
        b.classList.toggle('active', ['mac','ps'][i] === os);
      }});
    }}
    function copyActive() {{
      const text = document.getElementById('cfg-'+_activeOS).textContent;
      navigator.clipboard.writeText(text).then(() => {{
        const b = document.querySelector('.copy-btn');
        b.textContent = 'Copied ✓'; setTimeout(() => b.textContent = 'Copy', 1500);
      }});
    }}
    
    // ── Escape HTML ──
    function esc(s) {{
      return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }}
    
    // ── Requests ──
    let _all = [];
    
    function msgTypeSummary(msgs) {{
      if (!msgs || !msgs.length) return '';
      const counts = {{}};
      msgs.forEach(m => {{ counts[m.kind] = (counts[m.kind]||0) + 1; }});
      const cls = {{ message:'tag-blue', tool_use:'tag-orange', tool_result:'tag-purple' }};
      return Object.entries(counts).map(([k,v]) =>
        `<span class="tag ${{cls[k]||'tag-blue'}}">${{k==='message'?'msg':k==='tool_use'?'tool↑':'tool↓'}} ${{v}}</span>`
      ).join('');
    }}
    
    function lastPreview(r) {{
      const msgs = r.messages;
      if (msgs && msgs.length) {{
        const last = msgs[msgs.length-1];
        const text = (last.kind === 'message') ? (last.preview || '') : (last.body || last.preview || '');
        return esc(text.slice(0, 120));
      }}
      return esc((r.request_preview?.last_user_msg || '').slice(0, 120));
    }}
    
    async function loadRequests() {{
      try {{
        const data = await fetch('/audit/current').then(r => r.json());
        const reqs = (data.requests || []).slice().reverse();
        if (reqs.length === _all.length) return;  // no change
        _all = reqs;
        renderRows();
        document.getElementById('req-count-label').textContent = _all.length + ' requests';
        // update stats
        const ok = _all.filter(r => r.response?.status_code === 200).length;
        document.getElementById('s-total').textContent = _all.length;
        document.getElementById('s-ok').textContent = ok;
        document.getElementById('s-err').textContent = _all.length - ok;
      }} catch(e) {{}}
    }}
    
    function renderRows() {{
      const tbody = document.getElementById('req-tbody');
      if (!_all.length) {{
        tbody.innerHTML = '<tr><td colspan="6" class="empty">No requests yet</td></tr>';
        return;
      }}
      tbody.innerHTML = _all.map(r => {{
        const ok  = r.response?.status_code === 200;
        const cls = ok ? 'status-ok' : 'status-err';
        const ts  = (r.timestamp||'').slice(11,19);
        const typeTags = msgTypeSummary(r.messages);
        const preview  = lastPreview(r);
        return `<tr>
          <td class="muted mono" style="white-space:nowrap">${{ts}}</td>
          <td class="mono trunc" style="max-width:170px">${{esc(r.copilot_model||r.original_model||'')}}</td>
          <td><span class="${{cls}}">${{r.response?.status_code??'—'}}</span></td>
          <td class="muted" style="white-space:nowrap">${{r.duration_ms!=null?Math.round(r.duration_ms):'—'}}</td>
          <td>${{typeTags||'<span class="muted">—</span>'}}</td>
          <td class="trunc" style="max-width:0;color:var(--muted);font-size:11px">${{preview}}</td>
        </tr>`;
      }}).join('');
    }}
    
    // ── Modal ──
    loadRequests();
    setInterval(loadRequests, 5000);
    </script>
    </body>
    </html>"""
        return HTMLResponse(content=html)


# ============================================================
# Entry Point
# ============================================================

if __name__ == "__main__":
    import uvicorn

    host = "0.0.0.0" if ARGS.share else "127.0.0.1"

    logger.info("=== GitHub Copilot → Anthropic Proxy Starting ===")
    logger.info("Session ID: %s", SESSION_ID)
    logger.info("Mode: %s", ("fast" if ARGS.fast else "normal") + (" + share" if ARGS.share else ""))
    if not ARGS.fast:
        logger.info("Audit file: %s", AUDIT_FILE)

    logger.info("Initializing GitHub Copilot authentication...")
    try:
        api_key = get_api_key()
        api_base = get_api_base()
        logger.info("Authentication successful, API base: %s", api_base)
        models = get_models()
        logger.info("Loaded %d models", len(models))
    except Exception as e:
        logger.warning("Authentication failed at startup (will retry on first request): %s", e)

    base_url = f"http://{host}:{ARGS.port}"
    print("\n" + "=" * 60)
    if not ARGS.fast:
        print(f"  Dashboard : {base_url}/ui")
    print("  Configure Claude Code:")
    print("  [macOS/Linux]")
    print(f"    export ANTHROPIC_BASE_URL={base_url}")
    print( "    export ANTHROPIC_AUTH_TOKEN=dummy")
    print("  [Windows PowerShell]")
    print(f'    $env:ANTHROPIC_BASE_URL="{base_url}"')
    print( '    $env:ANTHROPIC_AUTH_TOKEN="dummy"')
    if ARGS.share:
        print("  ⚠  Share mode: accessible to anyone on the LAN")
    if ARGS.fast:
        print("  Fast mode: UI and audit disabled")
    print("=" * 60 + "\n")

    uvicorn.run(app, host=host, port=ARGS.port, log_level="warning")
