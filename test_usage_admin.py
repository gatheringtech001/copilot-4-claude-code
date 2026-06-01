import importlib
import sys
from datetime import datetime, timezone, timedelta

import pytest


@pytest.fixture()
def cp4cc(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["cp4cc.py", "--fast"])
    monkeypatch.setenv("CP4CC_ADMIN_USER", "admin")
    monkeypatch.setenv("CP4CC_ADMIN_PASSWORD", "secret")
    if "cp4cc" in sys.modules:
        del sys.modules["cp4cc"]
    return importlib.import_module("cp4cc")


def make_req(ts, model="gpt-5.5", endpoint="/v1/responses", status=200, duration=1000, usage=None, source=None):
    req = {
        "id": "req1",
        "timestamp": ts,
        "original_model": model,
        "copilot_model": model,
        "endpoint": endpoint,
        "stream": False,
        "messages_count": 1,
        "response": {"status_code": status, "body": {"usage": usage or {}}},
        "duration_ms": duration,
        "error": None,
    }
    if source:
        req["source"] = source
    return req


def test_usage_stats_summarizes_requests_by_window_model_endpoint_status_and_tokens(cp4cc):
    now = datetime(2026, 5, 29, 8, 0, tzinfo=timezone.utc)
    requests = [
        make_req((now - timedelta(hours=1)).isoformat(), usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}),
        make_req((now - timedelta(hours=2)).isoformat(), model="claude-opus-4.7", endpoint="/v1/messages", status=400, duration=3000, usage={"cache_read_input_tokens": 20}),
        make_req((now - timedelta(days=2)).isoformat(), model="old", duration=7000),
    ]

    stats = cp4cc.build_usage_stats(requests, now=now)

    assert stats["total"]["requests"] == 3
    assert stats["total"]["ok"] == 2
    assert stats["total"]["errors"] == 1
    assert stats["last_24h"]["requests"] == 2
    assert stats["last_7d"]["requests"] == 3
    assert stats["models"][0]["name"] == "gpt-5.5"
    assert stats["endpoints"]["/v1/responses"] == 2
    assert stats["status"]["400"] == 1
    assert stats["tokens"]["input_tokens"] == 10
    assert stats["tokens"]["output_tokens"] == 5
    assert stats["tokens"]["cache_read_input_tokens"] == 20
    assert stats["latency"]["avg_ms"] == 3667


def test_usage_stats_groups_requests_by_source(cp4cc):
    now = datetime(2026, 5, 29, 8, 0, tzinfo=timezone.utc)
    requests = [
        make_req(now.isoformat(), source={"client": "codex", "ip": "1.1.1.1"}),
        make_req(now.isoformat(), source={"client": "claude-code", "ip": "2.2.2.2"}),
        make_req(now.isoformat(), source={"client": "codex", "ip": "1.1.1.1"}),
        make_req(now.isoformat()),
    ]

    stats = cp4cc.build_usage_stats(requests, now=now)

    assert stats["sources"][0] == {"name": "codex", "count": 2}
    assert {"name": "claude-code", "count": 1} in stats["sources"]
    assert stats["source_ips"]["1.1.1.1"] == 2


def test_usage_stats_includes_clickable_ip_rows_with_token_totals(cp4cc):
    now = datetime(2026, 5, 29, 8, 0, tzinfo=timezone.utc)
    requests = [
        make_req(now.isoformat(), model="gpt-5.5", source={"client": "codex", "ip": "1.1.1.1"}, usage={"input_tokens": 10, "output_tokens": 2}),
        make_req(now.isoformat(), model="claude-opus-4.7", source={"client": "claude", "ip": "1.1.1.1"}, usage={"input_tokens": 5, "output_tokens": 7, "cache_read_input_tokens": 20}),
        make_req(now.isoformat(), model="gpt-5.5", source={"client": "codex", "ip": "2.2.2.2"}, usage={"input_tokens": 100}),
    ]

    stats = cp4cc.build_usage_stats(requests, now=now)
    first_ip = stats["ip_rows"][0]

    assert first_ip["ip"] == "1.1.1.1"
    assert first_ip["requests"] == 2
    assert first_ip["tokens"]["input_tokens"] == 15
    assert first_ip["tokens"]["output_tokens"] == 9
    assert first_ip["tokens"]["cache_read_input_tokens"] == 20
    assert first_ip["models"] == {"gpt-5.5": 1, "claude-opus-4.7": 1}


