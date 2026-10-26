"""End-to-end tests: a real uvicorn server process, real HTTP calls, and a real local callback receiver.

These hit the service the way a user would: start `uvicorn app.main:app` on a
temporary database (REMINDERS_DB), talk to it over the network, and watch what
arrives at the receiver. Default timings are used (one check per second).
"""

import os
import socket
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from tests.receiver import wait_for

REPO_ROOT = Path(__file__).resolve().parent.parent


# A free local port number (bound once to get one, then closed).
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# True once the server answers HTTP: an unknown reminder id gives 404.
def answers(client):
    try:
        return client.get(f"/reminders/{uuid.uuid4()}").status_code == 404
    except httpx.TransportError:
        return False


# Runs `uvicorn app.main:app` as its own process on a temporary database, waits
# until it answers, hands the tests an HTTP client for it, then shuts it down.
# The server's log goes to a file that is shown if it never comes up.
@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    port = free_port()
    log_path = tmp / "server.log"
    with open(log_path, "w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=REPO_ROOT,
            env={**os.environ, "REMINDERS_DB": str(tmp / "e2e.db")},
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5)
    try:
        try:
            wait_for(lambda: answers(client), timeout=10)
        except AssertionError:
            raise AssertionError(f"server did not start:\n{log_path.read_text()}") from None
        yield client
    finally:
        client.close()
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# A fire_at string `seconds` from now, in UTC with a "Z" suffix.
def utc_in(seconds):
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


# Creates a reminder on the real server and returns its id.
def create(server, url, payload, fire_at):
    r = server.post("/reminders", json={"fire_at": fire_at, "callback_url": url, "payload": payload})
    assert r.status_code == 201
    rid = r.json()["id"]
    assert r.headers["location"] == f"/reminders/{rid}"
    return rid


# The reminder's current status as GET returns it.
def status(server, rid):
    return server.get(f"/reminders/{rid}").json()["status"]


# The full flow: create a reminder due in about 1s, see it pending, receive exactly
# one POST with the exact JSON body, then see it as sent.
def test_full_flow_create_fire_get(server, receiver):
    rid = create(server, receiver.url("/e2e"), {"hello": "world", "n": 1}, utc_in(1))
    assert status(server, rid) == "pending"
    wait_for(lambda: status(server, rid) == "sent", timeout=5)
    [request] = receiver.requests
    assert (request.method, request.path) == ("POST", "/e2e")
    assert request.body == b'{"hello":"world","n":1}'
    assert request.headers["Content-Type"] == "application/json"


# A reminder cancelled before its fire_at is never received. A second reminder
# with the same fire_at proves the server did reach that moment (both are due
# in the same check, so the cancelled one would have gone out with it). fire_at is
# 2s out so the two POSTs and the DELETE finish first, even on a slow machine.
def test_cancelled_reminder_is_never_sent(server, receiver):
    fire_at = utc_in(2)
    cancelled = create(server, receiver.url("/cancelled"), {"n": 2}, fire_at)
    control = create(server, receiver.url("/control"), {"n": 3}, fire_at)
    r = server.delete(f"/reminders/{cancelled}")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    wait_for(lambda: status(server, control) == "sent", timeout=5)
    time.sleep(0.5)  # room for any send started in the same check to arrive
    assert [req.path for req in receiver.requests] == ["/control"]
    assert status(server, cancelled) == "cancelled"
    assert server.delete(f"/reminders/{cancelled}").status_code == 200


# Once a reminder has been sent, DELETE is too late: 409 with the current status.
def test_delete_after_send_is_too_late(server, receiver):
    rid = create(server, receiver.url("/early"), {"n": 4}, "2000-01-01T00:00:00Z")
    wait_for(lambda: status(server, rid) == "sent", timeout=5)
    r = server.delete(f"/reminders/{rid}")
    assert r.status_code == 409
    assert r.json() == {"detail": "Reminder can no longer be cancelled; current status is 'sent'.", "status": "sent"}
