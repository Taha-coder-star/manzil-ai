"""Task 3.1 acceptance tests: the Postgres job queue and worker runtime.

All runs use the restricted manzil_app login, so RLS applies throughout.
"""

import threading
import time
from collections import defaultdict

import psycopg
import pytest

from app import queue
from app.db import tenant_transaction
from app.provision_agency import create_agency
from app.worker import Worker

CUSTOMER_X, CUSTOMER_Y = "923000000001", "923000000002"


@pytest.fixture
def queue_db(app_role_db):
    _, app_url, _ = app_role_db
    conns = []

    def connect():
        conn = psycopg.connect(app_url, autocommit=True)
        conns.append(conn)
        return conn

    setup = connect()
    agency_a = create_agency(setup, "Demo Realty A (DEMO)", "demo-a", "100000000000001")
    agency_b = create_agency(setup, "Demo Realty B (DEMO)", "demo-b", "100000000000002")

    def enqueue(agency_id, dedupe_key, conversation_key=None, job_type="turn", **kwargs):
        with tenant_transaction(setup, agency_id):
            return queue.enqueue_job(
                setup, job_type, dedupe_key, {"n": dedupe_key},
                conversation_key=conversation_key, **kwargs,
            )

    def job_row(agency_id, job_id):
        with tenant_transaction(setup, agency_id):
            return setup.execute(
                "SELECT status, attempts, last_error_code, completed_at, available_at > now(), "
                "locked_by FROM job WHERE id = %s",
                (job_id,),
            ).fetchone()

    def make_due(agency_id, job_id):
        with tenant_transaction(setup, agency_id):
            setup.execute("UPDATE job SET available_at = now() WHERE id = %s", (job_id,))

    yield {
        "url": app_url, "connect": connect, "a": agency_a, "b": agency_b,
        "enqueue": enqueue, "row": job_row, "make_due": make_due,
    }
    for conn in conns:
        conn.close()


def claim(conn, agency_id, worker="w1", lease_seconds=60, job_types=("turn",)):
    return queue.claim_job(conn, agency_id, job_types, worker, lease_seconds)


def test_conversation_jobs_never_claimed_concurrently(queue_db):
    a = queue_db["a"]
    first = queue_db["enqueue"](a, "x1", CUSTOMER_X)
    second = queue_db["enqueue"](a, "x2", CUSTOMER_X)
    other = queue_db["enqueue"](a, "y1", CUSTOMER_Y)
    conn_1, conn_2 = queue_db["connect"](), queue_db["connect"]()

    job_1 = claim(conn_1, a, "w1")
    assert job_1.id == first and job_1.attempts == 1
    # x2 is older than y1 but its conversation is busy, so it is skipped.
    job_y = claim(conn_2, a, "w2")
    assert job_y.id == other
    assert queue.finish_job(conn_2, job_y, "w2") == "succeeded"
    assert claim(conn_2, a, "w2") is None

    assert queue.finish_job(conn_1, job_1, "w1") == "succeeded"
    assert claim(conn_2, a, "w2").id == second


def test_workers_never_overlap_within_a_conversation(queue_db):
    a = queue_db["a"]
    job_ids = [
        queue_db["enqueue"](a, f"{key}-{n}", key)
        for n in range(3) for key in (CUSTOMER_X, CUSTOMER_Y)
    ]
    guard = threading.Lock()
    running, peak = defaultdict(int), defaultdict(int)
    state = {"total": 0, "peak_total": 0, "done": 0}
    stop = threading.Event()

    def handler(conn, job):
        key = job.conversation_key
        with guard:
            running[key] += 1
            state["total"] += 1
            peak[key] = max(peak[key], running[key])
            state["peak_total"] = max(state["peak_total"], state["total"])
        time.sleep(0.2)
        with guard:
            running[key] -= 1
            state["total"] -= 1
            state["done"] += 1
            if state["done"] == len(job_ids):
                stop.set()

    workers = [
        Worker(queue_db["url"], [a], {"turn": handler}, stop, name=f"t{i}", poll_seconds=0.02)
        for i in range(3)
    ]
    threads = [threading.Thread(target=w.run) for w in workers]
    for thread in threads:
        thread.start()
    finished = stop.wait(30)
    stop.set()
    for thread in threads:
        thread.join(10)

    assert finished
    assert dict(peak) == {CUSTOMER_X: 1, CUSTOMER_Y: 1}
    assert state["peak_total"] == 2  # different conversations did run in parallel
    assert {queue_db["row"](a, job_id)[0] for job_id in job_ids} == {"succeeded"}


