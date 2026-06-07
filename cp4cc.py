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
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

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
ADMIN_USER = os.environ.get("CP4CC_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("CP4CC_ADMIN_PASSWORD", "")
ADMIN_SESSION_SECRET = os.environ.get("CP4CC_ADMIN_SESSION_SECRET") or os.environ.get("CP4CC_PUBLIC_TOKEN") or "cp4cc-dev-secret"
ADMIN_COOKIE_NAME = "cp4cc_admin"
ADMIN_SESSION_MAX_AGE = 60 * 60 * 12
ADMIN_PUBLIC_PREFIX = os.environ.get("CP4CC_ADMIN_PUBLIC_PREFIX", "")

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
# Crash diagnostics — capture "silent death" causes
# ============================================================
import atexit
import faulthandler
import signal
import sys
import threading

DIAG_DIR = LOGS_DIR / "diag"
DIAG_DIR.mkdir(parents=True, exist_ok=True)
FAULT_LOG = DIAG_DIR / "faulthandler.log"
HEARTBEAT_LOG = DIAG_DIR / "heartbeat.log"
LIFECYCLE_LOG = DIAG_DIR / "lifecycle.log"

# Open fault file unbuffered-binary; faulthandler writes via low-level fd write,
# so traceback survives even when the process is killed mid-flight.
_fault_fp = open(FAULT_LOG, "ab", buffering=0)
faulthandler.enable(file=_fault_fp, all_threads=True)

# On Windows also register SIGABRT/SIGFPE/SIGSEGV/SIGILL handlers explicitly
# (faulthandler.register only exists on Unix; on Windows the global enable()
# already covers SEH exceptions like access violations.)
if hasattr(faulthandler, "register"):
    for _sig_name in ("SIGABRT", "SIGFPE", "SIGSEGV", "SIGILL", "SIGBUS"):
        _sig = getattr(signal, _sig_name, None)
        if _sig is not None:
            try:
                faulthandler.register(_sig, file=_fault_fp, all_threads=True, chain=False)
            except (ValueError, OSError, RuntimeError):
                pass


def _lifecycle(event: str, **fields) -> None:
    """Single-line lifecycle event written to both app.log and lifecycle.log."""
    payload = {"ts": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(), "event": event, **fields}
    line = json.dumps(payload, default=str, ensure_ascii=False)
    try:
        with open(LIFECYCLE_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    logger.info("LIFECYCLE %s", line)


def _signal_handler(signum, frame):
    try:
        name = signal.Signals(signum).name
    except Exception:
        name = str(signum)
    _lifecycle("signal_received", signal=name, signum=int(signum))
    # Dump current stack of all threads to fault log for forensic purposes
    try:
        faulthandler.dump_traceback(file=_fault_fp, all_threads=True)
        _fault_fp.flush()
    except Exception:
        pass
    # Re-raise default behavior so process actually exits (or gets re-killed)
    if signum in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None), getattr(signal, "SIGBREAK", None)):
        sys.exit(128 + int(signum))


for _sig_name in ("SIGINT", "SIGTERM", "SIGBREAK", "SIGHUP"):
    _sig = getattr(signal, _sig_name, None)
    if _sig is not None:
        try:
            signal.signal(_sig, _signal_handler)
        except (ValueError, OSError):
            pass


def _excepthook(exc_type, exc_value, exc_tb):
    import traceback as _tb
    tb_text = "".join(_tb.format_exception(exc_type, exc_value, exc_tb))
    _lifecycle("uncaught_exception", exc_type=getattr(exc_type, "__name__", str(exc_type)), msg=str(exc_value))
    logger.error("UNCAUGHT EXCEPTION:\n%s", tb_text)


sys.excepthook = _excepthook


def _thread_excepthook(args):
    import traceback as _tb
    tb_text = "".join(_tb.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    _lifecycle("thread_exception", thread=getattr(args.thread, "name", "?"), exc_type=getattr(args.exc_type, "__name__", "?"), msg=str(args.exc_value))
    logger.error("THREAD EXCEPTION in %s:\n%s", getattr(args.thread, "name", "?"), tb_text)


threading.excepthook = _thread_excepthook


# In-flight request tracking (populated by middleware below)
_inflight_lock = threading.Lock()
_inflight_requests: dict[str, dict] = {}


def _get_rss_mb() -> float:
    """Return current RSS in MB. Best-effort, never raises."""
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        h = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(counters), counters.cb):
            return counters.WorkingSetSize / (1024 * 1024)
    except Exception:
        pass
    return -1.0


