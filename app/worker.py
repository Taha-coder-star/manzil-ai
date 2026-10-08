"""Worker runtime (Task 3.1): claims and runs queued jobs.

Tenant discovery uses the same trusted sender configuration as the webhook
(WEBHOOK_SENDER_AGENCIES), so the worker needs no RLS exception: every query
runs inside tenant_transaction for one configured agency. Agencies are polled
round-robin. Each thread owns one connection; a job's conversation lock lives
on that connection while the handler runs.

Handlers take (conn, job), open their own tenant_transaction(conn, job.agency_id)
and must be idempotent: a crashed job is retried after its lease expires.
Raise PermanentJobError to dead-letter without retrying.
"""

import logging
import os
import signal
import socket
import threading
import time

import psycopg
from psycopg.pq import TransactionStatus

from app import queue
from app.config import load_settings
from app.routing import RoutingConfigError, parse_sender_agencies

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5
POLL_SECONDS = 1.0
REAP_EVERY_SECONDS = 30
MAX_RECONNECT_DELAY_SECONDS = 30

# job_type -> handler(conn, job). Only registered types are claimed; others stay pending.
HANDLERS = {}


class Worker:
    def __init__(
        self, database_url, agency_ids, handlers, stop, *, name="worker-1",
        lease_seconds=queue.DEFAULT_LEASE_SECONDS, poll_seconds=POLL_SECONDS,
        reap_every_seconds=REAP_EVERY_SECONDS, backoff=queue.backoff_seconds,
    ):
        self.database_url = database_url
        self.agency_ids = list(dict.fromkeys(agency_ids))
        self.handlers = dict(handlers)
        self.stop = stop
        self.locked_by = f"{socket.gethostname()}:{os.getpid()}:{name}"
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.reap_every_seconds = reap_every_seconds
        self.backoff = backoff
        self.conn = None
        self._next_agency = 0
        self._next_reap = 0.0

    def connect(self):
        if self.conn is None or self.conn.closed:
            self.conn = psycopg.connect(
                self.database_url, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
            )
        return self.conn

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def run_once(self):
        """Reap if due, then run at most one job. Returns True if a job ran."""
        conn = self.connect()
        if time.monotonic() >= self._next_reap:
            for agency_id in self.agency_ids:
                queue.reap_expired(conn, agency_id)
            self._next_reap = time.monotonic() + self.reap_every_seconds
        if not self.handlers:
            return False
        for _ in range(len(self.agency_ids)):
            agency_id = self.agency_ids[self._next_agency]
            self._next_agency = (self._next_agency + 1) % len(self.agency_ids)
            job = queue.claim_job(
                conn, agency_id, self.handlers, self.locked_by, self.lease_seconds
            )
            if job is not None:
                self.execute(job)
                return True
        return False

    def execute(self, job):
        conn = self.conn
        error = None
        try:
            self.handlers[job.job_type](conn, job)
        except Exception as exc:  # every failure is recorded on the job
            error = exc
            if conn.closed:
                # The lock went with the connection; the reaper requeues the job.
                raise
            if conn.info.transaction_status != TransactionStatus.IDLE:
                conn.rollback()
        status = queue.finish_job(conn, job, self.locked_by, error, backoff=self.backoff)
        if error is None:
            logger.info("Job %s (%s) succeeded", job.id, job.job_type)
        else:
            logger.warning(
                "Job %s (%s) attempt %d/%d failed with %s; now %s", job.id, job.job_type,
                job.attempts, job.max_attempts, queue.error_code(error), status,
            )

    def run(self):
        failures = 0
        while not self.stop.is_set():
            try:
                ran = self.run_once()
                failures = 0
            except Exception as exc:  # noqa: BLE001 - a worker thread must never die
                # Closing the connection releases any conversation lock it held;
                # an unfinished job is requeued by the reaper after its lease.
                failures += 1
                logger.error("Worker error (%s); reconnecting", type(exc).__name__)
                self.close()
                self.stop.wait(min(2 ** failures, MAX_RECONNECT_DELAY_SECONDS))
                continue
            if not ran:
                self.stop.wait(self.poll_seconds)
        self.close()


def run():
    stop = threading.Event()

    def request_shutdown(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    settings = load_settings()
    try:
        agency_ids = list(parse_sender_agencies(settings.webhook_sender_agencies).values())
    except RoutingConfigError as exc:
        agency_ids = None
        logger.error("Worker idle: %s", exc)
    if agency_ids is not None and not settings.database_url:
        agency_ids = None
        logger.error("Worker idle: DATABASE_URL is not set")
    if agency_ids is None:
        # Idle rather than crash-loop, so the container stays up for inspection.
        stop.wait()
        logger.info("Worker stopped cleanly.")
        return

    workers = [
        Worker(settings.database_url, agency_ids, HANDLERS, stop, name=f"worker-{i + 1}")
        for i in range(settings.worker_concurrency)
    ]
    threads = [threading.Thread(target=w.run, name=f"worker-{i + 1}") for i, w in enumerate(workers)]
    for thread in threads:
        thread.start()
    logger.info(
        "Worker started: %d thread(s), %d agency(ies), job types: %s",
        len(threads), len(set(agency_ids)), ", ".join(sorted(HANDLERS)) or "none yet",
    )
    while not stop.is_set():
        stop.wait(1.0)
    for thread in threads:
        thread.join()
    logger.info("Worker stopped cleanly.")
