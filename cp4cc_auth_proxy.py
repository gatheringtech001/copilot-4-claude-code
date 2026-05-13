#!/usr/bin/env python3
"""Add a shared-secret gate in front of cp4cc before exposing it publicly.

The upstream project intentionally ignores ANTHROPIC_AUTH_TOKEN. That is OK for
localhost, but unsafe on a public domain. This wrapper imports cp4cc.app and adds
a middleware that requires Authorization: Bearer <token> or X-Api-Key: <token>
for all endpoints except /health.

Supported environment variables:
- CP4CC_PUBLIC_TOKEN: primary token, kept for backward compatibility.
- CP4CC_EXTRA_PUBLIC_TOKENS: optional comma/newline/space separated extra tokens.
"""
import os
import re
import sys
from fastapi import Request
from fastapi.responses import JSONResponse

# Make cp4cc parse known-safe argv when imported.
sys.argv = [sys.argv[0], "--port", os.environ.get("CP4CC_PORT", "8092")]
import cp4cc  # noqa: E402


def _split_tokens(value: str) -> set[str]:
    return {token.strip() for token in re.split(r"[,\s]+", value or "") if token.strip()}


PUBLIC_TOKENS = _split_tokens(os.environ.get("CP4CC_PUBLIC_TOKEN", ""))
PUBLIC_TOKENS |= _split_tokens(os.environ.get("CP4CC_EXTRA_PUBLIC_TOKENS", ""))

if not PUBLIC_TOKENS:
    raise RuntimeError("CP4CC_PUBLIC_TOKEN or CP4CC_EXTRA_PUBLIC_TOKENS is required")

app = cp4cc.app


@app.middleware("http")
async def require_public_token(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)

    auth = request.headers.get("authorization", "")
    x_api_key = request.headers.get("x-api-key", "")

    bearer_token = ""
    if auth.lower().startswith("bearer "):
        bearer_token = auth[7:].strip()

    # Claude Code apiKeyHelper may send the helper value as either:
    #   X-Api-Key: <value>
    # or
    #   Authorization: Bearer <value>
    # depending on version/request path. Accept either.
    if bearer_token not in PUBLIC_TOKENS and x_api_key not in PUBLIC_TOKENS:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    return await call_next(request)