def _heartbeat_loop():
    """Write heartbeat every 15s. If the process dies, the last heartbeat
    timestamp + in-flight request list will pinpoint the moment and likely culprit."""
    while True:
        try:
            with _inflight_lock:
                inflight_snapshot = [
                    {"id": rid, "path": v["path"], "age_s": round(time.time() - v["start"], 2),
                     "size": v.get("size", 0)}
                    for rid, v in _inflight_requests.items()
                ]
            payload = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "pid": os.getpid(),
                "rss_mb": round(_get_rss_mb(), 1),
                "threads": threading.active_count(),
                "inflight_n": len(inflight_snapshot),
                "inflight": inflight_snapshot,
            }
            with open(HEARTBEAT_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as e:
            try:
                logger.warning("heartbeat failed: %s", e)
            except Exception:
                pass
        time.sleep(15)


_hb_thread = threading.Thread(target=_heartbeat_loop, name="heartbeat", daemon=True)
_hb_thread.start()


@atexit.register
def _on_exit():
    _lifecycle("process_exit", inflight_n=len(_inflight_requests))
    try:
        _fault_fp.flush()
        _fault_fp.close()
    except Exception:
        pass


_lifecycle("process_start", argv=sys.argv, python=sys.version.split()[0], cwd=os.getcwd())



def _json_size_and_hash(value) -> tuple[int, str]:
    try:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    except Exception:
        return -1, ""
    return len(raw), hashlib.sha256(raw).hexdigest()[:12]


def _summarize_payload_shape(body: dict) -> dict:
    stats = {
        "messages": 0,
        "function_calls": 0,
        "function_call_outputs": 0,
        "input_text_blocks": 0,
        "output_text_blocks": 0,
        "image_blocks": 0,
        "image_url_fields": 0,
        "image_url_chars": 0,
        "image_data_urls": 0,
        "image_data_url_chars": 0,
        "max_image_url_chars": 0,
        "base64_fields": 0,
        "base64_chars": 0,
        "text_chars": 0,
        "max_text_chars": 0,
        "string_chars": 0,
        "max_string_chars": 0,
        "large_strings": [],
    }

    def add_text(text: str) -> None:
        stats["text_chars"] += len(text)
        stats["max_text_chars"] = max(stats["max_text_chars"], len(text))

    def note_string(path: str, key: str, text: str) -> None:
        length = len(text)
        stats["string_chars"] += length
        stats["max_string_chars"] = max(stats["max_string_chars"], length)
        if length >= 1024:
            stats["large_strings"].append(
                {
                    "path": path,
                    "key": key,
                    "chars": length,
                    "data_url": text.startswith("data:"),
                }
            )

    def note_image_url(value) -> None:
        if isinstance(value, str):
            stats["image_url_fields"] += 1
            stats["image_url_chars"] += len(value)
            stats["max_image_url_chars"] = max(stats["max_image_url_chars"], len(value))
            if value.startswith("data:"):
                stats["image_data_urls"] += 1
                stats["image_data_url_chars"] += len(value)
        elif isinstance(value, dict):
            url = value.get("url") or value.get("image_url")
            if isinstance(url, str):
                note_image_url(url)

    def visit(value, path: str = "$") -> None:
        if isinstance(value, dict):
            typ = value.get("type")
            if typ == "message":
                stats["messages"] += 1
            elif typ == "function_call":
                stats["function_calls"] += 1
            elif typ == "function_call_output":
                stats["function_call_outputs"] += 1
            elif typ == "input_text":
                stats["input_text_blocks"] += 1
            elif typ == "output_text":
                stats["output_text_blocks"] += 1
            elif typ in ("input_image", "image"):
                stats["image_blocks"] += 1

            if "image_url" in value:
                stats["image_blocks"] += 1
                note_image_url(value.get("image_url"))

            source = value.get("source")
            if isinstance(source, dict) and source.get("type") == "base64":
                data = source.get("data") or source.get("base64") or ""
                if isinstance(data, str):
                    stats["base64_fields"] += 1
                    stats["base64_chars"] += len(data)

            text = value.get("text")
            if isinstance(text, str):
                add_text(text)

            for key, child in value.items():
                child_path = f"{path}.{key}"
                if isinstance(child, str):
                    note_string(child_path, str(key), child)
                visit(child, child_path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")

    visit(body)
    stats["large_strings"] = sorted(
        stats["large_strings"],
        key=lambda item: item["chars"],
        reverse=True,
    )[:10]
    return stats


def _compact_shape(stats: dict) -> dict:
    keys = (
        "messages",
        "function_calls",
        "function_call_outputs",
        "input_text_blocks",
        "output_text_blocks",
        "image_blocks",
        "image_url_fields",
        "image_url_chars",
        "image_data_urls",
        "image_data_url_chars",
        "text_chars",
        "max_text_chars",
        "max_string_chars",
    )
    return {key: stats.get(key, 0) for key in keys if stats.get(key, 0)}


def _value_hint(value) -> dict:
    hint = {"kind": type(value).__name__}
    if isinstance(value, dict):
        if "type" in value:
            hint["type"] = value.get("type")
        if "role" in value:
            hint["role"] = value.get("role")
        if isinstance(value.get("content"), list):
            hint["content_items"] = len(value["content"])
        if isinstance(value.get("output"), list):
            hint["output_items"] = len(value["output"])
    return hint


def _summarize_top_key_sizes(body: dict) -> list[dict]:
    sizes = []
    for key, value in body.items():
        size, digest = _json_size_and_hash(value)
        sizes.append({"key": key, "bytes": size, "hash": digest, **_value_hint(value)})
    return sorted(sizes, key=lambda item: item["bytes"], reverse=True)


def _summarize_input_item_sizes(body: dict, limit: int = 12) -> list[dict]:
    input_value = body.get("input")
    if not isinstance(input_value, list):
        return []

    items = []
    for index, item in enumerate(input_value):
        size, digest = _json_size_and_hash(item)
        item_shape = _compact_shape(_summarize_payload_shape(item))
        items.append(
            {
                "index": index,
                "bytes": size,
                "hash": digest,
                **_value_hint(item),
                "shape": item_shape,
            }
        )
    return sorted(items, key=lambda item: item["bytes"], reverse=True)[:limit]


def log_request_diag(
    req_id: str,
    endpoint: str,
    original_model: str,
    copilot_model: str,
    request: Request,
    body: dict,
    forward_body: dict,
) -> None:
    body_bytes, body_hash = _json_size_and_hash(body)
    forward_bytes, forward_hash = _json_size_and_hash(forward_body)
    input_value = body.get("input")
    input_type = type(input_value).__name__
    input_items = len(input_value) if isinstance(input_value, list) else None
    input_chars = len(input_value) if isinstance(input_value, str) else None
    tools = body.get("tools")
    include = body.get("include")
    reasoning = body.get("reasoning")
    shape = _summarize_payload_shape(body)
    forward_shape = _summarize_payload_shape(forward_body)
    diag = {
        "content_length": request.headers.get("content-length"),
        "body_bytes": body_bytes,
        "forward_bytes": forward_bytes,
        "body_hash": body_hash,
        "forward_hash": forward_hash,
        "top_keys": sorted(body.keys()),
        "stream": body.get("stream", False),
        "input_type": input_type,
        "input_items": input_items,
        "input_chars": input_chars,
        "tools_count": len(tools) if isinstance(tools, list) else 0,
        "include_count": len(include) if isinstance(include, list) else 0,
        "previous_response_id": bool(body.get("previous_response_id")),
        "truncation": body.get("truncation"),
        "parallel_tool_calls": body.get("parallel_tool_calls"),
        "reasoning_keys": sorted(reasoning.keys()) if isinstance(reasoning, dict) else [],
        "shape": shape,
        "forward_shape": forward_shape,
        "top_key_sizes": _summarize_top_key_sizes(body),
        "forward_top_key_sizes": _summarize_top_key_sizes(forward_body),
        "input_item_sizes": _summarize_input_item_sizes(body),
        "forward_input_item_sizes": _summarize_input_item_sizes(forward_body),
    }
    logger.info(
        "request_diag id=%s endpoint=%s model=%s→%s diag=%s",
        req_id[:8],
        endpoint,
        original_model,
        copilot_model,
        json.dumps(diag, ensure_ascii=False, separators=(",", ":")),
    )


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip())
        return value if value >= 0 else default
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "").strip())
        return value if value >= 0 else default
    except ValueError:
        return default


IMAGE_SINGLE_CHAR_LIMIT = _int_env("CP4CC_IMAGE_SINGLE_CHAR_LIMIT", 1_000_000)
IMAGE_TOTAL_CHAR_LIMIT = _int_env("CP4CC_IMAGE_TOTAL_CHAR_LIMIT", 1_200_000)
IMAGE_MAX_FORWARDED = _int_env("CP4CC_IMAGE_MAX_FORWARDED", 4)
IMAGE_ATTACHMENT_DIR = LOGS_DIR / "image_attachments"
DATA_IMAGE_RE = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.*)$", re.DOTALL)
UPSTREAM_BUSY_RETRIES = _int_env("CP4CC_UPSTREAM_BUSY_RETRIES", 2)
UPSTREAM_BUSY_BACKOFF_SECONDS = _float_env("CP4CC_UPSTREAM_BUSY_BACKOFF_SECONDS", 2.0)


def _is_data_image_url(value) -> bool:
    return isinstance(value, str) and DATA_IMAGE_RE.match(value) is not None


def _image_meta(data_url: str) -> dict:
    match = DATA_IMAGE_RE.match(data_url)
    if not match:
        return {"mime": "image/unknown", "ext": "img", "chars": len(data_url)}
    mime, encoded = match.groups()
    subtype = mime.split("/", 1)[1].split("+", 1)[0].lower()
    ext = {"jpeg": "jpg", "svg+xml": "svg"}.get(subtype, subtype or "img")
    return {
        "mime": mime,
        "ext": ext,
        "chars": len(data_url),
        "base64_chars": len(encoded),
        "approx_bytes": len(encoded) * 3 // 4,
        "sha256": hashlib.sha256(data_url.encode("utf-8")).hexdigest()[:16],
    }


def _save_data_image(data_url: str, meta: dict) -> str | None:
    match = DATA_IMAGE_RE.match(data_url)
    if not match:
        return None
    _, encoded = match.groups()
    try:
        IMAGE_ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
        path = IMAGE_ATTACHMENT_DIR / f"{meta['sha256']}.{meta['ext']}"
        if not path.exists():
            path.write_bytes(base64.b64decode(encoded, validate=False))
        return str(path)
    except Exception as exc:
        logger.warning("failed to persist omitted image attachment: %s", exc)
        return None


def _path_to_str(path: tuple) -> str:
    result = "$"
    for part in path:
        if isinstance(part, int):
            result += f"[{part}]"
        else:
            result += f".{part}"
    return result


