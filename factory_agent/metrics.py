"""Prometheus 지표.

API는 /metrics로, 독립 워커는 FSA_METRICS_PORT의 별도 HTTP 서버로 노출한다.
큐 길이는 스크레이프 시점에 Redis에서 읽는다 (워커 수와 무관하게 한 곳에서 정확한 값).
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter("fsa_http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_SECONDS = Histogram("fsa_http_request_seconds", "HTTP request latency", ["method", "route"],
                         buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5))

JOBS_SUBMITTED = Counter("fsa_jobs_submitted_total", "Jobs submitted", ["kind"])
JOBS_FINISHED = Counter("fsa_jobs_finished_total", "Jobs finished", ["kind", "status"])
JOB_WAIT_SECONDS = Histogram("fsa_job_wait_seconds", "Queue wait (created -> started)", ["kind"],
                             buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600))
JOB_RUN_SECONDS = Histogram("fsa_job_run_seconds", "Job run time (started -> finished)", ["kind", "status"],
                            buckets=(0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 300))
LEASES_REAPED = Counter("fsa_leases_reaped_total", "Expired leases reaped", ["queue", "outcome"])
SCHEDULE_IMPROVEMENT = Histogram("fsa_schedule_tardiness_reduction_pct", "Weighted tardiness reduction vs rule baseline",
                                 buckets=(0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100))

QUEUE_PENDING = Gauge("fsa_queue_pending", "Jobs waiting", ["queue"])
QUEUE_RUNNING = Gauge("fsa_queue_running", "Jobs running", ["queue"])


def observe_schedule_result(result: dict) -> None:
    metrics, base = result.get("metrics"), result.get("baseline_metrics")
    if metrics and base and base.get("weighted_tardiness"):
        reduction = (base["weighted_tardiness"] - metrics["weighted_tardiness"]) / base["weighted_tardiness"] * 100
        SCHEDULE_IMPROVEMENT.observe(max(0.0, reduction))
