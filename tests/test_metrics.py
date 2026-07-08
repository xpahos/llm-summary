"""Metrics collection and Pushgateway export (offline: urlopen is faked)."""

from __future__ import annotations

import argparse
import urllib.request

import pytest

from llm_summary import graph as graph_mod
from llm_summary.graph import run_pipeline
from llm_summary.main import build_parser, cmd_run_daily
from llm_summary.metrics import (
    DEFAULT_PUSHGATEWAY,
    KNOWN_ERRORS,
    MetricsCollector,
    classify_error,
)

from conftest import FakeGithubClient, FakeSummarizer, make_issue


def run_daily_args(**overrides):
    ns = argparse.Namespace(
        command="run-daily",
        date=None,
        from_date=None,
        to_date=None,
        force_update=False,
        collect_metrics=False,
        push_gateway=DEFAULT_PUSHGATEWAY,
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def pushes(monkeypatch):
    """Capture Pushgateway requests instead of hitting the network."""
    captured = []

    def fake_urlopen(request, timeout=None):
        captured.append(request)
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return captured


# --- CLI parsing --------------------------------------------------------------

def test_cli_metrics_disabled_by_default():
    args = build_parser().parse_args(["run-daily"])
    assert args.collect_metrics is False
    assert args.push_gateway == "127.0.0.1:50100"


def test_cli_collect_metrics_flag():
    args = build_parser().parse_args(["run-daily", "--collect-metrics"])
    assert args.collect_metrics is True


def test_cli_push_gateway_override():
    args = build_parser().parse_args(
        ["run-daily", "--collect-metrics", "--push-gateway", "pushgw.internal:9091"]
    )
    assert args.push_gateway == "pushgw.internal:9091"


# --- disabled by default --------------------------------------------------------

def test_no_push_without_flag(monkeypatch, config, pushes):
    monkeypatch.setattr(
        graph_mod, "run_pipeline", lambda cfg, **kw: {"output_paths": []}
    )
    rc = cmd_run_daily(config, run_daily_args())
    assert rc == 0
    assert pushes == []


def test_run_pipeline_default_has_no_metrics(config):
    gh = FakeGithubClient(config.github.repo, {("issue", 999): make_issue(999)})
    result = run_pipeline(
        config,
        since="2026-06-27T00:00:00+00:00",
        until="2026-06-28T00:00:00+00:00",
        advance_cursor=False,
        gh=gh,
        summarizer=FakeSummarizer(),
    )
    assert not result.get("errors")


# --- collection during a pipeline run -------------------------------------------

def test_pipeline_collects_counts(config):
    collector = MetricsCollector()
    gh = FakeGithubClient(config.github.repo, {("issue", 999): make_issue(999)})
    result = run_pipeline(
        config,
        since="2026-06-27T00:00:00+00:00",
        until="2026-06-28T00:00:00+00:00",
        advance_cursor=False,
        gh=gh,
        summarizer=FakeSummarizer(),
        metrics=collector,
    )
    assert not result.get("errors")
    assert collector.tasks_received >= 1
    assert collector.tasks_processed == collector.tasks_received
    # At least: initial summary, one event update, the daily view model.
    assert collector.llm_requests >= 3
    assert all(count == 0 for count in collector.errors.values())


def test_llm_failure_classified_under_llm_stage(config):
    class FailingSummarizer(FakeSummarizer):
        def update_object_summary(self, prev, event, obj):
            raise ValueError("bad model output")

    collector = MetricsCollector()
    gh = FakeGithubClient(config.github.repo, {("issue", 999): make_issue(999)})
    run_pipeline(
        config,
        since="2026-06-27T00:00:00+00:00",
        until="2026-06-28T00:00:00+00:00",
        advance_cursor=False,
        gh=gh,
        summarizer=FailingSummarizer(),
        metrics=collector,
    )
    assert collector.errors[("llm", "invalid_response")] >= 1
    # The per-event handler must not double-count the same exception.
    assert collector.errors[("processing", "invalid_task")] == 0
    assert collector.tasks_processed < collector.tasks_received


# --- error classification --------------------------------------------------------

class Boom(Exception):
    def __init__(self, status=None):
        super().__init__("boom")
        self.status = status


def test_classify_error_types():
    assert classify_error(Boom(status=502)) == "http_5xx"
    assert classify_error(Boom(status=429)) == "rate_limit"
    assert classify_error(TimeoutError()) == "timeout"
    assert classify_error(ValueError("bad json")) == "invalid_response"
    assert classify_error(KeyError("field")) == "invalid_task"
    assert classify_error(Boom()) == "unknown"


def test_classify_error_by_exception_name():
    class RateLimitExceededException(Exception):
        pass

    class ReadTimeout(Exception):
        pass

    assert classify_error(RateLimitExceededException()) == "rate_limit"
    assert classify_error(ReadTimeout()) == "timeout"


def test_unknown_labels_fold_into_unknown():
    collector = MetricsCollector()
    collector.record_error("weird_stage", "weird_type")
    assert collector.errors[("unknown", "unknown")] == 1


# --- rendering --------------------------------------------------------------------

def test_render_seeds_known_error_series_with_zero():
    text = MetricsCollector().render()
    for stage, error_type in KNOWN_ERRORS:
        assert f'daily_job_errors{{stage="{stage}",error_type="{error_type}"}} 0' in text
    for name in (
        "daily_job_tasks_received",
        "daily_job_tasks_processed",
        "daily_job_llm_requests",
        "daily_job_duration_seconds",
        "daily_job_success",
        "daily_job_last_run_timestamp_seconds",
    ):
        assert f"# TYPE {name} gauge" in text


# --- pushing -----------------------------------------------------------------------

def test_push_uses_address_and_stable_job_name(pushes):
    collector = MetricsCollector()
    assert collector.push("10.1.2.3:9999") is True
    (request,) = pushes
    assert request.full_url == "http://10.1.2.3:9999/metrics/job/llm_summary_daily"
    assert request.get_method() == "PUT"
    assert b"daily_job_tasks_received 0" in request.data


def test_push_failure_is_swallowed(monkeypatch):
    def failing_urlopen(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", failing_urlopen)
    assert MetricsCollector().push("127.0.0.1:1") is False


# --- cmd_run_daily integration ------------------------------------------------------

def test_successful_run_pushes_success_one(monkeypatch, config, pushes):
    monkeypatch.setattr(
        graph_mod, "run_pipeline", lambda cfg, **kw: {"output_paths": []}
    )
    rc = cmd_run_daily(config, run_daily_args(collect_metrics=True))
    assert rc == 0
    (request,) = pushes
    body = request.data.decode()
    assert "daily_job_success 1" in body


def test_failed_run_pushes_success_zero(monkeypatch, config, pushes):
    monkeypatch.setattr(
        graph_mod, "run_pipeline", lambda cfg, **kw: {"errors": ["fetch_candidates: boom"]}
    )
    rc = cmd_run_daily(config, run_daily_args(collect_metrics=True))
    assert rc == 1
    (request,) = pushes
    body = request.data.decode()
    assert "daily_job_success 0" in body
    assert "daily_job_duration_seconds" in body
    assert "daily_job_last_run_timestamp_seconds" in body


def test_crashed_run_still_pushes(monkeypatch, config, pushes):
    def exploding_pipeline(cfg, **kw):
        raise RuntimeError("hard crash")

    monkeypatch.setattr(graph_mod, "run_pipeline", exploding_pipeline)
    with pytest.raises(RuntimeError):
        cmd_run_daily(config, run_daily_args(collect_metrics=True))
    (request,) = pushes
    body = request.data.decode()
    assert "daily_job_success 0" in body
    assert 'daily_job_errors{stage="unknown",error_type="unknown"} 1' in body


def test_push_gateway_override_is_used(monkeypatch, config, pushes):
    monkeypatch.setattr(
        graph_mod, "run_pipeline", lambda cfg, **kw: {"output_paths": []}
    )
    args = run_daily_args(collect_metrics=True, push_gateway="pushgw.internal:9091")
    cmd_run_daily(config, args)
    (request,) = pushes
    assert request.full_url.startswith("http://pushgw.internal:9091/")