def _collect_data_images(value, path: tuple = ()) -> list[dict]:
    found = []
    if isinstance(value, dict):
        image_url = value.get("image_url")
        if value.get("type") in ("input_image", "image") and _is_data_image_url(image_url):
            found.append({"path": path, "path_str": _path_to_str(path), "url": image_url, **_image_meta(image_url)})
        source = value.get("source")
        if isinstance(source, dict) and source.get("type") == "base64":
            data = source.get("data") or source.get("base64")
            media_type = source.get("media_type") or source.get("mime_type")
            if isinstance(data, str) and isinstance(media_type, str) and media_type.startswith("image/"):
                data_url = f"data:{media_type};base64,{data}"
                found.append({"path": path, "path_str": _path_to_str(path), "url": data_url, **_image_meta(data_url)})
        for key, child in value.items():
            found.extend(_collect_data_images(child, path + (key,)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_collect_data_images(child, path + (index,)))
    return found


def _image_placeholder(ref: dict, reason: str) -> str:
    saved_path = _save_data_image(ref["url"], ref)
    saved = f", saved={saved_path}" if saved_path else ""
    return (
        "[cp4cc image attachment omitted before GitHub Copilot forwarding: "
        f"path={ref['path_str']}, mime={ref['mime']}, chars={ref['chars']}, "
        f"approx_bytes={ref.get('approx_bytes', 0)}, sha256={ref['sha256']}{saved}, "
        f"reason={reason}. The original image remains available locally; use a smaller "
        "crop or explicitly inspect the saved file when visual details are required.]"
    )


def sanitize_responses_payload(body: dict) -> tuple[dict, dict]:
    """Approximate Copilot's attachment layer for Codex image tool output.

    Codex records view_image results as data:image URLs inside the Responses input
    history. GitHub Copilot's native clients use an attachment/blob path instead of
    replaying large base64 data URLs in every turn. Keep small recent images, but
    replace oversized or over-budget images with text placeholders before forwarding.
    """
    refs = _collect_data_images(body)
    if not refs:
        return body, {"images": 0, "kept": 0, "omitted": 0}

    keep_paths: set[tuple] = set()
    omit_reasons: dict[tuple, str] = {}
    total_chars = 0
    kept = 0
    for ref in reversed(refs):
        if ref["chars"] > IMAGE_SINGLE_CHAR_LIMIT:
            omit_reasons[ref["path"]] = (
                f"single image exceeds CP4CC_IMAGE_SINGLE_CHAR_LIMIT={IMAGE_SINGLE_CHAR_LIMIT}"
            )
        elif kept >= IMAGE_MAX_FORWARDED:
            omit_reasons[ref["path"]] = f"forwarded image count exceeds CP4CC_IMAGE_MAX_FORWARDED={IMAGE_MAX_FORWARDED}"
        elif total_chars + ref["chars"] > IMAGE_TOTAL_CHAR_LIMIT:
            omit_reasons[ref["path"]] = (
                f"image data budget exceeds CP4CC_IMAGE_TOTAL_CHAR_LIMIT={IMAGE_TOTAL_CHAR_LIMIT}"
            )
        else:
            keep_paths.add(ref["path"])
            total_chars += ref["chars"]
            kept += 1

    by_path = {ref["path"]: ref for ref in refs}

    def clone(value, path: tuple = ()):
        if isinstance(value, dict):
            image_url = value.get("image_url")
            if value.get("type") in ("input_image", "image") and _is_data_image_url(image_url):
                if path in keep_paths:
                    return dict(value)
                return {"type": "input_text", "text": _image_placeholder(by_path[path], omit_reasons[path])}
            source = value.get("source")
            if (
                isinstance(source, dict)
                and source.get("type") == "base64"
                and isinstance(source.get("media_type") or source.get("mime_type"), str)
                and (source.get("media_type") or source.get("mime_type")).startswith("image/")
                and path in by_path
                and path not in keep_paths
            ):
                return {"type": "text", "text": _image_placeholder(by_path[path], omit_reasons[path])}
            return {key: clone(child, path + (key,)) for key, child in value.items()}
        if isinstance(value, list):
            return [clone(child, path + (index,)) for index, child in enumerate(value)]
        return value

    sanitized = clone(body)
    omitted = len(refs) - len(keep_paths)
    omitted_chars = sum(ref["chars"] for ref in refs if ref["path"] not in keep_paths)
    return sanitized, {
        "images": len(refs),
        "kept": len(keep_paths),
        "omitted": omitted,
        "kept_chars": total_chars,
        "omitted_chars": omitted_chars,
        "single_char_limit": IMAGE_SINGLE_CHAR_LIMIT,
        "total_char_limit": IMAGE_TOTAL_CHAR_LIMIT,
        "max_forwarded": IMAGE_MAX_FORWARDED,
    }

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


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_usage(response_body) -> dict:
    if not isinstance(response_body, dict):
        return {}
    usage = response_body.get("usage") or {}
    if not isinstance(usage, dict):
        return {}
    return {k: v for k, v in usage.items() if isinstance(v, (int, float))}


def update_usage_from_sse_line(line: str, usage: dict) -> None:
    """Best-effort extraction of usage from an SSE data line without affecting streaming."""
    if not line.startswith("data:"):
        return
    data_str = line[5:].strip()
    if not data_str or data_str == "[DONE]":
        return
    try:
        payload = json.loads(data_str)
    except Exception:
        return
    if not isinstance(payload, dict):
        return

    for candidate in (payload, payload.get("response") if isinstance(payload.get("response"), dict) else None):
        extracted = _extract_usage(candidate)
        if extracted:
            usage.update(extracted)


def build_usage_stats(requests: list[dict], now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    total = len(requests)
    ok = 0
    errors = 0
    models = Counter()
    endpoints = Counter()
    status = Counter()
    tokens = Counter()
    sources = Counter()
    source_ips = Counter()
    ip_tokens: dict[str, Counter] = {}
    ip_models: dict[str, Counter] = {}
    ip_status: dict[str, Counter] = {}
    durations = []
    hourly = Counter()
    daily = Counter()
    last_24h = 0
    last_7d = 0

    for req in requests:
        code = str((req.get("response") or {}).get("status_code", "unknown"))
        status[code] += 1
        if code == "200":
            ok += 1
        else:
            errors += 1

        model = req.get("original_model") or req.get("copilot_model") or "unknown"
        models[model] += 1
        endpoints[req.get("endpoint") or "unknown"] += 1
        source = req.get("source") or {}
        if isinstance(source, dict):
            sources[source.get("client") or "unknown"] += 1
            if source.get("ip"):
                ip = source.get("ip")
                source_ips[ip] += 1
                ip_models.setdefault(ip, Counter())[model] += 1
                ip_status.setdefault(ip, Counter())[code] += 1
        else:
            sources["unknown"] += 1

        duration = req.get("duration_ms")
        if isinstance(duration, (int, float)):
            durations.append(float(duration))

        usage = _extract_usage((req.get("response") or {}).get("body"))
        for key, value in usage.items():
            tokens[key] += value
        source = req.get("source") or {}
        if isinstance(source, dict) and source.get("ip"):
            ip_counter = ip_tokens.setdefault(source.get("ip"), Counter())
            for key, value in usage.items():
                ip_counter[key] += value

        ts = _parse_ts(req.get("timestamp", ""))
        if ts:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = now - ts
            if age.total_seconds() <= 24 * 3600:
                last_24h += 1
            if age.total_seconds() <= 7 * 24 * 3600:
                last_7d += 1
            daily[ts.strftime("%Y-%m-%d")] += 1
            if age.total_seconds() <= 48 * 3600:
                hourly[ts.strftime("%m-%d %H:00")] += 1

    durations_sorted = sorted(durations)
    p95 = durations_sorted[int(len(durations_sorted) * 0.95) - 1] if durations_sorted else 0
    avg = round(sum(durations) / len(durations)) if durations else 0

    ip_rows = []
    for ip, count in source_ips.most_common():
        ip_rows.append({
            "ip": ip,
            "requests": count,
            "tokens": dict(ip_tokens.get(ip, Counter())),
            "models": dict(ip_models.get(ip, Counter()).most_common()),
            "status": dict(ip_status.get(ip, Counter()).most_common()),
        })

    return {
        "generated_at": now.isoformat(),
        "total": {"requests": total, "ok": ok, "errors": errors, "success_rate": round(ok / total * 100, 1) if total else 0},
        "last_24h": {"requests": last_24h},
        "last_7d": {"requests": last_7d},
        "models": [{"name": name, "count": count} for name, count in models.most_common()],
        "sources": [{"name": name, "count": count} for name, count in sources.most_common()],
        "source_ips": dict(source_ips.most_common()),
        "ip_rows": ip_rows,
        "endpoints": dict(endpoints.most_common()),
        "status": dict(status.most_common()),
        "tokens": dict(tokens),
        "latency": {"avg_ms": avg, "p95_ms": round(p95), "max_ms": round(max(durations)) if durations else 0},
        "hourly": dict(sorted(hourly.items())),
        "daily": dict(sorted(daily.items())),
    }


def build_daily_rows(requests: list[dict]) -> list[dict]:
    by_day: dict[str, dict] = {}
    for req in requests:
        ts = _parse_ts(req.get("timestamp", ""))
        if not ts:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        day = ts.strftime("%Y-%m-%d")
        row = by_day.setdefault(day, {
            "date": day,
            "requests": 0,
            "ok": 0,
            "errors": 0,
            "tokens": Counter(),
            "models": Counter(),
        })
        row["requests"] += 1
        code = str((req.get("response") or {}).get("status_code", "unknown"))
        if code == "200":
            row["ok"] += 1
        else:
            row["errors"] += 1
        model = req.get("original_model") or req.get("copilot_model") or "unknown"
        row["models"][model] += 1
        usage = _extract_usage((req.get("response") or {}).get("body"))
        for key, value in usage.items():
            row["tokens"][key] += value

    result = []
    for day in sorted(by_day):
        row = by_day[day]
        result.append({
            "date": row["date"],
            "requests": row["requests"],
            "ok": row["ok"],
            "errors": row["errors"],
            "tokens": dict(row["tokens"]),
            "models": dict(row["models"].most_common()),
        })
    return result


def build_request_rows(requests: list[dict]) -> list[dict]:
    rows = []
    for req in requests:
        usage = _extract_usage((req.get("response") or {}).get("body"))
        rows.append({
            "timestamp": req.get("timestamp", ""),
            "model": req.get("original_model") or req.get("copilot_model") or "unknown",
            "status": (req.get("response") or {}).get("status_code", "unknown"),
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
            "cache_read_input_tokens": int(usage.get("cache_read_input_tokens", 0) or 0),
            "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens", 0) or 0),
            "token_sum": int(sum(usage.values())) if usage else 0,
            "duration_ms": req.get("duration_ms", 0),
        })
    return sorted(rows, key=lambda row: row["input_tokens"])


