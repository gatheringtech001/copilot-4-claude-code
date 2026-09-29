import asyncio
import importlib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def proxy(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["cp4cc.py", "--fast"])
    sys.modules.pop("cp4cc", None)
    module = importlib.import_module("cp4cc")
    monkeypatch.setattr(module, "RESPONSES_BINDINGS_FILE", str(tmp_path / "bindings.json"))
    monkeypatch.setattr(module, "select_api_key_info_for_responses_body", lambda _: {
        "token": "test", "expires_at": 9999999999, "endpoints": {"api": "https://example.invalid"},
    })
    audits = []
    monkeypatch.setattr(module, "audit_log", lambda *args: audits.append({
        "id": args[0], "body": args[4], "status": args[5], "error": args[7],
    }))
    return module, audits


def sse(event, **fields):
    return f"event: {event}\ndata: {json.dumps({'type': event, **fields})}\n\n".encode()


CREATED = sse("response.created", sequence_number=5, response={"id": "resp_upstream", "status": "in_progress"})
COMPLETED = sse("response.completed", sequence_number=6, response={
    "id": "resp_upstream", "status": "completed",
    "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
})


class UpstreamStream(httpx.AsyncByteStream):
    def __init__(self, chunks, tail=None):
        self.chunks = chunks
        self.tail = tail
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.tail:
            await self.tail()

    async def aclose(self):
        self.closed = True


def install_upstream(monkeypatch, module, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))


def post(module):
    with TestClient(module.app) as client:
        return client.post("/v1/responses", json={"model": "gpt-6-sol", "input": "test", "stream": True})


def events(response):
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]


def test_completed_stream_does_not_wait_for_upstream_eof(proxy, monkeypatch):
    module, audits = proxy

    async def hang():
        pytest.fail("The proxy read past response.completed")

    upstream = UpstreamStream([CREATED, COMPLETED], hang)
    install_upstream(monkeypatch, module, lambda _: httpx.Response(200, stream=upstream))
    response = post(module)
    assert response.content == CREATED + COMPLETED
    assert response.headers["x-request-id"] == audits[0]["id"]
    assert upstream.closed
    assert len(audits) == 1
    assert audits[0]["status"] == 200
    assert audits[0]["body"]["outcome"] == "completed"
    assert audits[0]["body"]["usage"]["total_tokens"] == 12
    assert audits[0]["body"]["first_event_ms"] is not None


@pytest.mark.parametrize("case,status", [
    ("timeout", 504), ("partial_timeout", 504), ("eof", 502),
    ("bad_json", 502), ("partial_event", 502), ("done_without_completed", 502),
])
def test_failed_stream_has_terminal_failure_and_nonempty_audit(proxy, monkeypatch, case, status):
    module, audits = proxy

    async def timeout():
        raise httpx.ReadTimeout("")

    chunks = [] if case == "timeout" else [CREATED]
    if case == "bad_json":
        chunks.append(b"data: {bad}\n\n")
    elif case == "partial_event":
        chunks.append(b'data: {"type":"response.completed"}')
    elif case == "done_without_completed":
        chunks.append(b"data: [DONE]\n\n")
    upstream = UpstreamStream(chunks, timeout if "timeout" in case else None)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=upstream)

    install_upstream(monkeypatch, module, handler)
    result = events(post(module))
    assert result[-1]["type"] == "response.failed"
    assert result[-1]["response"]["status"] == "failed"
    assert not any(event["type"] == "response.completed" for event in result)
    assert result[-1]["sequence_number"] == (0 if case == "timeout" else 6)
    if case != "timeout":
        assert result[-1]["response"]["id"] == "resp_upstream"
    assert audits[0]["status"] == status
    assert audits[0]["error"]
    assert audits[0]["body"]["terminal_event"] == "response.failed"
    assert len(calls) == 1
    assert upstream.closed


@pytest.mark.parametrize("event", ["response.failed", "response.incomplete", "error"])
def test_upstream_terminal_error_is_preserved_not_counted_as_success(proxy, monkeypatch, event):
    module, audits = proxy
    raw = sse(event, response={"id": "resp_upstream", "error": {"message": "unavailable"}})
    upstream = UpstreamStream([CREATED, raw])
    install_upstream(monkeypatch, module, lambda _: httpx.Response(200, stream=upstream))
    assert post(module).content == CREATED + raw
    assert audits[0]["status"] == 502
    assert audits[0]["error"]
    assert len(audits) == 1


