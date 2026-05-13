import argparse
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