def build_token_metrics(request_rows: list[dict]) -> dict:
    input_values = [row["input_tokens"] for row in request_rows if row.get("input_tokens")]
    output_values = [row["output_tokens"] for row in request_rows if row.get("output_tokens")]
    return {
        "avg_input_tokens": round(sum(input_values) / len(input_values)) if input_values else 0,
        "max_input_tokens": max(input_values) if input_values else 0,
        "avg_output_tokens": round(sum(output_values) / len(output_values)) if output_values else 0,
        "max_output_tokens": max(output_values) if output_values else 0,
    }


def build_ip_usage_stats(requests: list[dict], ip: str, now: datetime | None = None) -> dict:
    filtered = [req for req in requests if isinstance(req.get("source"), dict) and req.get("source", {}).get("ip") == ip]
    stats = build_usage_stats(filtered, now=now)
    stats["ip"] = ip
    stats["daily_rows"] = build_daily_rows(filtered)
    stats["request_rows"] = build_request_rows(filtered)
    stats["token_metrics"] = build_token_metrics(stats["request_rows"])
    return stats


def load_all_audit_requests() -> list[dict]:
    requests = []
    for path in sorted(AUDIT_DIR.glob("session_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            requests.extend(data.get("requests", []))
        except Exception as exc:
            logger.warning("failed to read audit file %s: %s", path, exc)
    return requests


def classify_user_agent(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if "codex" in ua:
        return "codex"
    if "claude" in ua:
        return "claude-code"
    if "cursor" in ua:
        return "cursor"
    if "vscode" in ua or "githubcopilot" in ua:
        return "vscode/copilot"
    if "curl" in ua:
        return "curl"
    return "unknown"


def source_from_request(request: Request) -> dict:
    headers = request.headers
    forwarded = headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() if forwarded else ""
    if not ip and getattr(request, "client", None):
        ip = request.client.host

    user_agent = headers.get("user-agent", "")
    explicit = headers.get("x-cp4cc-source") or headers.get("x-source") or headers.get("x-client-name")
    client = explicit.strip() if explicit else classify_user_agent(user_agent)
    return {
        "client": client or "unknown",
        "ip": ip or "unknown",
        "user_agent": user_agent[:200],
        "referer": headers.get("referer", "")[:200],
    }


def verify_admin_credentials(username: str, password: str) -> bool:
    if not ADMIN_PASSWORD:
        return False
    return hmac.compare_digest(username or "", ADMIN_USER) and hmac.compare_digest(password or "", ADMIN_PASSWORD)


def _admin_signature(payload: str) -> str:
    return hmac.new(ADMIN_SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def create_admin_session_token(username: str) -> str:
    expires = int(time.time()) + ADMIN_SESSION_MAX_AGE
    payload = f"{username}:{expires}"
    sig = _admin_signature(payload)
    raw = f"{payload}:{sig}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def verify_admin_session_token(token: str | None) -> str | None:
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        username, expires_s, sig = raw.rsplit(":", 2)
        payload = f"{username}:{expires_s}"
        if not hmac.compare_digest(sig, _admin_signature(payload)):
            return None
        if int(expires_s) < int(time.time()):
            return None
        return username
    except Exception:
        return None


def require_admin(request: Request) -> str | None:
    return verify_admin_session_token(request.cookies.get(ADMIN_COOKIE_NAME))


def html_escape(value) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def audit_log(
    req_id: str,
    request_body: dict,
    copilot_model: str,
    endpoint: str,
    response_body: dict | str | None,
    status_code: int,
    duration_ms: float,
    error: str | None = None,
    source: dict | None = None,
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
        "source": source or request_body.get("_source") or {"client": "unknown", "ip": "unknown"},
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


def is_expired_ide_token_error(status_code: int, body_text: str | None) -> bool:
    """Detect Copilot API key expiry reported by the upstream API itself."""
    if status_code != 401 or not body_text:
        return False
    lower = body_text.lower()
    return "token expired" in lower and ("ide token" in lower or "unauthorized" in lower)


def is_upstream_high_demand_error(status_code: int, body_text: str | None) -> bool:
    if status_code != 503 or not body_text:
        return False
    lower = body_text.lower()
    return (
        "high demand" in lower
        and ("upstream model provider" in lower or "try another model" in lower)
    )


def upstream_busy_retry_delay(retry_number: int) -> float:
    retry_number = max(1, retry_number)
    return min(30.0, UPSTREAM_BUSY_BACKOFF_SECONDS * (2 ** (retry_number - 1)))


def invalidate_api_key_cache(reason: str) -> None:
    try:
        os.remove(API_KEY_FILE)
        logger.warning("Invalidated cached Copilot API key: %s", reason)
    except FileNotFoundError:
        logger.warning("Cached Copilot API key already absent: %s", reason)
    except OSError as exc:
        logger.warning("Failed to invalidate cached Copilot API key: %s (%s)", reason, exc)


def refresh_upstream_auth_after_401(req_id: str, endpoint: str, error_msg: str) -> tuple[str, dict]:
    invalidate_api_key_cache(f"upstream {endpoint} req={req_id[:8]} returned token expiry: {error_msg[:200]}")
    api_key = get_api_key()
    return f"{get_api_base()}{endpoint}", get_copilot_headers(api_key)


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

    Override: any claude-opus-* is routed to claude-opus-4.7-1m-internal
    (matches the model Copilot CLI itself uses).
    """
    original = model
    model = re.sub(r"-\d{8}$", "", model)         # strip YYYYMMDD date suffix
    model = re.sub(r"(\d)-(\d+)$", r"\1.\2", model)  # 4-6 → 4.6

    # Force-route every Claude Opus variant to the 1M-context internal build
    if model.startswith("claude-opus"):
        model = "claude-opus-4.7-1m-internal"

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


def normalize_chat_completion_request(body: dict, mapped_model: str) -> dict:
    result = {k: v for k, v in body.items() if not k.startswith("_")}
    result["model"] = mapped_model
    if "max_tokens" in result and "max_completion_tokens" not in result:
        result["max_completion_tokens"] = result.pop("max_tokens")
    return result


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


def synthetic_responses_error_events(status_code: int, error_msg: str, response_id: str, model: str, req_id: str) -> list[str]:
    item_id = f"msg_{uuid4().hex[:24]}"
    text = (
        f"cp4cc upstream error {status_code} for request {req_id[:8]}: "
        f"{error_msg[:800]}"
    )
    completed = {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "model": model,
            "status": "completed",
            "output": [
                {
                    "id": item_id,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            ],
            "output_text": text,
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        },
    }
    return [
        f"event: response.created\ndata: {json.dumps({'type':'response.created','response':{'id':response_id,'model':model,'status':'in_progress'}})}\n\n",
        f"event: response.output_item.added\ndata: {json.dumps({'type':'response.output_item.added','output_index':0,'item':{'id':item_id,'type':'message','status':'in_progress','role':'assistant','content':[]}})}\n\n",
        f"event: response.output_text.delta\ndata: {json.dumps({'type':'response.output_text.delta','delta':text})}\n\n",
        f"event: response.output_item.done\ndata: {json.dumps({'type':'response.output_item.done','output_index':0,'item':completed['response']['output'][0]})}\n\n",
        f"event: response.completed\ndata: {json.dumps(completed)}\n\n",
    ]


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


@app.middleware("http")
async def _request_lifecycle(request: Request, call_next):
    """Track every request in the in-flight table so heartbeat can show what
    was running when the process died, and to log unexpected mid-request aborts."""
    rid = uuid4().hex[:8]
    try:
        size = int(request.headers.get("content-length", "0") or 0)
    except ValueError:
        size = 0
    info = {"path": request.url.path, "method": request.method, "start": time.time(), "size": size}
    with _inflight_lock:
        _inflight_requests[rid] = info
    try:
        response = await call_next(request)
        return response
    except BaseException as exc:
        _lifecycle(
            "request_failed",
            rid=rid,
            path=info["path"],
            method=info["method"],
            content_length=size,
            duration_s=round(time.time() - info["start"], 2),
            exc_type=type(exc).__name__,
            msg=str(exc)[:300],
        )
        raise
    finally:
        with _inflight_lock:
            _inflight_requests.pop(rid, None)


@app.on_event("startup")
async def _install_loop_diagnostics():
    import asyncio
    loop = asyncio.get_running_loop()

    def _loop_exc_handler(_loop, context):
        msg = context.get("exception") or context.get("message")
        _lifecycle("asyncio_exception", message=str(msg)[:500], keys=list(context.keys()))
        logger.error("ASYNCIO EXC: %s", context)

    loop.set_exception_handler(_loop_exc_handler)
    _lifecycle("loop_ready", loop=type(loop).__name__)


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


def upstream_body(body: dict, mapped_model: str) -> dict:
    """Copy request body for upstream while removing proxy-internal metadata."""
    result = {k: v for k, v in body.items() if not str(k).startswith("_")}
    result["model"] = mapped_model
    return result




@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    req_id = str(uuid4())
    t_start = time.monotonic()
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        logger.warning("Invalid Chat Completions JSON request req=%s: %s", req_id[:8], e)
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    body["_source"] = source_from_request(request)
    original_model: str = body.get("model", "")
    copilot_model = map_model_name(original_model)
    forward_body = normalize_chat_completion_request(body, copilot_model)

    try:
        api_key = get_api_key()
    except Exception as e:
        logger.error("Authentication failed req=%s: %s", req_id[:8], e)
        audit_log(req_id, body, copilot_model, "chat-completions-auth", None, 401, 0, str(e))
        raise HTTPException(status_code=401, detail=f"GitHub Copilot authentication failed: {e}")

    api_base = get_api_base()
    copilot_headers = get_copilot_headers(api_key)
    endpoint = "/chat/completions"
    is_stream = forward_body.get("stream", False)
    url = f"{api_base}{endpoint}"

    logger.debug(
        "chat-completions req=%s model=%s→%s endpoint=%s stream=%s",
        req_id[:8], original_model, copilot_model, endpoint, is_stream,
    )

    if is_stream:
        async def generate():
            nonlocal t_start
            error_msg = None
            status = 200
            usage = {}
            auth_retry_count = 0
            busy_retry_count = 0
            target_url = url
            headers = copilot_headers
            try:
                while True:
                    async with httpx.AsyncClient(timeout=120) as client:
                        async with client.stream("POST", target_url, headers=headers, json=forward_body) as resp:
                            status = resp.status_code
                            if status != 200:
                                err = await resp.aread()
                                error_msg = err.decode()
                                if auth_retry_count == 0 and is_expired_ide_token_error(status, error_msg):
                                    auth_retry_count += 1
                                    logger.warning("Upstream %s req=%s returned expired token; refreshing and retrying once", endpoint, req_id[:8])
                                    target_url, headers = refresh_upstream_auth_after_401(req_id, endpoint, error_msg)
                                    continue
                                if busy_retry_count < UPSTREAM_BUSY_RETRIES and is_upstream_high_demand_error(status, error_msg):
                                    busy_retry_count += 1
                                    delay = upstream_busy_retry_delay(busy_retry_count)
                                    logger.warning("Upstream %s req=%s returned high demand 503; retrying same model in %.1fs (%d/%d)", endpoint, req_id[:8], delay, busy_retry_count, UPSTREAM_BUSY_RETRIES)
                                    await asyncio.sleep(delay)
                                    continue
                                logger.warning("Upstream %s returned %s: %s", endpoint, status, error_msg[:200])
                                yield f"event: error\ndata: {json.dumps({'type':'error','error':{'message':error_msg}})}\n\n"
                            else:
                                async for line in resp.aiter_lines():
                                    if line:
                                        update_usage_from_sse_line(line, usage)
                                        yield line + "\n"
                                    else:
                                        yield "\n"
                            break
            except Exception as e:
                error_msg = str(e)
                logger.error("Chat Completions streaming request error req=%s: %s", req_id[:8], e)
            duration = (time.monotonic() - t_start) * 1000
            audit_log(req_id, body, copilot_model, "/v1/chat/completions", {"stream": True, "usage": usage}, status, duration, error_msg)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            target_url = url
            headers = copilot_headers
            resp = await client.post(target_url, headers=headers, json=forward_body)
            if is_expired_ide_token_error(resp.status_code, resp.text):
                logger.warning("Upstream %s req=%s returned expired token; refreshing and retrying once", endpoint, req_id[:8])
                target_url, headers = refresh_upstream_auth_after_401(req_id, endpoint, resp.text)
                resp = await client.post(target_url, headers=headers, json=forward_body)
            busy_retry_count = 0
            while busy_retry_count < UPSTREAM_BUSY_RETRIES and is_upstream_high_demand_error(resp.status_code, resp.text):
                busy_retry_count += 1
                delay = upstream_busy_retry_delay(busy_retry_count)
                logger.warning("Upstream %s req=%s returned high demand 503; retrying same model in %.1fs (%d/%d)", endpoint, req_id[:8], delay, busy_retry_count, UPSTREAM_BUSY_RETRIES)
                await asyncio.sleep(delay)
                resp = await client.post(target_url, headers=headers, json=forward_body)
    except Exception as e:
        duration = (time.monotonic() - t_start) * 1000
        audit_log(req_id, body, copilot_model, "/v1/chat/completions", None, 500, duration, str(e))
        raise HTTPException(status_code=500, detail=str(e))

    duration = (time.monotonic() - t_start) * 1000
    if resp.status_code != 200:
        logger.warning("Upstream %s returned %s: %s", endpoint, resp.status_code, resp.text[:300])
        audit_log(req_id, body, copilot_model, "/v1/chat/completions", resp.text[:500], resp.status_code, duration, f"upstream {resp.status_code}")
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    result = resp.json()
    audit_log(req_id, body, copilot_model, "/v1/chat/completions", result, 200, duration)
    return JSONResponse(content=result)


@app.post("/v1/responses")
async def responses(request: Request):
    req_id = str(uuid4())
    t_start = time.monotonic()
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        logger.warning("Invalid Responses JSON request req=%s: %s", req_id[:8], e)
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    body["_source"] = source_from_request(request)
    original_model: str = body.get("model", "")
    copilot_model = map_model_name(original_model)
    raw_forward_body = upstream_body(body, copilot_model)
    forward_body, image_sanitize_report = sanitize_responses_payload(raw_forward_body)

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
    log_request_diag(req_id, endpoint, original_model, copilot_model, request, body, forward_body)
    if image_sanitize_report.get("omitted"):
        logger.info(
            "responses req=%s image_sanitize=%s",
            req_id[:8],
            json.dumps(image_sanitize_report, ensure_ascii=False, separators=(",", ":")),
        )

    logger.debug(
        "responses req=%s model=%s→%s endpoint=%s stream=%s",
        req_id[:8], original_model, copilot_model, endpoint, is_stream,
    )

    if is_stream:
        async def generate():
            nonlocal t_start
            error_msg = None
            status = 200
            usage = {}
            auth_retry_count = 0
            busy_retry_count = 0
            target_url = url
            headers = copilot_headers
            try:
                while True:
                    async with httpx.AsyncClient(timeout=120) as client:
                        async with client.stream("POST", target_url, headers=headers, json=forward_body) as resp:
                            status = resp.status_code
                            if status != 200:
                                err = await resp.aread()
                                error_msg = err.decode()
                                if auth_retry_count == 0 and is_expired_ide_token_error(status, error_msg):
                                    auth_retry_count += 1
                                    logger.warning("Upstream %s req=%s returned expired token; refreshing and retrying once", endpoint, req_id[:8])
                                    target_url, headers = refresh_upstream_auth_after_401(req_id, endpoint, error_msg)
                                    continue
                                if busy_retry_count < UPSTREAM_BUSY_RETRIES and is_upstream_high_demand_error(status, error_msg):
                                    busy_retry_count += 1
                                    delay = upstream_busy_retry_delay(busy_retry_count)
                                    logger.warning("Upstream %s req=%s returned high demand 503; retrying same model in %.1fs (%d/%d)", endpoint, req_id[:8], delay, busy_retry_count, UPSTREAM_BUSY_RETRIES)
                                    await asyncio.sleep(delay)
                                    continue
                                logger.warning("Upstream %s req=%s returned %s: %s", endpoint, req_id[:8], status, error_msg[:200])
                                for event in synthetic_responses_error_events(status, error_msg, f"resp_{req_id.replace('-', '')[:24]}", copilot_model, req_id):
                                    yield event
                            else:
                                async for line in resp.aiter_lines():
                                    if line:
                                        update_usage_from_sse_line(line, usage)
                                        yield line + "\n"
                                    else:
                                        yield "\n"
                            break
            except Exception as e:
                error_msg = str(e)
                logger.error("Responses streaming request error req=%s: %s", req_id[:8], e)
            duration = (time.monotonic() - t_start) * 1000
            audit_log(req_id, body, copilot_model, "/v1/responses", {"stream": True, "usage": usage}, status, duration, error_msg)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            target_url = url
            headers = copilot_headers
            resp = await client.post(target_url, headers=headers, json=forward_body)
            if is_expired_ide_token_error(resp.status_code, resp.text):
                logger.warning("Upstream %s req=%s returned expired token; refreshing and retrying once", endpoint, req_id[:8])
                target_url, headers = refresh_upstream_auth_after_401(req_id, endpoint, resp.text)
                resp = await client.post(target_url, headers=headers, json=forward_body)
            busy_retry_count = 0
            while busy_retry_count < UPSTREAM_BUSY_RETRIES and is_upstream_high_demand_error(resp.status_code, resp.text):
                busy_retry_count += 1
                delay = upstream_busy_retry_delay(busy_retry_count)
                logger.warning("Upstream %s req=%s returned high demand 503; retrying same model in %.1fs (%d/%d)", endpoint, req_id[:8], delay, busy_retry_count, UPSTREAM_BUSY_RETRIES)
                await asyncio.sleep(delay)
                resp = await client.post(target_url, headers=headers, json=forward_body)
    except Exception as e:
        duration = (time.monotonic() - t_start) * 1000
        audit_log(req_id, body, copilot_model, "/v1/responses", None, 500, duration, str(e))
        raise HTTPException(status_code=500, detail=str(e))

    duration = (time.monotonic() - t_start) * 1000
    if resp.status_code != 200:
        logger.warning("Upstream %s req=%s returned %s: %s", endpoint, req_id[:8], resp.status_code, resp.text[:300])
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

    body["_source"] = source_from_request(request)
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
        # Update model in body to Copilot format while stripping proxy-internal fields.
        forward_body = upstream_body(body, copilot_model)
        # Remove fields sent by Anthropic/Claude Code that Copilot does not support
        forward_body.pop("betas", None)
        forward_body.pop("context_management", None)
        forward_body.pop("output_config", None)
    else:
        # ── Non-Claude models: convert to OpenAI format ─────────────────
        endpoint = "/chat/completions"
        forward_body = anthropic_to_openai(body, copilot_model)

    is_stream = forward_body.get("stream", False)
    log_request_diag(req_id, endpoint, original_model, copilot_model, request, body, forward_body)
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
            usage = {}
            status = 200
            auth_retry_count = 0
            busy_retry_count = 0
            target_url = url
            headers = copilot_headers
            try:
                while True:
                    async with httpx.AsyncClient(timeout=120) as client:
                        async with client.stream("POST", target_url, headers=headers, json=forward_body) as resp:
                            status = resp.status_code
                            if status != 200:
                                err = await resp.aread()
                                error_msg = err.decode()
                                if auth_retry_count == 0 and is_expired_ide_token_error(status, error_msg):
                                    auth_retry_count += 1
                                    logger.warning("Upstream %s req=%s returned expired token; refreshing and retrying once", endpoint, req_id[:8])
                                    target_url, headers = refresh_upstream_auth_after_401(req_id, endpoint, error_msg)
                                    continue
                                if busy_retry_count < UPSTREAM_BUSY_RETRIES and is_upstream_high_demand_error(status, error_msg):
                                    busy_retry_count += 1
                                    delay = upstream_busy_retry_delay(busy_retry_count)
                                    logger.warning("Upstream %s req=%s returned high demand 503; retrying same model in %.1fs (%d/%d)", endpoint, req_id[:8], delay, busy_retry_count, UPSTREAM_BUSY_RETRIES)
                                    await asyncio.sleep(delay)
                                    continue
                                logger.warning("Upstream %s req=%s returned %s: %s", endpoint, req_id[:8], status, error_msg[:200])
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
                                                if d.get("usage"):
                                                    usage.update(_extract_usage(d))
                                                if d.get("message", {}).get("usage"):
                                                    usage.update(_extract_usage(d.get("message", {})))
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
                            break
            except Exception as e:
                error_msg = str(e)
                logger.error("Streaming request error req=%s: %s", req_id[:8], e)

            duration = (time.monotonic() - t_start) * 1000
            audit_log(req_id, body, copilot_model, endpoint,
                      {"streamed_text": "".join(collected_text)[:2000], "usage": usage},
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
                target_url = url
                headers = copilot_headers
                resp = await client.post(target_url, headers=headers, json=forward_body)
                if is_expired_ide_token_error(resp.status_code, resp.text):
                    logger.warning("Upstream %s req=%s returned expired token; refreshing and retrying once", endpoint, req_id[:8])
                    target_url, headers = refresh_upstream_auth_after_401(req_id, endpoint, resp.text)
                    resp = await client.post(target_url, headers=headers, json=forward_body)
                busy_retry_count = 0
                while busy_retry_count < UPSTREAM_BUSY_RETRIES and is_upstream_high_demand_error(resp.status_code, resp.text):
                    busy_retry_count += 1
                    delay = upstream_busy_retry_delay(busy_retry_count)
                    logger.warning("Upstream %s req=%s returned high demand 503; retrying same model in %.1fs (%d/%d)", endpoint, req_id[:8], delay, busy_retry_count, UPSTREAM_BUSY_RETRIES)
                    await asyncio.sleep(delay)
                    resp = await client.post(target_url, headers=headers, json=forward_body)
        except Exception as e:
            duration = (time.monotonic() - t_start) * 1000
            audit_log(req_id, body, copilot_model, endpoint, None, 500, duration, str(e))
            raise HTTPException(status_code=500, detail=str(e))

        duration = (time.monotonic() - t_start) * 1000

        if resp.status_code != 200:
            logger.warning("Upstream %s req=%s returned %s: %s", endpoint, req_id[:8], resp.status_code, resp.text[:300])
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

    @app.get("/admin/login", response_class=HTMLResponse)
    async def admin_login_page(error: str = ""):
        err = '<div class="error">账号或密码不正确</div>' if error else ""
        return HTMLResponse(f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Copilot 用量后台登录</title><style>
        body{{font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans SC',sans-serif;background:#f7f8fb;color:#111827;display:grid;place-items:center;min-height:100vh;margin:0}}
        .card{{width:min(420px,92vw);background:#fff;border:1px solid #e5e7eb;border-radius:22px;box-shadow:0 22px 70px rgba(15,23,42,.10);padding:34px}}
        h1{{margin:0 0 6px;font-size:26px}}p{{margin:0 0 24px;color:#6b7280}}label{{display:block;margin:14px 0 6px;font-weight:700}}input{{width:100%;box-sizing:border-box;border:1px solid #d1d5db;border-radius:12px;padding:12px 14px;font-size:15px}}button{{width:100%;margin-top:22px;border:0;border-radius:12px;background:#2563eb;color:#fff;font-weight:800;padding:13px 16px;font-size:15px;cursor:pointer}}.error{{background:#fef2f2;color:#b91c1c;border:1px solid #fecaca;padding:10px 12px;border-radius:12px;margin-bottom:14px}}
        </style></head><body><main class="card"><h1>Copilot 用量后台</h1><p>请输入账号密码查看统计</p>{err}<form method="post" action="{ADMIN_PUBLIC_PREFIX}/admin/login"><label>账号</label><input name="username" autocomplete="username" autofocus><label>密码</label><input name="password" type="password" autocomplete="current-password"><button type="submit">登录</button></form></main></body></html>""")

    @app.post("/admin/login")
    async def admin_login(request: Request):
        body = (await request.body()).decode()
        form = parse_qs(body)
        username = form.get("username", [""])[0]
        password = form.get("password", [""])[0]
        if not verify_admin_credentials(username, password):
            return RedirectResponse(f"{ADMIN_PUBLIC_PREFIX}/admin/login?error=1", status_code=303)
        resp = RedirectResponse(f"{ADMIN_PUBLIC_PREFIX}/admin", status_code=303)
        resp.set_cookie(ADMIN_COOKIE_NAME, create_admin_session_token(username), max_age=ADMIN_SESSION_MAX_AGE, httponly=True, secure=True, samesite="lax")
        return resp

    @app.get("/admin/logout")
    async def admin_logout():
        resp = RedirectResponse(f"{ADMIN_PUBLIC_PREFIX}/admin/login", status_code=303)
        resp.delete_cookie(ADMIN_COOKIE_NAME)
        return resp

    @app.get("/admin/stats")
    async def admin_stats(request: Request):
        if not require_admin(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return build_usage_stats(load_all_audit_requests())

    @app.get("/admin/ip/{ip:path}/stats")
    async def admin_ip_stats(ip: str, request: Request):
        if not require_admin(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return build_ip_usage_stats(load_all_audit_requests(), ip)

    @app.get("/admin/ip/{ip:path}", response_class=HTMLResponse)
    async def admin_ip_dashboard(ip: str, request: Request):
        if not require_admin(request):
            return RedirectResponse(f"{ADMIN_PUBLIC_PREFIX}/admin/login", status_code=303)
        stats = build_ip_usage_stats(load_all_audit_requests(), ip)
        model_rows = "".join(f"<tr><td>{html_escape(m['name'])}</td><td>{m['count']}</td></tr>" for m in stats["models"])
        token_rows = "".join(f"<tr><td>{html_escape(k)}</td><td>{int(v):,}</td></tr>" for k, v in stats["tokens"].items()) or '<tr><td colspan="2">暂无 token usage 数据</td></tr>'
        status_rows = "".join(f"<tr><td>{html_escape(k)}</td><td>{v}</td></tr>" for k, v in stats["status"].items())
        daily_labels = [row["date"] for row in stats["daily_rows"][-14:]]
        daily_values = [row["requests"] for row in stats["daily_rows"][-14:]]
        daily_token_values = [sum(row["tokens"].values()) for row in stats["daily_rows"][-14:]]
        daily_rows = "".join(
            f"<tr><td>{html_escape(row['date'])}</td><td>{row['requests']}</td><td>{row['ok']}</td><td>{row['errors']}</td><td>{sum(row['tokens'].values()):,}</td><td>{html_escape(', '.join(f'{k}: {v}' for k, v in row['models'].items()))}</td></tr>"
            for row in stats["daily_rows"]
        ) or '<tr><td colspan="6">暂无每日数据</td></tr>'
        token_total = sum(stats["tokens"].values())
        request_rows = "".join(
            f"<tr><td>{html_escape(row['timestamp'])}</td><td>{html_escape(row['model'])}</td><td>{row['status']}</td><td>{row['input_tokens']:,}</td><td>{row['output_tokens']:,}</td><td>{row['cache_read_input_tokens']:,}</td><td>{row['cache_creation_input_tokens']:,}</td><td>{round(float(row.get('duration_ms') or 0)):,}</td></tr>"
            for row in sorted(stats["request_rows"], key=lambda r: r["input_tokens"], reverse=True)[:100]
        ) or '<tr><td colspan="8">暂无单请求 token 数据</td></tr>'
        metrics = stats["token_metrics"]
        return HTMLResponse(f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>IP 消耗 - {html_escape(ip)}</title><script src="https://cdn.jsdelivr.net/npm/chart.js"></script><style>
        body{{font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans SC',sans-serif;background:#f7f8fb;color:#111827;margin:0}}.wrap{{max-width:1280px;margin:0 auto;padding:28px}}header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:22px}}a{{color:#2563eb;text-decoration:none}}h1{{margin:0;font-size:28px}}.muted{{color:#6b7280}}.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}}.card{{background:#fff;border:1px solid #e5e7eb;border-radius:18px;box-shadow:0 12px 35px rgba(15,23,42,.06);padding:18px}}.num{{font-size:30px;font-weight:850;margin-top:8px}}.two{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px}}.full{{margin-top:16px}}table{{width:100%;border-collapse:collapse}}td,th{{padding:10px;border-bottom:1px solid #eef2f7;text-align:left;vertical-align:top}}th{{color:#6b7280;font-size:13px}}@media(max-width:800px){{.grid,.two{{grid-template-columns:1fr}}header{{display:block}}}}
        </style></head><body><div class="wrap"><header><div><h1>IP 专属消耗：{html_escape(ip)}</h1><div class="muted">生成时间：{html_escape(stats['generated_at'])}</div></div><div><a href="{ADMIN_PUBLIC_PREFIX}/admin">返回总览</a></div></header><section class="grid"><div class="card"><div class="muted">请求数</div><div class="num">{stats['total']['requests']:,}</div></div><div class="card"><div class="muted">Input Token 总量</div><div class="num">{int(stats['tokens'].get('input_tokens',0)):,}</div></div><div class="card"><div class="muted">单请求最大 Input</div><div class="num">{metrics['max_input_tokens']:,}</div></div><div class="card"><div class="muted">单请求平均 Input</div><div class="num">{metrics['avg_input_tokens']:,}</div></div></section><section class="two"><div class="card"><h2>每日请求量</h2><canvas id="dailyChart" height="130"></canvas></div><div class="card"><h2>每日 Token</h2><canvas id="dailyTokenChart" height="130"></canvas></div></section><section class="full card"><h2>每日用量明细</h2><table><tr><th>日期</th><th>请求数</th><th>成功</th><th>错误</th><th>Token</th><th>使用模型</th></tr>{daily_rows}</table></section><section class="full card"><h2>单请求 Token 明细</h2><table><tr><th>时间</th><th>模型</th><th>状态</th><th>Input</th><th>Output</th><th>Cache Read</th><th>Cache Create</th><th>耗时 ms</th></tr>{request_rows}</table></section><section class="two"><div class="card"><h2>使用模型汇总</h2><table><tr><th>模型</th><th>请求数</th></tr>{model_rows}</table></div><div class="card"><h2>Token 消耗汇总</h2><table><tr><th>字段</th><th>数量</th></tr>{token_rows}</table></div></section><section class="two"><div class="card"><h2>状态码</h2><table><tr><th>状态</th><th>数量</th></tr>{status_rows}</table></div><div class="card"><h2>延迟</h2><table><tr><th>指标</th><th>毫秒</th></tr><tr><td>平均</td><td>{stats['latency']['avg_ms']:,}</td></tr><tr><td>P95</td><td>{stats['latency']['p95_ms']:,}</td></tr><tr><td>最大</td><td>{stats['latency']['max_ms']:,}</td></tr></table><p class="muted">Token 仅统计上游响应返回 usage 的请求；没有 usage 的历史/流式请求不会被计入 token 字段。</p></div></section></div><script>new Chart(document.getElementById('dailyChart'),{{type:'bar',data:{{labels:{json.dumps(daily_labels, ensure_ascii=False)},datasets:[{{label:'请求数',data:{json.dumps(daily_values)},backgroundColor:'#2563eb',borderRadius:8}}]}},options:{{plugins:{{legend:{{display:false}}}},scales:{{y:{{beginAtZero:true}}}}}}}});new Chart(document.getElementById('dailyTokenChart'),{{type:'bar',data:{{labels:{json.dumps(daily_labels, ensure_ascii=False)},datasets:[{{label:'Token',data:{json.dumps(daily_token_values)},backgroundColor:'#10b981',borderRadius:8}}]}},options:{{plugins:{{legend:{{display:false}}}},scales:{{y:{{beginAtZero:true}}}}}}}});</script></body></html>""")

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_dashboard(request: Request):
        if not require_admin(request):
            return RedirectResponse(f"{ADMIN_PUBLIC_PREFIX}/admin/login", status_code=303)
        stats = build_usage_stats(load_all_audit_requests())
        model_rows = "".join(f"<tr><td>{html_escape(m['name'])}</td><td>{m['count']}</td></tr>" for m in stats["models"][:12])
        source_rows = "".join(f"<tr><td>{html_escape(s['name'])}</td><td>{s['count']}</td></tr>" for s in stats["sources"][:12])
        source_ip_rows = "".join(
            f"<tr><td><a href='{ADMIN_PUBLIC_PREFIX}/admin/ip/{html_escape(row['ip'])}'>{html_escape(row['ip'])}</a></td><td>{row['requests']}</td><td>{sum(row['tokens'].values()):,}</td></tr>"
            for row in stats["ip_rows"][:12]
        ) or '<tr><td colspan="3">暂无 IP 数据</td></tr>'
        endpoint_rows = "".join(f"<tr><td>{html_escape(k)}</td><td>{v}</td></tr>" for k, v in stats["endpoints"].items())
        status_rows = "".join(f"<tr><td>{html_escape(k)}</td><td>{v}</td></tr>" for k, v in stats["status"].items())
        token_rows = "".join(f"<tr><td>{html_escape(k)}</td><td>{int(v):,}</td></tr>" for k, v in stats["tokens"].items()) or '<tr><td colspan="2">暂无 token usage 数据</td></tr>'
        daily_labels = list(stats["daily"].keys())[-14:]
        daily_values = [stats["daily"][k] for k in daily_labels]
        return HTMLResponse(f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Copilot 用量后台</title><script src="https://cdn.jsdelivr.net/npm/chart.js"></script><style>
        body{{font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans SC',sans-serif;background:#f7f8fb;color:#111827;margin:0}}.wrap{{max-width:1180px;margin:0 auto;padding:28px}}header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:22px}}h1{{margin:0;font-size:28px}}a{{color:#2563eb;text-decoration:none}}.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}}.card{{background:#fff;border:1px solid #e5e7eb;border-radius:18px;box-shadow:0 12px 35px rgba(15,23,42,.06);padding:18px}}.num{{font-size:30px;font-weight:850;margin-top:8px}}.muted{{color:#6b7280}}.two{{display:grid;grid-template-columns:1.3fr 1fr;gap:16px;margin-top:16px}}table{{width:100%;border-collapse:collapse}}td,th{{padding:10px;border-bottom:1px solid #eef2f7;text-align:left}}th{{color:#6b7280;font-size:13px}}@media(max-width:800px){{.grid,.two{{grid-template-columns:1fr}}header{{display:block}}}}
        </style></head><body><div class="wrap"><header><div><h1>Copilot 用量后台</h1><div class="muted">生成时间：{html_escape(stats['generated_at'])}</div></div><div><a href="{ADMIN_PUBLIC_PREFIX}/admin/logout">退出登录</a></div></header><section class="grid"><div class="card"><div class="muted">累计请求</div><div class="num">{stats['total']['requests']:,}</div></div><div class="card"><div class="muted">最近 24 小时</div><div class="num">{stats['last_24h']['requests']:,}</div></div><div class="card"><div class="muted">最近 7 天</div><div class="num">{stats['last_7d']['requests']:,}</div></div><div class="card"><div class="muted">成功率</div><div class="num">{stats['total']['success_rate']}%</div></div></section><section class="two"><div class="card"><h2>近 14 天请求量</h2><canvas id="dailyChart" height="120"></canvas></div><div class="card"><h2>延迟</h2><table><tr><th>指标</th><th>毫秒</th></tr><tr><td>平均</td><td>{stats['latency']['avg_ms']:,}</td></tr><tr><td>P95</td><td>{stats['latency']['p95_ms']:,}</td></tr><tr><td>最大</td><td>{stats['latency']['max_ms']:,}</td></tr></table></div></section><section class="two"><div class="card"><h2>Source 来源</h2><table><tr><th>来源</th><th>请求数</th></tr>{source_rows}</table></div><div class="card"><h2>Source IP</h2><table><tr><th>IP</th><th>请求数</th><th>Token</th></tr>{source_ip_rows}</table></div></section><section class="two"><div class="card"><h2>模型排行</h2><table><tr><th>模型</th><th>请求数</th></tr>{model_rows}</table></div><div class="card"><h2>Token 汇总</h2><table><tr><th>字段</th><th>数量</th></tr>{token_rows}</table></div></section><section class="two"><div class="card"><h2>接口分布</h2><table><tr><th>接口</th><th>请求数</th></tr>{endpoint_rows}</table></div><div class="card"><h2>状态码</h2><table><tr><th>状态</th><th>数量</th></tr>{status_rows}</table></div></section></div><script>new Chart(document.getElementById('dailyChart'),{{type:'bar',data:{{labels:{json.dumps(daily_labels, ensure_ascii=False)},datasets:[{{label:'请求数',data:{json.dumps(daily_values)},backgroundColor:'#2563eb',borderRadius:8}}]}},options:{{plugins:{{legend:{{display:false}}}},scales:{{y:{{beginAtZero:true}}}}}}}});</script></body></html>""")




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
