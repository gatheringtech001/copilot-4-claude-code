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


def test_map_model_name_normalizes_current_opus_model_ids(cp4cc, monkeypatch):
    monkeypatch.delenv("CP4CC_DEFAULT_CLAUDE_OPUS_MODEL", raising=False)

    assert cp4cc.map_model_name("claude-opus-4-8") == "claude-opus-4.8"
    assert cp4cc.map_model_name("claude-opus-4-8-20260528") == "claude-opus-4.8"
    assert cp4cc.map_model_name("claude-opus-4-8[1m]") == "claude-opus-4.8"
    assert cp4cc.map_model_name("claude-opus-4.7") == "claude-opus-4.7"


def test_map_model_name_routes_opus_alias_to_configured_default(cp4cc, monkeypatch):
    monkeypatch.setenv("CP4CC_DEFAULT_CLAUDE_OPUS_MODEL", "claude-opus-4-7")

    assert cp4cc.map_model_name("opus") == "claude-opus-4.7"
    assert cp4cc.map_model_name("claude-opus-latest") == "claude-opus-4.7"


def test_map_model_name_keeps_opus_context_tiers_separate(cp4cc, monkeypatch):
    monkeypatch.setenv("CP4CC_DEFAULT_CLAUDE_OPUS_200K_MODEL", "claude-opus-4-8")
    monkeypatch.setenv("CP4CC_DEFAULT_CLAUDE_OPUS_1M_MODEL", "claude-opus-4-8-1m-internal")

    assert cp4cc.map_model_name("opus-200k") == "claude-opus-4.8"
    assert cp4cc.map_model_name("claude-opus-4-8[200k]") == "claude-opus-4.8"
    assert cp4cc.map_model_name("opus-1m") == "claude-opus-4.8-1m-internal"
    assert cp4cc.map_model_name("claude-opus-4-8[1m]") == "claude-opus-4.8-1m-internal"


def test_unknown_claude_variant_still_uses_messages_api(cp4cc):
    assert cp4cc.use_messages_api_for_model("claude-opus-4.8-1m-internal", None)
    assert not cp4cc.use_messages_api_for_model("gpt-5.5-preview", None)


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



def test_collect_encrypted_content_hashes_recurses(cp4cc):
    body = {
        "input": [
            {"type": "reasoning", "encrypted_content": "cipher-one"},
            {"nested": {"encrypted_content": "cipher-two"}},
        ]
    }

    hashes = cp4cc.collect_encrypted_content_hashes(body)

    assert cp4cc.encrypted_content_hash("cipher-one") in hashes
    assert cp4cc.encrypted_content_hash("cipher-two") in hashes


def test_bound_responses_encrypted_content_reuses_original_api_key(cp4cc, monkeypatch, tmp_path):
    binding_file = tmp_path / "bindings.json"
    monkeypatch.setattr(cp4cc, "RESPONSES_BINDINGS_FILE", str(binding_file))
    old_info = {
        "token": "old-token",
        "expires_at": 9999999999,
        "endpoints": {"api": "https://old.example"},
        "tracking_id": "old-tracking",
    }
    new_info = {
        "token": "new-token",
        "expires_at": 9999999999,
        "endpoints": {"api": "https://new.example"},
        "tracking_id": "new-tracking",
    }
    cipher_hash = cp4cc.encrypted_content_hash("cipher-from-old-account")
    cp4cc.bind_encrypted_content_hashes({cipher_hash}, old_info)
    monkeypatch.setattr(cp4cc, "get_api_key_info", lambda: new_info)

    selected = cp4cc.select_api_key_info_for_responses_body({
        "input": [{"type": "reasoning", "encrypted_content": "cipher-from-old-account"}]
    })

    assert selected["token"] == "old-token"
    assert cp4cc.get_api_base(selected) == "https://old.example"


def test_expired_encrypted_content_binding_falls_back_to_current_key(cp4cc, monkeypatch, tmp_path):
    binding_file = tmp_path / "bindings.json"
    monkeypatch.setattr(cp4cc, "RESPONSES_BINDINGS_FILE", str(binding_file))
    expired_info = {"token": "expired", "expires_at": 1, "endpoints": {"api": "https://old.example"}}
    current_info = {"token": "current", "expires_at": 9999999999, "endpoints": {"api": "https://current.example"}}
    cp4cc.bind_encrypted_content_hashes({cp4cc.encrypted_content_hash("cipher")}, expired_info)
    monkeypatch.setattr(cp4cc, "get_api_key_info", lambda: current_info)

    selected = cp4cc.select_api_key_info_for_responses_body({
        "input": [{"type": "reasoning", "encrypted_content": "cipher"}]
    })

    assert selected["token"] == "current"


def test_update_encrypted_hashes_from_sse_line_extracts_nested_response_output(cp4cc):
    hashes = set()
    line = 'data: {"type":"response.completed","response":{"output":[{"type":"reasoning","encrypted_content":"cipher-out"}]}}'

    cp4cc.update_encrypted_hashes_from_sse_line(line, hashes)

    assert cp4cc.encrypted_content_hash("cipher-out") in hashes


def test_strip_encrypted_content_removes_only_cipher_fields(cp4cc):
    body = {
        "input": [
            {"type": "reasoning", "encrypted_content": "bad-cipher", "summary": [{"text": "keep"}]},
            {"type": "message", "content": "keep message"},
        ],
        "include": ["reasoning.encrypted_content"],
    }

    stripped = cp4cc.strip_encrypted_content_fields(body)

    assert "encrypted_content" not in str(stripped)
    assert stripped["input"][0]["summary"] == [{"text": "keep"}]
    assert stripped["input"][1]["content"] == "keep message"


def test_invalid_encrypted_content_error_detection(cp4cc):
    body = '{"error":{"message":"The encrypted content abc could not be verified. Reason: Encrypted content could not be decrypted or parsed.","code":"invalid_request_body"}}'

    assert cp4cc.is_invalid_encrypted_content_error(400, body)
    assert not cp4cc.is_invalid_encrypted_content_error(401, body)



def test_sanitize_responses_payload_removes_unsupported_image_generation_tool(cp4cc):
    body = {
        "model": "gpt-5.5",
        "input": "draw a cat",
        "tools": [
            {"type": "image_generation", "quality": "high"},
            {"type": "function", "name": "lookup", "parameters": {"type": "object"}},
        ],
        "tool_choice": {"type": "image_generation"},
    }

    sanitized, report = cp4cc.sanitize_responses_payload(body)

    assert sanitized["tools"] == [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}]
    assert "tool_choice" not in sanitized
    assert report["unsupported_tools_removed"] == 1
    assert report["unsupported_tool_types"] == ["image_generation"]
    assert body["tools"][0]["type"] == "image_generation"
