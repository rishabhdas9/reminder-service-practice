"""Tests for the HTTP API in app/main.py: validation, the three routes, and status codes."""

import uuid

import pytest

from app.store import ReminderStore, now_us

FIRE_AT = "2026-10-26T10:00:00Z"
LEASE = 40_000_000  # 40s lease, in microseconds


# Posts a reminder body. Fields passed in `overrides` replace the defaults; a
# value of None removes that field so tests can leave it out.
def post(client, **overrides):
    body = {"fire_at": FIRE_AT, "callback_url": "http://example.com/hook", "payload": {"a": 1}}
    body.update(overrides)
    body = {k: v for k, v in body.items() if v is not None}
    return client.post("/reminders", json=body)


# Creates a reminder and returns its id.
def create(client, **overrides):
    return post(client, **overrides).json()["id"]


# Creates a reminder due in the past, then moves it to 'sending' as the sender would.
def make_sending(client, db_path):
    rid = create(client, fire_at="2000-01-01T00:00:00Z")
    ReminderStore(db_path).claim_due(now_us(), 10, LEASE)
    return rid


# A valid POST returns 201, a body with only the id, and a Location header for it.
def test_post_valid_returns_201_with_id_and_location(client):
    r = post(client)
    assert r.status_code == 201
    body = r.json()
    assert list(body) == ["id"]
    uuid.UUID(body["id"])
    assert r.headers["location"] == f"/reminders/{body['id']}"


# fire_at in Z form and +00:00 form are accepted and come back with a trailing Z.
@pytest.mark.parametrize(
    "sent, returned",
    [
        ("2026-10-26T10:00:00Z", "2026-10-26T10:00:00Z"),
        ("2026-10-26T10:00:00+00:00", "2026-10-26T10:00:00Z"),
        ("2026-10-26T10:00:00.123456Z", "2026-10-26T10:00:00.123456Z"),
    ],
)
def test_fire_at_accepted_and_returned_with_z(client, sent, returned):
    rid = create(client, fire_at=sent)
    assert client.get(f"/reminders/{rid}").json()["fire_at"] == returned


# fire_at must be an ISO 8601 UTC string: no zone, other offsets, numbers, and junk are refused.
@pytest.mark.parametrize(
    "bad",
    ["2026-10-26T10:00:00", "2026-10-26T10:00:00+05:30", 1700000000, "1700000000", "not a date", None],
)
def test_fire_at_rejected(client, bad):
    assert post(client, fire_at=bad).status_code == 422


# A fire_at in the past is accepted; it will be sent on the sender's next check.
def test_past_fire_at_accepted(client):
    assert post(client, fire_at="2000-01-01T00:00:00Z").status_code == 201


# callback_url must be an absolute http or https URL.
@pytest.mark.parametrize("bad", ["ftp://example.com/x", "/relative/path", "not a url", None])
def test_callback_url_rejected(client, bad):
    assert post(client, callback_url=bad).status_code == 422


# payload is required: leaving it out is refused.
def test_payload_is_required(client):
    body = {"fire_at": FIRE_AT, "callback_url": "http://example.com/hook"}
    assert client.post("/reminders", json=body).status_code == 422


# Any JSON value (null, a list, a string) is accepted and comes back unchanged.
@pytest.mark.parametrize("value", [None, [1, "two", {"three": 3}], "hello"])
def test_payload_any_json_value_accepted(client, value):
    body = {"fire_at": FIRE_AT, "callback_url": "http://example.com/hook", "payload": value}
    r = client.post("/reminders", json=body)
    assert r.status_code == 201
    assert client.get(f"/reminders/{r.json()['id']}").json()["payload"] == value


# A payload over 64 KB of JSON is refused; one just under the limit is accepted.
def test_payload_size_limit(client):
    assert post(client, payload="x" * (64 * 1024 - 2)).status_code == 201  # 2 bytes of quotes
    assert post(client, payload="x" * (64 * 1024 - 1)).status_code == 422


# NaN and Infinity are not valid JSON; a payload containing them is refused (422), not stored.
@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_payload_nan_and_infinity_rejected(client, token):
    raw = '{"fire_at":"2030-01-01T00:00:00Z","callback_url":"http://example.com/h","payload":{"x":%s}}' % token
    r = client.post("/reminders", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 422


# The 422 error body repeats the bad input, so it must stay valid JSON even when
# that input is NaN (here in fire_at, which fails its own check).
def test_nan_in_rejected_field_still_gives_422(client):
    raw = '{"fire_at":NaN,"callback_url":"http://example.com/h","payload":1}'
    r = client.post("/reminders", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 422


# Fields the API does not define are refused instead of silently dropped.
def test_unknown_field_rejected(client):
    assert post(client, surprise="x").status_code == 422


# GET returns every field of the reminder; a new one is pending with no send details.
def test_get_returns_full_reminder(client):
    rid = create(client, payload={"msg": "hi"})
    r = client.get(f"/reminders/{rid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == rid
    assert body["fire_at"] == FIRE_AT
    assert body["callback_url"] == "http://example.com/hook"
    assert body["payload"] == {"msg": "hi"}
    assert body["status"] == "pending"
    assert body["created_at"].endswith("Z")
    assert body["cancelled_at"] is None
    assert body["attempted_at"] is None
    assert body["response_status"] is None
    assert body["error"] is None


# GET on an id that does not exist is 404; a malformed id is 422.
def test_get_unknown_and_malformed_id(client):
    r = client.get(f"/reminders/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json() == {"detail": "Reminder not found"}
    assert client.get("/reminders/not-a-uuid").status_code == 422


# DELETE on a pending reminder cancels it and records when.
def test_delete_pending_cancels(client):
    rid = create(client)
    r = client.delete(f"/reminders/{rid}")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert r.json()["cancelled_at"].endswith("Z")
    assert client.get(f"/reminders/{rid}").json()["status"] == "cancelled"


# DELETE twice is harmless: 200 again, with the original cancel time.
def test_delete_twice_keeps_first_cancelled_at(client):
    rid = create(client)
    first = client.delete(f"/reminders/{rid}").json()
    second = client.delete(f"/reminders/{rid}")
    assert second.status_code == 200
    assert second.json()["cancelled_at"] == first["cancelled_at"]


# DELETE after sending has started or finished is 409, with the current status in the body.
@pytest.mark.parametrize("final", ["sending", "sent", "failed"])
def test_delete_too_late_returns_409(client, db_path, final):
    rid = make_sending(client, db_path)
    if final == "sent":
        ReminderStore(db_path).mark_sent(rid, 200)
    elif final == "failed":
        ReminderStore(db_path).mark_failed(rid, 500, "HTTP 500")
    r = client.delete(f"/reminders/{rid}")
    assert r.status_code == 409
    assert r.json() == {
        "detail": f"Reminder can no longer be cancelled; current status is '{final}'.",
        "status": final,
    }


# DELETE on an id that does not exist is 404; a malformed id is 422.
def test_delete_unknown_and_malformed_id(client):
    r = client.delete(f"/reminders/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json() == {"detail": "Reminder not found"}
    assert client.delete("/reminders/not-a-uuid").status_code == 422
