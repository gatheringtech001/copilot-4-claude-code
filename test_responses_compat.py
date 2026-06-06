import importlib
import sys

import pytest


@pytest.fixture()
def cp4cc(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["cp4cc.py", "--fast"])
    if "cp4cc" in sys.modules:
        del sys.modules["cp4cc"]
    return importlib.import_module("cp4cc")


def test_responses_to_openai_chat_converts_string_input(cp4cc):
    body = {
        "model": "gpt-5.5",
        "input": "Say OK only",
        "max_output_tokens": 20,
        "stream": False,
    }

    result = cp4cc.responses_to_openai_chat(body, "gpt-5.5")

    assert result == {
        "model": "gpt-5.5",
        "messages": [{"role": "user", "content": "Say OK only"}],
        "max_completion_tokens": 20,
        "stream": False,
    }


def test_openai_chat_to_responses_extracts_output_text(cp4cc):
    response = {
        "id": "chatcmpl_123",
        "model": "gpt-5.5",
        "choices": [
            {"message": {"content": "OK"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }

    result = cp4cc.openai_chat_to_responses(response, "resp_123")

    assert result["id"] == "resp_123"
    assert result["object"] == "response"
    assert result["model"] == "gpt-5.5"
    assert result["status"] == "completed"
    assert result["output_text"] == "OK"
    assert result["output"][0]["content"][0]["text"] == "OK"
    assert result["usage"] == {
        "input_tokens": 3,
        "output_tokens": 1,
        "total_tokens": 4,
    }


def test_forward_body_strips_internal_source_before_upstream(cp4cc):
    body = {
        "model": "claude-sonnet-4.6",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "_source": {"client": "test", "ip": "1.1.1.1"},
    }

    result = cp4cc.upstream_body(body, "claude-sonnet-4.6")

    assert "_source" not in result
    assert result == {
        "model": "claude-sonnet-4.6",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }


def test_forward_body_keeps_original_body_source_for_audit(cp4cc):
    body = {"model": "gpt-5.5", "input": "hi", "_source": {"client": "test"}}

    result = cp4cc.upstream_body(body, "gpt-5.5")

    assert body["_source"] == {"client": "test"}
    assert "_source" not in result


def test_update_usage_from_sse_line_extracts_responses_usage(cp4cc):
    usage = {}
    line = 'data: {"type":"response.completed","response":{"usage":{"input_tokens":12,"output_tokens":3,"total_tokens":15}}}'

    cp4cc.update_usage_from_sse_line(line, usage)

    assert usage == {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}


def test_update_usage_from_sse_line_ignores_invalid_json_without_raising(cp4cc):
    usage = {"input_tokens": 1}

    cp4cc.update_usage_from_sse_line("data: {not-json", usage)

    assert usage == {"input_tokens": 1}


def test_app_exposes_openai_chat_completions_endpoint(cp4cc):
    routes = {
        (route.path, tuple(sorted(route.methods)))
        for route in cp4cc.app.routes
        if hasattr(route, "methods")
    }

    assert ("/v1/chat/completions", ("POST",)) in routes


def test_normalize_chat_completion_request_maps_model_and_strips_internal_fields(cp4cc):
    body = {
        "model": "gpt-5.5",
        "messages": [{"role": "user", "content": "Say OK only"}],
        "max_tokens": 20,
        "stream": False,
        "_source": {"client": "test"},
    }

    result = cp4cc.normalize_chat_completion_request(body, "gpt-5.5")

    assert result == {
        "model": "gpt-5.5",
        "messages": [{"role": "user", "content": "Say OK only"}],
        "max_completion_tokens": 20,
        "stream": False,
    }
    assert body["_source"] == {"client": "test"}


def test_sanitize_responses_payload_omits_oversized_data_image(cp4cc):
    small_image = "data:image/png;base64," + ("A" * 1024)
    big_image = "data:image/png;base64," + ("A" * (cp4cc.IMAGE_SINGLE_CHAR_LIMIT + 128))
    body = {
        "model": "gpt-5.5",
        "stream": True,
        "input": [
            {
                "type": "function_call_output",
                "call_id": "call_small",
                "output": [{"type": "input_image", "image_url": small_image}],
            },
            {
                "type": "function_call_output",
                "call_id": "call_big",
                "output": [{"type": "input_image", "image_url": big_image}],
            },
        ],
    }

    sanitized, report = cp4cc.sanitize_responses_payload(body)

    assert report["images"] == 2
    assert report["kept"] == 1
    assert report["omitted"] == 1
    assert sanitized["input"][0]["output"][0]["image_url"] == small_image
    replacement = sanitized["input"][1]["output"][0]
    assert replacement["type"] == "input_text"
    assert "cp4cc image attachment omitted" in replacement["text"]
    assert "CP4CC_IMAGE_SINGLE_CHAR_LIMIT" in replacement["text"]


def test_synthetic_responses_error_events_complete_the_stream(cp4cc):
    events = cp4cc.synthetic_responses_error_events(
        413,
        '{"error":{"message":"failed to parse request"}}',
        "resp_test",
        "gpt-5.5",
        "12345678-1234-1234-1234-123456789abc",
    )

    assert any("event: response.completed" in event for event in events)
    joined = "".join(events)
    assert "cp4cc upstream error 413" in joined
    assert "failed to parse request" in joined


def test_is_expired_ide_token_error_matches_only_upstream_token_expiry(cp4cc):
    assert cp4cc.is_expired_ide_token_error(
        401,
        "IDE token expired: unauthorized: token expired",
    )
    assert cp4cc.is_expired_ide_token_error(401, "unauthorized: token expired")
    assert not cp4cc.is_expired_ide_token_error(401, "unauthorized")
    assert not cp4cc.is_expired_ide_token_error(413, "IDE token expired: unauthorized: token expired")


def test_invalidate_api_key_cache_removes_cached_key(cp4cc, monkeypatch, tmp_path):
    api_key_file = tmp_path / "api-key.json"
    api_key_file.write_text('{"token":"old"}')
    monkeypatch.setattr(cp4cc, "API_KEY_FILE", str(api_key_file))

    cp4cc.invalidate_api_key_cache("test")

    assert not api_key_file.exists()
    cp4cc.invalidate_api_key_cache("already gone")


def test_is_upstream_high_demand_error_matches_copilot_capacity_503(cp4cc):
    msg = "Sorry, the upstream model provider is currently experiencing high demand. Please try another model."

    assert cp4cc.is_upstream_high_demand_error(503, msg)
    assert not cp4cc.is_upstream_high_demand_error(503, "Service unavailable")
    assert not cp4cc.is_upstream_high_demand_error(429, msg)


def test_upstream_busy_retry_delay_uses_bounded_exponential_backoff(cp4cc, monkeypatch):
    monkeypatch.setattr(cp4cc, "UPSTREAM_BUSY_BACKOFF_SECONDS", 3.0)

    assert cp4cc.upstream_busy_retry_delay(1) == 3.0
    assert cp4cc.upstream_busy_retry_delay(2) == 6.0
    assert cp4cc.upstream_busy_retry_delay(10) == 30.0
