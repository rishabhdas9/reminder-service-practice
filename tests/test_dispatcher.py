"""Tests for app/dispatcher.py: the running app sends due reminders to a real local HTTP receiver."""

import asyncio
import json
import socket
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.dispatcher import Dispatcher
from app.main import create_app
from app.schemas import datetime_to_us
from app.store import LEASE_EXPIRED_ERROR, now_us
from tests.receiver import wait_for

PAST = "2000-01-01T00:00:00Z"
LONG_AGO_US = datetime_to_us(datetime(2000, 1, 1, tzinfo=UTC))
SEND_TIMEOUT = 0.5


# The real app on `db_path` with short timings (a check every 50ms). Use it in a
# `with` block: entering starts the dispatcher, leaving stops it.
def start_app(db_path):
    return TestClient(create_app(db_path, poll_interval=0.05, send_timeout=SEND_TIMEOUT, max_in_flight=20))


# A running app on the test's database. It depends on `receiver` so the receiver
# stays up until the app has shut down and finished any send.
@pytest.fixture
def live(store, receiver):
    with start_app(store.db_path) as client:
        yield client


# Creates a reminder through the API and returns its id.
def create(client, url, payload, fire_at=PAST):
    r = client.post("/reminders", json={"fire_at": fire_at, "callback_url": url, "payload": payload})
    assert r.status_code == 201
    return r.json()["id"]


# The reminder as GET returns it.
def get(client, rid):
    return client.get(f"/reminders/{rid}").json()


# Waits until the reminder is sent or failed, then returns it.
def wait_done(client, rid, timeout=5.0):
    wait_for(lambda: get(client, rid)["status"] in ("sent", "failed"), timeout)
    return get(client, rid)


# A local port with nothing listening on it (bound once to get a free number, then closed).
def unused_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# A due reminder is POSTed with the stored compact JSON as its body, then shows as sent.
def test_due_reminder_is_delivered(live, receiver):
    payload = {"n": 1, "msg": "hi there", "list": [1, 2]}
    rid = create(live, receiver.url("/hook"), payload, fire_at=datetime.now(UTC).isoformat())
    body = wait_done(live, rid)
    assert body["status"] == "sent"
    assert body["response_status"] == 200
    assert body["attempted_at"] is not None
    [request] = receiver.requests
    assert request.method == "POST"
    assert request.path == "/hook"
    assert request.body == b'{"n":1,"msg":"hi there","list":[1,2]}'
    assert request.headers["Content-Type"] == "application/json"


# A fire_at long in the past is sent by the very first check. This drives a
# Dispatcher directly, so "first check" means exactly one call to tick().
def test_past_fire_at_sent_on_first_check(store, receiver):
    rid = store.create(LONG_AGO_US, receiver.url("/past"), '{"n":2}', now_us())

    # Starts a dispatcher whose own timer never fires here, runs one check, then stops it.
    async def one_check():
        dispatcher = Dispatcher(store, poll_interval=60, send_timeout=SEND_TIMEOUT, max_in_flight=20)
        dispatcher.start()
        await dispatcher.tick()
        await dispatcher.stop()

    asyncio.run(one_check())
    assert store.get(rid)["status"] == "sent"
    assert [r.body for r in receiver.requests] == [b'{"n":2}']


# A reminder due about 1s from now is not sent before its fire_at, and is sent after it.
def test_never_sent_early(live, receiver):
    fire_at = datetime.now(UTC) + timedelta(seconds=1)
    rid = create(live, receiver.url("/soon"), {"n": 3}, fire_at=fire_at.isoformat())
    assert get(live, rid)["status"] == "pending"
    wait_for(lambda: len(receiver.requests) == 1)
    assert receiver.requests[0].arrived_at >= fire_at.timestamp()
    assert wait_done(live, rid)["status"] == "sent"


# A reminder cancelled before its fire_at is never sent, even 2s after fire_at.
# A second reminder with the same fire_at proves the dispatcher did reach that moment.
# fire_at is 2s out so the two POSTs and the DELETE finish first, even on a slow machine.
def test_cancelled_is_never_sent(live, receiver):
    fire_at = datetime.now(UTC) + timedelta(seconds=2)
    cancelled = create(live, receiver.url("/cancelled"), {"n": 4}, fire_at=fire_at.isoformat())
    control = create(live, receiver.url("/control"), {"n": 5}, fire_at=fire_at.isoformat())
    assert live.delete(f"/reminders/{cancelled}").status_code == 200
    assert wait_done(live, control)["status"] == "sent"
    time.sleep(max(0.0, fire_at.timestamp() + 2 - time.time()))
    assert [r.path for r in receiver.requests] == ["/control"]
    assert get(live, cancelled)["status"] == "cancelled"


# A 500 from the receiver marks the reminder failed, with the status and the reason.
def test_server_error_marks_failed(live, receiver):
    body = wait_done(live, create(live, receiver.url("/fail"), {"n": 6}))
    assert (body["status"], body["response_status"], body["error"]) == ("failed", 500, "HTTP 500")


# A receiver slower than send_timeout marks the reminder failed as timed out.
def test_slow_receiver_times_out(live, receiver):
    assert receiver.slow_seconds > SEND_TIMEOUT
    body = wait_done(live, create(live, receiver.url("/slow"), {"n": 7}))
    assert body["status"] == "failed"
    assert body["response_status"] is None
    assert body["error"].startswith("timed out")


# Only the status code matters: a receiver that answers 200 but then stalls (or
# streams a huge body) still counts as sent, because the body is never read.
def test_response_body_is_not_read(live, receiver):
    assert receiver.slow_seconds > SEND_TIMEOUT
    body = wait_done(live, create(live, receiver.url("/stall-body"), {"n": 12}))
    assert body["status"] == "sent"
    assert body["response_status"] == 200


# A redirect is not followed: the reminder fails with the 302, and /ok is never requested.
def test_redirect_is_not_followed(live, receiver):
    body = wait_done(live, create(live, receiver.url("/redirect"), {"n": 8}))
    assert (body["status"], body["response_status"], body["error"]) == ("failed", 302, "HTTP 302")
    assert [r.path for r in receiver.requests] == ["/redirect"]


# A callback URL that nothing listens on marks the reminder failed, with no status and an error.
def test_connection_refused_marks_failed(live):
    body = wait_done(live, create(live, f"http://127.0.0.1:{unused_port()}/x", {"n": 9}))
    assert body["status"] == "failed"
    assert body["response_status"] is None
    assert body["error"]


# 50 reminders due at once, with at most 20 sends in flight, are each delivered exactly once.
def test_burst_all_delivered_once(live, receiver):
    ids = [create(live, receiver.url("/burst"), {"n": i}) for i in range(50)]
    wait_for(lambda: all(get(live, rid)["status"] == "sent" for rid in ids), timeout=10)
    assert sorted(json.loads(r.body)["n"] for r in receiver.requests) == list(range(50))


# A send left in 'sending' past its lease (as after a crash) is turned into failed
# by the running dispatcher and is never delivered. The row is set up before the
# app starts, so the dispatcher cannot claim it first.
def test_expired_lease_becomes_failed(store, receiver):
    rid = store.create(LONG_AGO_US, receiver.url("/lease"), '{"n":10}', LONG_AGO_US)
    [claimed] = store.claim_due(LONG_AGO_US, 1, 1)  # the lease ended 1 microsecond later, in 2000
    assert claimed["id"] == rid
    with start_app(store.db_path) as client:
        body = wait_done(client, rid)
    assert body["status"] == "failed"
    assert body["error"] == LEASE_EXPIRED_ERROR
    assert receiver.requests == []
