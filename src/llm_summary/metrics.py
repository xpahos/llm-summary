"""Optional per-run job metrics pushed to a Prometheus Pushgateway.

The job runs once per day and every internal counter starts from zero, so
metrics are exported as gauges holding the final values of the current run —
never lifetime process counters. Error series use a small fixed vocabulary of
stage / error_type labels (no raw messages, task ids, request ids or stack
traces), and the known combinations are pre-seeded to 0 so Grafana can draw a
continuous time series instead of a gap.

Pushing is strictly best-effort: a Pushgateway failure is logged and never
fails the job itself.
"""

from __future__ import annotations

import logging
import time
import urllib.request
from typing import Any

log = logging.getLogger("llm_summary.metrics")

# Stable Pushgateway grouping key. Never add dynamic values (timestamps, run
# ids) here: each push must replace the previous run's group.
JOB_NAME = "llm_summary_daily"
DEFAULT_PUSHGATEWAY = "127.0.0.1:50100"

STAGES = ("fetch_tasks", "llm", "processing", "unknown")
ERROR_TYPES = (
    "http_5xx",
    "timeout",
    "rate_limit",
    "invalid_response",
    "invalid_task",
    "unknown",
)

# (stage, error_type) series always exported, even with value 0.
KNOWN_ERRORS = (
    ("fetch_tasks", "http_5xx"),
    ("fetch_tasks", "timeout"),
    ("llm", "rate_limit"),
    ("llm", "invalid_response"),
    ("processing", "invalid_task"),
    ("unknown", "unknown"),
)


def _http_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status extraction across PyGithub/httpx/requests/openai."""
    for attr in ("status", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def classify_error(exc: BaseException) -> str:
    """Map an exception onto the small fixed error_type vocabulary.

    Checks are name/attribute based so no optional client library needs to be
    imported just to recognize its exceptions.
    """
    name = type(exc).__name__.lower().replace("_", "")
    status = _http_status(exc)
    if status == 429 or "ratelimit" in name:
        return "rate_limit"
    if isinstance(exc, TimeoutError) or "timeout" in name:
        return "timeout"
    if status is not None and 500 <= status <= 599:
        return "http_5xx"
    # ValueError also covers json.JSONDecodeError and pydantic's ValidationError.
    if isinstance(exc, ValueError) or name == "validationerror":
        return "invalid_response"
    if isinstance(exc, (KeyError, TypeError)):
        return "invalid_task"
    return "unknown"


class MetricsCollector:
    """Accumulates per-run counters and pushes them as gauges."""

    def __init__(self) -> None:
        self.tasks_received = 0
        self.tasks_processed = 0
        self.llm_requests = 0
        self.duration_seconds = 0.0
        self.success = 0
        self.last_run_timestamp = 0.0
        self.errors: dict[tuple[str, str], int] = {pair: 0 for pair in KNOWN_ERRORS}

    # --- counters ------------------------------------------------------------
    def inc_tasks_received(self, n: int = 1) -> None:
        self.tasks_received += n

    def inc_tasks_processed(self, n: int = 1) -> None:
        self.tasks_processed += n

    def inc_llm_requests(self, n: int = 1) -> None:
        self.llm_requests += n

    def record_error(self, stage: str, error_type: str) -> None:
        if stage not in STAGES:
            stage = "unknown"
        if error_type not in ERROR_TYPES:
            error_type = "unknown"
        key = (stage, error_type)
        self.errors[key] = self.errors.get(key, 0) + 1

    def record_exception(self, stage: str, exc: BaseException) -> None:
        """Record an exception once: re-recording the same object is a no-op.

        The LLM wrapper classifies its own failures under stage="llm"; the
        marker keeps outer handlers (per-event loop, node wrapper) from
        double-counting the same exception under a coarser stage.
        """
        if getattr(exc, "_metrics_recorded", False):
            return
        try:
            exc._metrics_recorded = True  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - exceptions with __slots__
            pass
        self.record_error(stage, classify_error(exc))

    def finalize(self, success: bool, duration_seconds: float) -> None:
        self.success = 1 if success else 0
        self.duration_seconds = duration_seconds
        self.last_run_timestamp = time.time()

    # --- export ---------------------------------------------------------------
    def render(self) -> str:
        """Prometheus text exposition format, all metrics as gauges."""
        lines: list[str] = []

        def gauge(name: str, value: float | int) -> None:
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value}")

        gauge("daily_job_tasks_received", self.tasks_received)
        gauge("daily_job_tasks_processed", self.tasks_processed)
        gauge("daily_job_llm_requests", self.llm_requests)
        gauge("daily_job_duration_seconds", self.duration_seconds)
        gauge("daily_job_success", self.success)
        gauge("daily_job_last_run_timestamp_seconds", self.last_run_timestamp)
        lines.append("# TYPE daily_job_errors gauge")
        for stage, error_type in sorted(self.errors):
            count = self.errors[(stage, error_type)]
            lines.append(
                f'daily_job_errors{{stage="{stage}",error_type="{error_type}"}} {count}'
            )
        return "\n".join(lines) + "\n"

    def push(self, address: str, job: str = JOB_NAME, timeout: float = 10.0) -> bool:
        """PUT the run's metrics to the Pushgateway. Never raises."""
        base = address if "://" in address else f"http://{address}"
        url = f"{base.rstrip('/')}/metrics/job/{job}"
        request = urllib.request.Request(
            url,
            data=self.render().encode("utf-8"),
            method="PUT",
            headers={"Content-Type": "text/plain; version=0.0.4"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                log.info("Pushed run metrics to %s (HTTP %s)", url, resp.status)
            return True
        except Exception as exc:  # noqa: BLE001 - metrics must never fail the job
            log.warning("Failed to push metrics to %s: %s", url, exc)
            return False


class CountingSummarizer:
    """Summarizer wrapper that counts LLM requests and classifies LLM errors."""

    def __init__(self, inner: Any, metrics: MetricsCollector) -> None:
        self._inner = inner
        self._metrics = metrics

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        self._metrics.inc_llm_requests()
        try:
            return getattr(self._inner, method)(*args, **kwargs)
        except Exception as exc:
            self._metrics.record_exception("llm", exc)
            raise

    def initial_object_summary(self, obj: dict[str, Any]) -> str:
        return self._call("initial_object_summary", obj)

    def summarize_head_diff(self, old_sha: str, new_sha: str, compare: dict[str, Any]) -> str:
        return self._call("summarize_head_diff", old_sha, new_sha, compare)

    def update_object_summary(self, prev: str, event: dict[str, Any], obj: dict[str, Any]) -> str:
        return self._call("update_object_summary", prev, event, obj)

    def daily_view_model(self, payload: dict[str, Any]):
        return self._call("daily_view_model", payload)