def test_multiline_sse_and_comments_preserve_framing(proxy, monkeypatch):
    module, audits = proxy
    raw = b': ping\n\nevent: response.completed\ndata: {"type":"response.completed",\ndata: "response":{"id":"resp_multi","status":"completed"}}\n\n'
    install_upstream(monkeypatch, module, lambda _: httpx.Response(200, stream=UpstreamStream([raw[:25], raw[25:]])))
    assert post(module).content == raw
    assert audits[0]["status"] == 200


def test_terminal_http_error_is_not_synthetic_success(proxy, monkeypatch):
    module, audits = proxy
    install_upstream(monkeypatch, module, lambda _: httpx.Response(413, json={"error": {"message": "too large"}}))
    response = post(module)
    assert [e["type"] for e in events(response)] == ["response.failed"]
    assert "too large" in response.text
    assert audits[0]["status"] == 413


def test_retries_share_total_deadline(proxy, monkeypatch):
    module, audits = proxy
    monkeypatch.setattr(module, "RESPONSES_TOTAL_TIMEOUT", 0.1)
    monkeypatch.setattr(module, "UPSTREAM_BUSY_BACKOFF_SECONDS", 1)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(502, text="bad gateway")

    install_upstream(monkeypatch, module, handler)
    start = time.monotonic()
    assert events(post(module))[-1]["type"] == "response.failed"
    assert time.monotonic() - start < 1
    assert audits[0]["status"] == 504
    assert len(calls) == 1


def test_first_event_deadline_expires_even_when_upstream_sends_comments(proxy, monkeypatch):
    module, audits = proxy
    monkeypatch.setattr(module, "RESPONSES_FIRST_EVENT_TIMEOUT", 0.05)

    class Heartbeats(UpstreamStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.01)
                yield b": upstream ping\n\n"

    upstream = Heartbeats([])
    install_upstream(monkeypatch, module, lambda _: httpx.Response(200, stream=upstream))
    assert events(post(module))[-1]["type"] == "response.failed"
    assert audits[0]["status"] == 504
    assert audits[0]["body"]["first_event_ms"] is None
    assert upstream.closed


def test_continuous_output_is_bounded_by_total_deadline(proxy, monkeypatch):
    module, audits = proxy
    monkeypatch.setattr(module, "RESPONSES_TOTAL_TIMEOUT", 0.1)

    class Deltas(UpstreamStream):
        async def __aiter__(self):
            yield CREATED
            while True:
                await asyncio.sleep(0.01)
                yield sse("response.output_text.delta", delta="x")

    upstream = Deltas([])
    install_upstream(monkeypatch, module, lambda _: httpx.Response(200, stream=upstream))
    result = events(post(module))
    assert any(e["type"] == "response.output_text.delta" for e in result)
    assert result[-1]["type"] == "response.failed"
    assert audits[0]["status"] == 504
    assert upstream.closed


def test_refreshed_token_is_used_for_new_bindings(proxy, monkeypatch):
    module, audits = proxy
    calls, bindings = [], []
    fresh = {"token": "fresh", "expires_at": 9999999999, "endpoints": {"api": "https://new.invalid"}}
    monkeypatch.setattr(module, "refresh_responses_key", lambda _: fresh)
    monkeypatch.setattr(module, "bind_encrypted_content_hashes", lambda hashes, info: bindings.append(info))

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(401, text="IDE token expired: unauthorized: token expired")
        return httpx.Response(200, stream=UpstreamStream([
            sse("response.completed", response={"status": "completed", "output": [{"encrypted_content": "cipher"}]}),
        ]))

    install_upstream(monkeypatch, module, handler)
    assert events(post(module))[-1]["type"] == "response.completed"
    assert calls[1].headers["authorization"] == "Bearer fresh"
    assert calls[1].url.host == "new.invalid"
    assert bindings == [fresh]
    assert audits[0]["body"]["attempts"] == 2


