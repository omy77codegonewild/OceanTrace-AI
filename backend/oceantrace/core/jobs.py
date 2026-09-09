"""Asynchronous job runner. Long analysis never runs inside the request thread.
Local implementation uses a ThreadPoolExecutor; the interface (submit/get) is
identical to what a Celery/RQ adapter would expose, so swapping is mechanical."""
from __future__ import annotations

import logging
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .config import get_settings
from .db import db, dumps, loads, row_to_dict, utcnow

log = logging.getLogger("oceantrace.jobs")

_executor: ThreadPoolExecutor | None = None
_exec_lock = threading.Lock()

STATES = ("queued", "running", "partial", "completed", "failed")


def _pool() -> ThreadPoolExecutor:
    global _executor
    with _exec_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=get_settings().max_workers, thread_name_prefix="otjob")
        return _executor


class JobContext:
    def __init__(self, job_id: str):
        self.job_id = job_id

    def progress(self, fraction: float, message: str | None = None) -> None:
        with db() as conn:
            conn.execute(
                "UPDATE jobs SET progress=?, message=COALESCE(?, message), state='running', updated_at=? WHERE id=?",
                (max(0.0, min(1.0, float(fraction))), message, utcnow(), self.job_id),
            )
        if message:
            log.info("job=%s %.0f%% %s", self.job_id, fraction * 100, message)


def create_job(kind: str, case_id: str | None) -> str:
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    now = utcnow()
    with db() as conn:
        conn.execute(
            "INSERT INTO jobs(id, case_id, kind, state, progress, message, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (job_id, case_id, kind, "queued", 0.0, "queued", now, now),
        )
    return job_id


def submit(kind: str, case_id: str | None, fn: Callable[[JobContext], Any]) -> str:
    """Create a job row and run `fn(ctx)` in the worker pool. Returns job_id."""
    job_id = create_job(kind, case_id)

    def _run() -> None:
        ctx = JobContext(job_id)
        with db() as conn:
            conn.execute("UPDATE jobs SET state='running', message='started', updated_at=? WHERE id=?", (utcnow(), job_id))
        try:
            result = fn(ctx)
            with db() as conn:
                conn.execute(
                    "UPDATE jobs SET state='completed', progress=1.0, message='completed', result=?, updated_at=? WHERE id=?",
                    (dumps(result), utcnow(), job_id),
                )
            log.info("job=%s kind=%s completed", job_id, kind)
        except Exception as exc:  # noqa: BLE001 - we persist every failure for the analyst
            err = f"{type(exc).__name__}: {exc}"
            log.error("job=%s kind=%s failed: %s\n%s", job_id, kind, err, traceback.format_exc())
            with db() as conn:
                conn.execute(
                    "UPDATE jobs SET state='failed', message='failed', error=?, updated_at=? WHERE id=?",
                    (err, utcnow(), job_id),
                )

    _pool().submit(_run)
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    d = row_to_dict(row)
    if d:
        d["result"] = loads(d.get("result"))
    return d


def list_jobs(case_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, case_id, kind, state, progress, message, error, created_at, updated_at FROM jobs WHERE case_id=? ORDER BY created_at DESC LIMIT ?",
            (case_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