def test_failing_job_dead_letters_after_max_attempts(queue_db):
    a = queue_db["a"]
    job_id = queue_db["enqueue"](a, "boom", CUSTOMER_X)
    calls = []

    def handler(conn, job):
        calls.append(job.attempts)
        raise RuntimeError("fictional failure with customer text that must not be stored")

    worker = Worker(
        queue_db["url"], [a], {"turn": handler}, threading.Event(), backoff=lambda attempts: 0
    )
    try:
        while worker.run_once():
            pass
    finally:
        worker.close()

    assert calls == [1, 2, 3]
    status, attempts, code, completed_at, _, locked_by = queue_db["row"](a, job_id)
    assert (status, attempts, code, locked_by) == ("dead", 3, "RuntimeError", None)
    assert completed_at is not None
    # The conversation lock was released with the final status.
    other = queue_db["connect"]()
    assert queue.try_lock_conversation(other, a, CUSTOMER_X)


def test_permanent_error_dead_letters_immediately(queue_db):
    a = queue_db["a"]
    job_id = queue_db["enqueue"](a, "perm", CUSTOMER_X)

    def handler(conn, job):
        raise queue.PermanentJobError("unsupported_message")

    worker = Worker(queue_db["url"], [a], {"turn": handler}, threading.Event())
    try:
        assert worker.run_once()
        assert not worker.run_once()
    finally:
        worker.close()

    assert queue_db["row"](a, job_id)[:3] == ("dead", 1, "unsupported_message")


def test_retry_waits_for_backoff_then_succeeds(queue_db):
    a = queue_db["a"]
    job_id = queue_db["enqueue"](a, "flaky", CUSTOMER_X)
    calls = []

    def handler(conn, job):
        calls.append(job.attempts)
        if job.attempts == 1:
            # Work done inside a failed handler transaction is rolled back.
            with tenant_transaction(conn, job.agency_id):
                conn.execute("UPDATE job SET payload = '{\"touched\": true}' WHERE id = %s",
                             (job.id,))
                raise TimeoutError

    worker = Worker(
        queue_db["url"], [a], {"turn": handler}, threading.Event(), backoff=lambda attempts: 60
    )
    try:
        assert worker.run_once()
        status, attempts, code, _, delayed, _ = queue_db["row"](a, job_id)
        assert (status, attempts, code, delayed) == ("pending", 1, "TimeoutError", True)
        assert not worker.run_once()  # not due yet

        queue_db["make_due"](a, job_id)
        assert worker.run_once()
    finally:
        worker.close()

    assert calls == [1, 2]
    assert queue_db["row"](a, job_id)[:3] == ("succeeded", 2, None)


def test_delayed_job_is_claimed_only_when_due(queue_db):
    a = queue_db["a"]
    job_id = queue_db["enqueue"](a, "later", CUSTOMER_X, delay_seconds=60)
    conn = queue_db["connect"]()

    assert claim(conn, a) is None
    queue_db["make_due"](a, job_id)
    assert claim(conn, a).id == job_id


def test_enqueue_is_idempotent_on_dedupe_key(queue_db):
    a = queue_db["a"]
    assert queue_db["enqueue"](a, "same") is not None
    assert queue_db["enqueue"](a, "same") is None


