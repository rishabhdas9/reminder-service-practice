# Reminders API - design and build plan

> The "Implementation rulings" section at the end overrides anything above it that disagrees.

## Context

Interview take-home. Build a small FastAPI service that stores reminders and, when each one's `fire_at` time arrives, sends an HTTP POST to its `callback_url` with its `payload` as the JSON body. Reviewers will judge correctness, design reasoning, code, and tests.

Hard rule from the brief: **a cancelled reminder must never be sent.**

The project folder (`/Users/rishabh/Desktop/practice`) is empty apart from `CLAUDE.md` and tool folders (`graft/`, `.remember/`). It is not a git repo. This is a new build with nothing to reuse.

## The brief (verbatim, from the user)

> I need to create an API service with HTTPS methods GET, POST, and DELETE. Create the file in Python and implement FastAPI into it. I'm giving you the 3 rules for which we will be building this API: POST /reminders takes fire_at (a UTC time), callback_url, and payload, and returns the reminder's id. GET /reminders/{id} to return a reminder and its status. DELETE /reminders/{id} to cancel it.
> - When fire_at arrives. Functionality check: When fire_at arrives, send an HTTP POST to callback_url with payload as the JSON body. A cancelled reminder must never be sent.

The brief contains only these three endpoints (no list endpoint). "HTTPS methods" means the HTTP methods GET, POST, and DELETE; the user didn't object to that reading. POST returning only `{"id"}` comes from the brief. Everything else below is either the user's decision or marked as Claude's call.

## Goals, non-goals, anti-goals

- **Goals:** the three endpoints from the brief; reminders survive crashes and restarts; delivery on time (within about 1s); the never-send-after-cancel rule holds even under races; tests that prove each rule.
- **Non-goals:** auth, retries, POST idempotency keys, multi-machine scaling, a cleanup job for old rows, serving TLS directly.
- **Anti-goals (a failure even if it "works"):** a cancelled reminder gets sent; a reminder fires early; a reminder is sent twice; a client gets an id for a reminder that a crash then loses; a reminder stays stuck in `sending` forever.

## Decisions made by the user

1. Purpose: interview take-home.
2. Storage: SQLite. Every write runs in an ACID transaction, so a crash or redeploy can't lose or corrupt data.
3. Delivery: **one attempt only.** A 2xx response means `sent`. A connection error, timeout, or non-2xx response means `failed`, with the reason stored.
4. `fire_at` is required and **must be UTC**: an RFC 3339 string ending in `Z` or `+00:00`. Anything else gets a 422.
5. A `fire_at` in the past or at the current moment is **accepted**, not rejected, and fires on the dispatcher's next check.
6. `callback_url`: any http(s) URL is allowed. The SSRF risk is documented, not blocked.
7. Code layout: a small package (`app/` with separate modules), not a single file.
8. DELETE keeps the row (soft cancel) and returns 409 when it's too late to cancel. A successful DELETE returns **200 with the reminder JSON**.
9. Hand-in: **a git repo with one commit per build slice.** Tests pass at every commit, so reviewers can follow the history.

## Decisions made by Claude (check these)

