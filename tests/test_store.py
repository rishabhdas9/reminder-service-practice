"""Tests for app/store.py: every status change, and the cancel-vs-claim race."""

import random
import threading

from app.store import ReminderStore

NOW = 1_800_000_000_000_000  # a fixed "current time" in UTC microseconds
LEASE = 40_000_000  # 40s lease, in microseconds


# Creates one reminder due at `fire_at_us` and returns its id.
def make(store, fire_at_us=NOW, payload='{"a":1}'):
    return store.create(fire_at_us, "http://example.com/hook", payload, NOW - 1)


# A created reminder comes back with every field as stored and status pending.
def test_create_then_get_round_trips(store):
    rid = make(store, payload='{"k":[1,2]}')
    row = store.get(rid)
    assert row["id"] == rid
    assert row["fire_at_us"] == NOW
    assert row["callback_url"] == "http://example.com/hook"
    assert row["payload"] == '{"k":[1,2]}'
    assert row["status"] == "pending"
    assert row["created_at_us"] == NOW - 1


# Looking up an id that was never created returns None.
def test_get_unknown_returns_none(store):
    assert store.get("00000000-0000-0000-0000-000000000000") is None


# A committed reminder is still there when a new store opens the same file (simulated restart).
def test_reminder_survives_reopen(tmp_path):
    path = str(tmp_path / "durable.db")
    first = ReminderStore(path)
    first.init_schema()
    rid = make(first)
    second = ReminderStore(path)
    second.init_schema()
    assert second.get(rid)["status"] == "pending"


# Cancelling a pending reminder marks it cancelled and records when.
def test_cancel_pending_marks_cancelled(store):
    rid = make(store)
    row = store.cancel(rid, NOW)
    assert row["status"] == "cancelled"
    assert row["cancelled_at_us"] == NOW


# Cancelling twice is harmless: still cancelled, original cancel time kept.
def test_cancel_twice_keeps_first_cancel(store):
    rid = make(store)
    store.cancel(rid, NOW)
    row = store.cancel(rid, NOW + 5)
    assert row["status"] == "cancelled"
    assert row["cancelled_at_us"] == NOW


# Cancelling an unknown id returns None.
def test_cancel_unknown_returns_none(store):
    assert store.cancel("00000000-0000-0000-0000-000000000000", NOW) is None


# Once the dispatcher has claimed a reminder, cancel cannot change it.
def test_cancel_after_claim_is_too_late(store):
    rid = make(store)
    store.claim_due(NOW, 10, LEASE)
    row = store.cancel(rid, NOW)
    assert row["status"] == "sending"
    assert row["cancelled_at_us"] is None


# A cancelled reminder is never claimed for sending.
def test_claim_skips_cancelled(store):
    rid = make(store)
    store.cancel(rid, NOW)
    assert store.claim_due(NOW, 10, LEASE) == []


# Only reminders whose fire_at has arrived are claimed (fire_at == now counts as due).
def test_claim_only_due_reminders(store):
    due = make(store, fire_at_us=NOW)
    make(store, fire_at_us=NOW + 1)
    claimed = store.claim_due(NOW, 10, LEASE)
    assert [r["id"] for r in claimed] == [due]
    row = store.get(due)
    assert row["status"] == "sending"
    assert row["attempted_at_us"] == NOW
    assert row["lease_until_us"] == NOW + LEASE


# Claims take the oldest fire_at first and never more than the limit.
def test_claim_respects_limit_and_order(store):
    ids = [make(store, fire_at_us=NOW - i) for i in range(5)]  # ids[4] is the oldest
    claimed = store.claim_due(NOW, 2, LEASE)
    assert {r["id"] for r in claimed} == {ids[4], ids[3]}


# A reminder is handed out by claim_due at most once.
def test_claim_never_returns_same_reminder_twice(store):
    make(store)
    assert len(store.claim_due(NOW, 10, LEASE)) == 1
    assert store.claim_due(NOW, 10, LEASE) == []


# mark_sent records the receiver's status code; it only works on a reminder being sent.
def test_mark_sent_only_from_sending(store):
    rid = make(store)
    assert store.mark_sent(rid, 200) is False  # still pending
    store.claim_due(NOW, 10, LEASE)
    assert store.mark_sent(rid, 204) is True
    row = store.get(rid)
    assert (row["status"], row["response_status"]) == ("sent", 204)


# mark_failed records the reason; it only works on a reminder being sent.
def test_mark_failed_only_from_sending(store):
    rid = make(store)
    assert store.mark_failed(rid, None, "boom") is False  # still pending
    store.claim_due(NOW, 10, LEASE)
    assert store.mark_failed(rid, 500, "HTTP 500") is True
    row = store.get(rid)
    assert (row["status"], row["response_status"], row["error"]) == ("failed", 500, "HTTP 500")


# A send whose lease runs out (server died mid-send) becomes failed, never resent.
def test_expired_lease_becomes_failed(store):
    rid = make(store)
    store.claim_due(NOW, 10, LEASE)
    assert store.fail_expired_leases(NOW + LEASE) == 0  # lease still valid at its deadline
    assert store.fail_expired_leases(NOW + LEASE + 1) == 1
    row = store.get(rid)
    assert row["status"] == "failed"
    assert "outcome unknown" in row["error"]
    assert store.claim_due(NOW + LEASE + 1, 10, LEASE) == []


# Racing cancels against claims from two threads: every reminder ends up with exactly one winner.
def test_cancel_and_claim_race_has_exactly_one_winner(store):
    ids = [make(store) for _ in range(200)]
    cancelled, claimed = set(), set()
    start = threading.Barrier(2)
    cancel_done = threading.Event()

    # Cancels every reminder in random order, remembering which cancels won.
    def canceller():
        order = ids[:]
        random.shuffle(order)
        start.wait()
        for rid in order:
            if store.cancel(rid, NOW)["status"] == "cancelled":
                cancelled.add(rid)
        cancel_done.set()

    # Claims due reminders in small batches until nothing is left to claim.
    def claimer():
        start.wait()
        while True:
            batch = store.claim_due(NOW, 3, LEASE)
            claimed.update(r["id"] for r in batch)
            if not batch and cancel_done.is_set():
                break

    threads = [threading.Thread(target=canceller), threading.Thread(target=claimer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert cancelled.isdisjoint(claimed)
    assert cancelled | claimed == set(ids)
