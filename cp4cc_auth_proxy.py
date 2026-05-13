#!/usr/bin/env python3
"""Add a shared-secret gate in front of cp4cc before exposing it publicly.

The upstream project intentionally ignores ANTHROPIC_AUTH_TOKEN. That is OK for
localhost, but unsafe on a public domain. This wrapper imports cp4cc.app and adds
a middleware that requires Authorization: Bearer <CP4CC_PUBLIC_TOKEN> for all
endpoints except /health.
"""
import os
import sys
from fastapi import Request
from fastapi.responses import JSONResponse

# Make cp4cc parse known-safe argv when imported.
sys.argv = [sys.argv[0], "--port", os.environ.get("CP4CC_PORT", "8092")]
import cp4cc  # noqa: E402

PUBLIC_TOKEN = os.environ.get("CP4CC_PUBLIC_TOKEN", "")
if not PUBLIC_TOKEN:
    raise RuntimeError("CP4CC_PUBLIC_TOKEN is required")

app = cp4cc.app

@app.middleware("http")
async def require_public_token(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    x_api_key = request.headers.get("x-api-key", "")
    expected = f"Bearer {PUBLIC_TOKEN}"
    # Claude Code apiKeyHelper sends the helper value as BOTH
    #   X-Api-Key: <value>
    # and
    #   Authorization: Bearer <value>
    # depending on version/request path. Accept either so Claude Code works.
    if auth != expected and x_api_key != PUBLIC_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)