- **IDs:** random UUID4 strings. With no auth, sequential ids would let anyone cancel someone else's reminder by guessing.
- **Time storage:** integer UTC **microseconds** since the Unix epoch. (Earlier I said milliseconds. Microseconds match Python's datetime precision, so a submitted time comes back exactly as sent.) All API times are returned as ISO 8601 ending in `Z`.
- **Payload:** any JSON value, at most 64 KB once serialized; anything bigger gets a 422. It's stored as JSON text and sent as those exact bytes, with `Content-Type: application/json`.
- **Unknown request fields** get a 422 (`extra="forbid"`), so typos are caught instead of silently ignored.
- **Delivery:** a total deadline of 10s per callback, enforced with `asyncio.timeout` because httpx's own timeout applies per phase (connect, read, and so on), not to the whole request. Redirects are **not followed**, and a 3xx counts as `failed`. At most 20 sends run at once.
- **Status names** (`pending`, `sending`, `sent`, `failed`, `cancelled`) and the **409 body** shape are my calls. The user saw both during the DELETE discussion.
- **Reminders that came due while the server was down** are sent late, on the first check after restart. This follows from accepting past `fire_at` values. The GET fields `fire_at` and `attempted_at` show how late each one was.
- **`"payload": null`** is accepted, since null is a valid JSON value.
- **Settings** are read from environment variables in one place, with defaults: `REMINDERS_DB` (`reminders.db`), `REMINDERS_POLL_INTERVAL_S` (1), `REMINDERS_SEND_TIMEOUT_S` (10), `REMINDERS_MAX_IN_FLIGHT` (20). `create_app(settings)` takes them explicitly, so tests can pass values directly and the e2e subprocess can set them through env.
- **Crash during a send (changed from my earlier "fix it on startup" idea):** each claim records a lease deadline, `lease_until = claim time + send timeout + 30s`. The 30s margin is deliberately larger than SQLite's 5s `busy_timeout`, so a slow `mark_sent` that's waiting on the lock can't be beaten by another process's lease cleanup. On every tick, any `sending` row whose lease has passed becomes `failed` with the error "server stopped during delivery; outcome unknown". It is never resent. Startup-only cleanup would be wrong if two server processes share the database: a newly started process would mark the other one's in-flight sends as failed. The lease avoids that and also covers the single-process crash case.
- **Python 3.14** is pinned with uv (`.python-version`). 3.15.0 is brand new, and pydantic may not ship pre-built wheels for it yet.
- **Design write-up:** this document lives in the repo root as `plan.md` and is committed with slice 1. The README holds run instructions, the API, a short summary of the decisions, and the known limitations.
- **Dependencies:** `fastapi`, `uvicorn`, `httpx` (to send callbacks); dev: `pytest`. The database uses the stdlib `sqlite3` (SQLite 3.54, which supports `RETURNING`).

## Design

### API contract

**POST /reminders**
```json
{"fire_at": "2026-10-26T10:00:00Z", "callback_url": "https://example.com/hook", "payload": {"any": "json"}}
```
- `201 Created`, body `{"id": "<uuid>"}`, header `Location: /reminders/<uuid>`. The 201 goes out only after COMMIT.
- 422 for: a missing field, an unknown field, `fire_at` with no timezone or a non-zero offset, `fire_at` sent as a number, a non-http(s) or relative URL, or a payload over 64 KB.

**GET /reminders/{id}**: 200 with the Reminder; 404 for an unknown id; 422 for an id that isn't a UUID.

Reminder JSON (also returned by DELETE):
```json
{
  "id": "…", "fire_at": "2026-10-26T10:00:00Z", "callback_url": "https://example.com/hook",
  "payload": {"any": "json"}, "status": "pending | sending | sent | failed | cancelled",
  "created_at": "…Z", "cancelled_at": null, "attempted_at": null,
  "response_status": null, "error": null
}
```

**DELETE /reminders/{id}**

| Current status | Response |
|---|---|
| `pending` | 200, Reminder with `status: cancelled` and `cancelled_at` set |
| `cancelled` | 200, the same Reminder (safe to repeat) |
| `sending` / `sent` / `failed` | 409 `{"detail": "Reminder can no longer be cancelled; current status is 'sent'.", "status": "sent"}` |
| unknown id | 404 |
| id that isn't a UUID | 422 |

### Status lifecycle and the rules it guarantees

```
             DELETE
 pending ───────────────► cancelled
    │
    │ dispatcher claim (atomic, only when fire_at <= now)
    ▼
 sending ──► sent     (2xx)
    └──────► failed   (error / timeout / non-2xx / 3xx / lease expired)
```

- **R1 Never send after cancel:** cancel and claim both run `UPDATE … WHERE id=? AND status='pending'` inside `BEGIN IMMEDIATE` transactions. SQLite allows one writer at a time, so exactly one of them changes the row. A DELETE that returned 200 means the claim can never match.
- **R2 Send at most once:** only the claim moves a row out of `pending`, and nothing moves a row back to `pending`.
- **R3 Never early:** the claim requires `fire_at_us <= now`.
- **R4 Durable once acknowledged:** COMMIT happens before the 201, with `synchronous=FULL`.
- **R5 Never stuck:** an expired lease turns `sending` into `failed`.

### Storage (`app/store.py`)

- File path from env `REMINDERS_DB`, default `reminders.db`.
- One connection per operation (`sqlite3.connect(path, autocommit=True)`, so transactions are explicit). Pragmas: `journal_mode=WAL`, `synchronous=FULL`, `busy_timeout=5000`.
- Every write: `BEGIN IMMEDIATE` … `COMMIT`, with `ROLLBACK` on any error. **No transaction is open during the HTTP call.**
- Schema:
```sql
CREATE TABLE IF NOT EXISTS reminders (
  id              TEXT PRIMARY KEY,
  fire_at_us      INTEGER NOT NULL,
  callback_url    TEXT NOT NULL,
  payload         TEXT NOT NULL,
  status          TEXT NOT NULL CHECK (status IN ('pending','sending','sent','failed','cancelled')),
  created_at_us   INTEGER NOT NULL,
  cancelled_at_us INTEGER,
  attempted_at_us INTEGER,
  lease_until_us  INTEGER,
  response_status INTEGER,
  error           TEXT
);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders (fire_at_us) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS reminders_lease ON reminders (lease_until_us) WHERE status = 'sending';
```
- `ReminderStore` methods: `init_schema`, `create`, `get`, `cancel` (returns cancelled / already_cancelled / too_late / not_found plus the row), `claim_due(now, limit, lease_us)` (one `UPDATE … WHERE id IN (SELECT … ORDER BY fire_at_us LIMIT ?) AND status='pending' RETURNING …`), `mark_sent`, `mark_failed` (both conditional on `status='sending'`), `fail_expired_leases(now)`.

### Dispatcher (`app/dispatcher.py`)

An asyncio task started and stopped by FastAPI's lifespan hook. Every 1s:
1. `fail_expired_leases(now)`.
2. Claim up to `20 - in_flight` due reminders. Claiming only what can be sent right away means unclaimed reminders stay `pending` and are never needlessly lost to a crash.
3. For each claimed reminder, start a send task: POST the stored payload bytes with `follow_redirects=False` inside a 10s `asyncio.timeout`, then `mark_sent` or `mark_failed`.
4. A tick that raises is logged and the loop keeps going.

Database calls go through `asyncio.to_thread` so they don't block the event loop. On shutdown the loop stops claiming and waits for in-flight sends, each bounded by the 10s deadline. Settings come from the env-backed settings object described above. Tests and the e2e check set them small.

### Routes (`app/main.py`) and models (`app/schemas.py`)

- `create_app(settings)` builds the store, the dispatcher, and the routes. Module-level `app = create_app(Settings.from_env())` is what uvicorn loads.
- Route handlers are plain `def` functions (FastAPI runs them in its threadpool) and call `ReminderStore`.
- `schemas.py`: `ReminderCreate` (strict UTC `fire_at` that rejects numbers, `HttpUrl`, `JsonValue` payload with a size check, `extra="forbid"`), `ReminderCreated`, `Reminder`.

### Layout
```
pyproject.toml   .python-version   README.md
app/__init__.py  app/main.py  app/schemas.py  app/store.py  app/dispatcher.py
tests/conftest.py  tests/receiver.py  tests/test_store.py  tests/test_api.py
tests/test_dispatcher.py  tests/test_e2e.py
```
Follow CLAUDE.md: a comment at the top of every file, a comment before every function, plain language, no em dashes.

## Build slices (test-first in each)

Each slice ends with `uv run pytest -q` passing and then exactly one commit. The commit message describes the slice and ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

1. **Foundation: project + store.** Run `git init`. Keep the existing `.gitignore` lines (graft) and add `.venv/`, `__pycache__/`, `.pytest_cache/`, `*.db*`, `.DS_Store`, `.remember/`. Then the uv project, pinned Python, and `ReminderStore` with every status change. *Delivers* the correctness core (R1-R5 at the database level), proven without HTTP. *Tests:* create/get round trip; cancel pending/twice/after claim; claim after cancel returns nothing; claim respects fire_at and the limit; lease expiry becomes failed; a thread stress test racing cancel against claim on 200 rows, asserting exactly one wins per row.
2. **HTTP API.** Schemas plus POST/GET/DELETE. *Delivers* the full contract from the brief, without sending yet. *Tests:* every 422 case above; `Z` and `+00:00` accepted; past `fire_at` accepted; 201 with Location; returned times end in `Z`; every row of the DELETE table.
3. **Dispatcher.** The background loop, delivery, leases, lifespan start/stop. *Delivers* reminders that actually fire. *Tests* against a real local receiver (`tests/receiver.py`, a stdlib HTTP server on a random port that records requests and can be told to return 500, hang, or redirect): exact payload body and content type; past `fire_at` sent on the first tick; nothing sent early; cancelled never sent; 500 / timeout / 302 / connection refused all become `failed` with the reason; a burst larger than 20 all gets delivered.
4. **End-to-end check + README.** *Delivers* proof that it works the way a user would hit it, plus reviewer docs. `test_e2e.py` runs real `uvicorn` in a subprocess with a temp DB: the full create → fire → GET `sent` flow; cancel → never received; `kill -9` during a hanging send, then restart → the row becomes `failed` "outcome unknown" and the receiver is never called again.

## Verification

- `uv run pytest -q`: all green, including the e2e test.
- Manual: `uv run uvicorn app.main:app`, run `tests/receiver.py` on a port, then curl POST (future, past, and non-UTC `fire_at`), GET, and DELETE (pending, then sent) and compare against the tables above. The README lists these commands.

## Known limitations (documented in the README)

No auth. SSRF: callbacks can reach internal addresses. No POST idempotency key, so a client retry can create a duplicate. Single machine only (several processes on one machine are fine). Rows are kept forever. The server speaks plain HTTP (use a proxy or `uvicorn --ssl-*` for TLS). The 64 KB payload check happens after the body is parsed, so total request size isn't capped. Delivery is at most once: a crash mid-send ends as `failed` "outcome unknown". Timing precision is about 1s.

## Implementation rulings (made while building; these override the sections above)

1. **Work happens on `main` in this new repo, with no worktree.** The user chose one commit per slice in this repo, and an initial docs commit (CLAUDE.md, plan.md, ignore files) comes before slice 1 so every slice has a parent to diff against.
2. **Settings are `create_app` keyword arguments** (`poll_interval=1.0`, `send_timeout=10.0`, `max_in_flight=20`). Only `REMINDERS_DB` is read from the environment. This replaces the four env vars and the Settings class.
3. **The automated `kill -9` e2e test is dropped.** The lease logic is covered by fast store and dispatcher tests, plus one manual `kill -9` check in slice 4. The 30s lease margin would make an automated version slow.
4. **No `ReminderCreated` model.** POST returns `{"id": ...}` directly.
5. **`ReminderStore.cancel` returns the row after the attempt** (None if the id is unknown), with no outcome enum. A status of `cancelled` means success; any other status means it was too late.
6. **`fire_at` is parsed with `datetime.fromisoformat`** in a before-validator, which requires a string and a UTC offset of exactly 0. Pydantic's own datetime parsing also accepts Unix timestamps, including numeric strings.
7. **Microseconds and datetimes are converted with exact integer arithmetic** (`EPOCH + timedelta(microseconds=us)`), never float timestamps, so times round-trip exactly.
8. **`lease_until_us` is not cleared** after a send finishes. Nothing reads it once the status leaves `sending`.
9. **Commits from slice 3 on go to the branch `dispatcher-and-e2e`** (the user asked for this). Slices 1 and 2 are on `main`.
10. **The dispatcher reads only the callback's status code** (it streams the request and never reads the body), so a huge response can't fill memory.
11. **The payload is stored as UTF-8 JSON** (`ensure_ascii=False`), so the 64 KB limit counts UTF-8 bytes. Text with lone surrogates gets a 422, because it can't be written as UTF-8 and would break GET and DELETE later.
12. **The `fire_at` text must literally end in `Z` or `+00:00`** before it is parsed (the user's rule). `+0000` and `-00:00` get a 422.
