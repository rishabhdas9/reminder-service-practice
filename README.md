# Reminders API

A small FastAPI service that stores reminders and, when a reminder's `fire_at` time arrives, sends an HTTP POST to its `callback_url` with its `payload` as the JSON body. A cancelled reminder is never sent. Reminders are kept in SQLite, so they survive restarts and crashes.

Built as an interview take-home. The full design, every decision, and the reasoning behind it are in [plan.md](plan.md).

## Run it

Requires [uv](https://docs.astral.sh/uv/). Python 3.14 is pinned in `.python-version`.

```sh
uv sync
uv run uvicorn app.main:app      # serves http://127.0.0.1:8000 (interactive docs at /docs)
uv run pytest -q                 # 66 tests in about 14s, including end-to-end tests that start a real server
```

The database file is `reminders.db` in the current folder. Set `REMINDERS_DB=/path/to/file.db` to use another one.

## API

### POST /reminders

```sh
curl -i -X POST http://127.0.0.1:8000/reminders \
  -H 'Content-Type: application/json' \
  -d '{"fire_at": "2026-10-26T10:00:00Z", "callback_url": "http://127.0.0.1:9000/hook", "payload": {"msg": "hello"}}'
```

Returns `201 Created` with header `Location: /reminders/<id>` and body `{"id": "<uuid>"}`.

| Field | Rules |
|---|---|
| `fire_at` | Required. An ISO 8601 time in UTC, ending in `Z` or `+00:00`. A time with no timezone, a different offset (like `+05:30`), or a Unix timestamp gets `422`. A time in the past (or right now) is accepted and fires on the next check, within about a second. |
| `callback_url` | Required. An absolute `http` or `https` URL. Local and internal hosts such as `localhost` are allowed. |
| `payload` | Required. Any JSON value (object, array, string, number, true/false, null), at most 64 KB as compact UTF-8 JSON. `NaN`, `Infinity`, and text with lone surrogate characters are rejected because they can't be sent as valid JSON. |

Unknown fields get `422`.

### GET /reminders/{id}

```sh
curl http://127.0.0.1:8000/reminders/3f0c6d2e-8a51-4c1f-9a63-2b7d4e5f6a70
```

```json
{
  "id": "3f0c6d2e-8a51-4c1f-9a63-2b7d4e5f6a70",
  "fire_at": "2026-10-26T10:00:00Z",
  "callback_url": "http://127.0.0.1:9000/hook",
  "payload": {"msg": "hello"},
  "status": "sent",
  "created_at": "2026-10-26T09:59:12.412345Z",
  "cancelled_at": null,
  "attempted_at": "2026-10-26T10:00:00.731204Z",
  "response_status": 200,
  "error": null
}
```

`attempted_at` is when the POST started, `response_status` is the receiver's HTTP status, and `error` says why a send failed. An unknown id gets `404`; an id that is not a UUID gets `422`.

### DELETE /reminders/{id}

```sh
curl -i -X DELETE http://127.0.0.1:8000/reminders/3f0c6d2e-8a51-4c1f-9a63-2b7d4e5f6a70
```

| Status when DELETE arrives | Response |
|---|---|
| `pending` | `200` with the reminder, now `cancelled` (with `cancelled_at`) |
| `cancelled` | `200` with the same reminder (safe to repeat) |
| `sending`, `sent` or `failed` | `409` `{"detail": "Reminder can no longer be cancelled; current status is 'sent'.", "status": "sent"}` |
| unknown id | `404` |
| id that is not a UUID | `422` |

**What 409 means here: too late to cancel.** The request is valid and the reminder exists, but it is being sent right now, was already sent, or already had its one attempt. Answering "cancelled" would be false, so the server refuses. Repeating the DELETE never changes the answer; GET shows the final status.

## How a reminder moves through statuses

```
             DELETE
 pending ───────────────► cancelled
    │
    │ sender claims it (one atomic update, only once fire_at <= now)
    ▼
 sending ──► sent     (2xx)
    └──────► failed   (error / timeout / non-2xx / 3xx / server died mid-send)
```

Guarantees, and how each is enforced:

1. **Never sent after a successful cancel.** DELETE and the sender both change a reminder only with `UPDATE ... WHERE status = 'pending'`, and SQLite runs one write transaction at a time, so exactly one of them wins. Once DELETE has returned 200, the sender's claim can no longer match.
2. **Sent at most once.** Only the sender's claim moves a reminder out of `pending`, nothing ever moves it back, and there are no retries.
3. **Never early.** A reminder is claimed only when `fire_at <= now`.
4. **Durable once acknowledged.** POST returns the id only after the transaction commits (WAL mode, `synchronous=FULL`).
5. **Never stuck.** Each claim has a lease (send timeout of 10s plus 30s). If the server dies mid-send, the reminder becomes `failed` with "server stopped during delivery; outcome unknown" once the lease passes. It is not resent, because the receiver may already have it.

## Delivery

- One attempt per reminder. A `2xx` answer means `sent`. Anything else means `failed`, with the reason saved:
  - a non-2xx status, including `3xx` (redirects are not followed): `"HTTP <code>"`
  - no response status within 10 seconds in total (connecting, sending, and waiting for the answer's headers): `"timed out after 10.0s"`
  - a connection problem: the error's name and message
- The body is the payload exactly as stored, with `Content-Type: application/json`. Only the response's status code is read; its body is never read.
- The sender checks once per second and runs at most 20 sends at a time.

## Design decisions in short

- **SQLite with explicit transactions.** Every write is a short `BEGIN IMMEDIATE ... COMMIT`, so a crash leaves each reminder fully written or untouched. No transaction is ever held open during an HTTP call.
- **The sender runs inside the API process**, started and stopped with the app. Shutdown waits for sends already in progress.
- **Times** are stored as integer UTC microseconds and returned as ISO 8601 ending in `Z`, so they come back to the exact microsecond (a `+00:00` input is returned with `Z`).
- **Random UUID ids**, so nobody can guess another person's reminder.
- **Layout:** `app/store.py` (the only module with SQL), `app/schemas.py` (validation and response shape), `app/dispatcher.py` (sending), `app/main.py` (routes and startup). Tests live in `tests/`, including a small local HTTP receiver (`tests/receiver.py`).

## Known limitations

- **No authentication.** Anyone who can reach the server can create, read and cancel reminders.
- **SSRF is intentionally not blocked.** Any `http(s)` callback URL is allowed, including `localhost` and internal hosts, so local receivers work. This is a local-only service. Before it is ever exposed publicly, it would need egress filtering. The `error` and `response_status` shown by GET also reveal whether an internal host or port answered.
- **No idempotency key on POST.** A client that retries a POST after a network error can create a duplicate reminder.
- **One machine.** SQLite is a local file. Several server processes on the same machine are safe (atomic claims and leases); several machines sharing one file are not.
- **Rows are kept forever.** There is no cleanup job.
- **Plain HTTP.** For TLS, put a proxy in front or use uvicorn's `--ssl-keyfile`/`--ssl-certfile`.
- **Request size.** The 64 KB payload limit is checked after the request body is parsed, so the app does not cap total request size.
- **At-most-once delivery.** A crash mid-send ends as `failed` ("outcome unknown") and is never resent.
- **About 1 second of timing precision** (one check per second).

## Manual check: a crash during a send

1. Start a receiver that holds `/slow` open for 60s and prints each request: `uv run python tests/receiver.py 9123 60`
2. Start the server: `REMINDERS_DB=/tmp/crash.db .venv/bin/python -m uvicorn app.main:app --port 8123`. Use `.venv/bin/python` rather than `uv run` so the process you `kill -9` is the server itself (killing `uv run` leaves the server running).
3. POST a reminder with `fire_at` set to now and `callback_url` `http://127.0.0.1:9123/slow`. About 2 seconds later, GET shows `sending`.
4. Within 10 seconds of step 3 (after that, the send's own 10s timeout marks it `failed` "timed out" instead), `kill -9` the server and start it again. GET still shows `sending`.
5. About 40 seconds after the send started, GET shows `failed` with "server stopped during delivery; outcome unknown". The receiver printed exactly one request.