def test_nonstream_request_has_same_total_budget(proxy, monkeypatch):
    module, audits = proxy
    monkeypatch.setattr(module, "RESPONSES_TOTAL_TIMEOUT", 0.05)

    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"status": "completed"})

    install_upstream(monkeypatch, module, handler)
    with TestClient(module.app) as client:
        response = client.post("/v1/responses", json={"model": "gpt-6-sol", "input": "test"})
    assert response.status_code == 504
    assert audits[0]["status"] == 504
    assert len(audits) == 1


async def invoke_asgi(module, send, spec_version="2.4", disconnected=None):
    body = json.dumps({"model": "gpt-6-sol", "input": "test", "stream": True}).encode()
    sent_body = False

    async def receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": body, "more_body": False}
        if disconnected is not None:
            await disconnected.wait()
            return {"type": "http.disconnect"}
        await asyncio.Event().wait()

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1", "scheme": "http", "method": "POST",
        "path": "/v1/responses", "raw_path": b"/v1/responses", "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1234), "server": ("test", 80),
    }
    await module.app(scope, receive, send)


@pytest.mark.anyio
async def test_cancelled_stream_audited_and_upstream_closed(proxy, monkeypatch):
    module, audits = proxy
    seen = asyncio.Event()

    async def hang():
        await asyncio.Event().wait()

    upstream = UpstreamStream([CREATED], hang)
    install_upstream(monkeypatch, module, lambda _: httpx.Response(200, stream=upstream))

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body") == CREATED:
            assert len(module._inflight_requests) == 1
            seen.set()

    task = asyncio.create_task(invoke_asgi(module, send))
    await asyncio.wait_for(seen.wait(), 2)
    assert len(module._inflight_requests) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(audits) == 1
    assert audits[0]["status"] == 499
    assert audits[0]["body"]["outcome"] == "cancelled"
    assert not module._inflight_requests
    assert upstream.closed


@pytest.mark.anyio
async def test_asgi_23_disconnect_cleans_up_and_audits(proxy, monkeypatch):
    module, audits = proxy
    disconnected = asyncio.Event()

    async def hang():
        await asyncio.Event().wait()

    upstream = UpstreamStream([CREATED], hang)
    install_upstream(monkeypatch, module, lambda _: httpx.Response(200, stream=upstream))

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body") == CREATED:
            assert len(module._inflight_requests) == 1
            disconnected.set()

    await asyncio.wait_for(invoke_asgi(module, send, "2.3", disconnected), 2)
    assert len(audits) == 1
    assert audits[0]["status"] == 499
    assert upstream.closed
    assert not module._inflight_requests


@pytest.mark.anyio
async def test_disconnect_after_terminal_event_still_audits_completion(proxy, monkeypatch):
    module, audits = proxy
    upstream = UpstreamStream([CREATED, COMPLETED])
    install_upstream(monkeypatch, module, lambda _: httpx.Response(200, stream=upstream))

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body") == COMPLETED:
            raise OSError("client closed after reading completed event")

    with pytest.raises(ClientDisconnect):
        await invoke_asgi(module, send)
    assert len(audits) == 1
    assert audits[0]["status"] == 200
    assert not module._inflight_requests
    assert upstream.closed


def test_slow_sync_auth_does_not_block_health(proxy, monkeypatch):
    module, audits = proxy
    entered = threading.Event()
    release = threading.Event()

    def auth(_):
        entered.set()
        assert release.wait(2)
        return {"token": "test", "endpoints": {"api": "https://example.invalid"}}

    monkeypatch.setattr(module, "select_api_key_info_for_responses_body", auth)
    install_upstream(monkeypatch, module, lambda _: httpx.Response(200, stream=UpstreamStream([COMPLETED])))
    with TestClient(module.app) as client, ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(client.post, "/v1/responses", json={"model": "gpt-6-sol", "stream": True})
        try:
            assert entered.wait(1)
            start = time.monotonic()
            assert client.get("/health").status_code == 200
            assert time.monotonic() - start < 0.5
        finally:
            release.set()
        assert pending.result(timeout=2).status_code == 200