def test_reaper_requeues_only_when_owner_is_gone(queue_db):
    a = queue_db["a"]
    job_id = queue_db["enqueue"](a, "crash", CUSTOMER_X)
    owner, reaper = queue_db["connect"](), queue_db["connect"]()

    assert claim(owner, a, lease_seconds=0).id == job_id
    time.sleep(0.05)
    # Lease expired, but the owner's connection still holds the conversation lock.
    assert queue.reap_expired(reaper, a) == 0
    assert queue_db["row"](a, job_id)[0] == "running"

    owner.close()  # simulated crash: Postgres drops the session lock
    assert queue.reap_expired(reaper, a) == 1
    status, attempts, code, *_ = queue_db["row"](a, job_id)
    assert (status, attempts, code) == ("pending", 1, "lease_expired")
    # The reaper released the lock it used for the check.
    assert claim(reaper, a, "w2").id == job_id


def test_reaper_dead_letters_exhausted_job(queue_db):
    a = queue_db["a"]
    job_id = queue_db["enqueue"](a, "last-try", CUSTOMER_X)
    with tenant_transaction(queue_db["connect"](), a) as conn:
        conn.execute("UPDATE job SET max_attempts = 1 WHERE id = %s", (job_id,))
    owner, reaper = queue_db["connect"](), queue_db["connect"]()

    claim(owner, a, lease_seconds=0)
    owner.close()
    time.sleep(0.05)

    assert queue.reap_expired(reaper, a) == 1
    assert queue_db["row"](a, job_id)[:3] == ("dead", 1, "lease_expired")


def test_finish_after_lost_lease_does_not_overwrite(queue_db):
    a = queue_db["a"]
    job_id = queue_db["enqueue"](a, "status-job")  # no conversation lock
    owner, other = queue_db["connect"](), queue_db["connect"]()

    job = claim(owner, a, "w1", lease_seconds=0)
    time.sleep(0.05)
    assert queue.reap_expired(other, a) == 1
    assert claim(other, a, "w2").id == job_id

    assert queue.finish_job(owner, job, "w1") is None
    assert queue_db["row"](a, job_id)[0] == "running"
    assert queue_db["row"](a, job_id)[5] == "w2"


def test_worker_only_claims_its_tenant_and_registered_types(queue_db):
    a, b = queue_db["a"], queue_db["b"]
    for_b = queue_db["enqueue"](b, "b-job", CUSTOMER_X)
    unregistered = queue_db["enqueue"](a, "other-type", job_type="not_registered")
    conn = queue_db["connect"]()

    assert claim(conn, a) is None
    assert claim(conn, b).id == for_b
    assert queue_db["row"](a, unregistered)[0] == "pending"


def test_unclaimed_lock_is_released_when_claim_fails(queue_db, monkeypatch):
    a = queue_db["a"]
    queue_db["enqueue"](a, "x1", CUSTOMER_X)
    conn = queue_db["connect"]()
    original = conn.execute

    def failing_update(query, *args, **kwargs):
        if str(query).lstrip().startswith("UPDATE job"):
            raise psycopg.errors.QueryCanceled("forced")
        return original(query, *args, **kwargs)

    monkeypatch.setattr(conn, "execute", failing_update)
    with pytest.raises(psycopg.errors.QueryCanceled):
        claim(conn, a)
    monkeypatch.undo()

    assert queue.try_lock_conversation(queue_db["connect"](), a, CUSTOMER_X)


@pytest.mark.parametrize(("attempts", "expected"), [(1, 5), (2, 10), (3, 20), (7, 300), (20, 300)])
def test_backoff_doubles_and_caps(attempts, expected):
    assert queue.backoff_seconds(attempts, rng=lambda: 0.5) == expected
    assert expected * 0.8 <= queue.backoff_seconds(attempts) <= expected * 1.2


def test_error_code_never_carries_the_message():
    assert queue.error_code(ValueError("Customer said: DHA Phase 6")) == "ValueError"
    assert queue.error_code(queue.PermanentJobError("bad_payload")) == "bad_payload"
