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