def test_build_ip_usage_stats_filters_to_one_ip_with_models_and_tokens(cp4cc):
    now = datetime(2026, 5, 29, 8, 0, tzinfo=timezone.utc)
    requests = [
        make_req(now.isoformat(), model="gpt-5.5", source={"client": "codex", "ip": "1.1.1.1"}, usage={"input_tokens": 10, "output_tokens": 2}),
        make_req(now.isoformat(), model="claude-opus-4.7", status=400, source={"client": "claude", "ip": "1.1.1.1"}, usage={"input_tokens": 5, "output_tokens": 7}),
        make_req(now.isoformat(), model="gpt-5.5", source={"client": "codex", "ip": "2.2.2.2"}, usage={"input_tokens": 100}),
    ]

    stats = cp4cc.build_ip_usage_stats(requests, "1.1.1.1", now=now)

    assert stats["ip"] == "1.1.1.1"
    assert stats["total"]["requests"] == 2
    assert stats["total"]["ok"] == 1
    assert stats["total"]["errors"] == 1
    assert stats["models"] == [{"name": "gpt-5.5", "count": 1}, {"name": "claude-opus-4.7", "count": 1}]
    assert stats["tokens"] == {"input_tokens": 15, "output_tokens": 9}


def test_build_ip_usage_stats_includes_daily_model_and_token_breakdown(cp4cc):
    requests = [
        make_req("2026-05-28T01:00:00+00:00", model="gpt-5.5", source={"client": "codex", "ip": "1.1.1.1"}, usage={"input_tokens": 10, "output_tokens": 2}),
        make_req("2026-05-28T02:00:00+00:00", model="claude-opus-4.7", source={"client": "claude", "ip": "1.1.1.1"}, usage={"input_tokens": 5, "output_tokens": 7}),
        make_req("2026-05-29T03:00:00+00:00", model="gpt-5.5", source={"client": "codex", "ip": "1.1.1.1"}, usage={"input_tokens": 100, "cache_read_input_tokens": 50}),
        make_req("2026-05-29T03:00:00+00:00", model="gpt-5.5", source={"client": "codex", "ip": "2.2.2.2"}, usage={"input_tokens": 999}),
    ]

    stats = cp4cc.build_ip_usage_stats(requests, "1.1.1.1", now=datetime(2026, 5, 30, tzinfo=timezone.utc))

    assert stats["daily_rows"] == [
        {
            "date": "2026-05-28",
            "requests": 2,
            "ok": 2,
            "errors": 0,
            "tokens": {"input_tokens": 15, "output_tokens": 9},
            "models": {"gpt-5.5": 1, "claude-opus-4.7": 1},
        },
        {
            "date": "2026-05-29",
            "requests": 1,
            "ok": 1,
            "errors": 0,
            "tokens": {"input_tokens": 100, "cache_read_input_tokens": 50},
            "models": {"gpt-5.5": 1},
        },
    ]


def test_build_ip_usage_stats_includes_request_rows_and_input_token_extremes(cp4cc):
    requests = [
        make_req("2026-05-29T01:00:00+00:00", model="claude-sonnet-4.6", source={"client": "claude", "ip": "1.1.1.1"}, usage={"input_tokens": 60000, "output_tokens": 100}),
        make_req("2026-05-29T02:00:00+00:00", model="claude-sonnet-4.6", source={"client": "claude", "ip": "1.1.1.1"}, usage={"input_tokens": 40000, "output_tokens": 50}),
    ]

    stats = cp4cc.build_ip_usage_stats(requests, "1.1.1.1", now=datetime(2026, 5, 30, tzinfo=timezone.utc))

    assert stats["token_metrics"]["avg_input_tokens"] == 50000
    assert stats["token_metrics"]["max_input_tokens"] == 60000
    assert stats["request_rows"][0]["input_tokens"] == 40000
    assert stats["request_rows"][1]["input_tokens"] == 60000
    assert stats["request_rows"][1]["model"] == "claude-sonnet-4.6"


def test_source_from_request_prefers_explicit_headers_and_masks_token(cp4cc):
    class Req:
        client = type("Client", (), {"host": "10.0.0.1"})()
        headers = {
            "x-cp4cc-source": "team-a/codex",
            "x-forwarded-for": "8.8.8.8, 10.0.0.1",
            "user-agent": "codex-cli/1.2",
            "authorization": "Bearer should-not-leak",
        }

    source = cp4cc.source_from_request(Req())

    assert source["client"] == "team-a/codex"
    assert source["ip"] == "8.8.8.8"
    assert source["user_agent"] == "codex-cli/1.2"
    assert "authorization" not in source


def test_source_from_request_falls_back_to_user_agent_family(cp4cc):
    class Req:
        client = type("Client", (), {"host": "127.0.0.1"})()
        headers = {"user-agent": "Claude-Code/1.0"}

    source = cp4cc.source_from_request(Req())

    assert source["client"] == "claude-code"
    assert source["ip"] == "127.0.0.1"


def test_admin_credentials_accept_configured_username_password(cp4cc):
    assert cp4cc.verify_admin_credentials("admin", "secret") is True
    assert cp4cc.verify_admin_credentials("admin", "wrong") is False
    assert cp4cc.verify_admin_credentials("wrong", "secret") is False


def test_admin_session_token_roundtrip(cp4cc):
    token = cp4cc.create_admin_session_token("admin")

    assert cp4cc.verify_admin_session_token(token) == "admin"
    assert cp4cc.verify_admin_session_token(token + "x") is None
