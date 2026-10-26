"""Background sender: finds due reminders and POSTs each one's payload to its callback URL.

Every poll_interval seconds it (1) marks sends whose lease ran out as failed,
then (2) claims as many due reminders as it has free send slots and starts
sending them. A reminder is only sent after the store has moved it from pending
to sending, so a cancelled reminder can never be sent. Each reminder gets
exactly one attempt; nothing is ever retried.
"""

import asyncio
import logging
from contextlib import suppress

import httpx

from app.store import ReminderStore, now_us

# Extra time on top of send_timeout before a claim's lease runs out. It must stay
# larger than SQLite's 5s busy_timeout, so a slow mark_sent waiting on the lock
# can't lose to another process's lease cleanup.
LEASE_MARGIN_S = 30

log = logging.getLogger(__name__)


class Dispatcher:
    """Sends due reminders in the background. Call start() once, and stop() on shutdown."""

    # Keeps the settings and opens the shared HTTP client. Nothing runs until start().
    def __init__(self, store: ReminderStore, *, poll_interval: float, send_timeout: float, max_in_flight: int):
        self.store = store
        self.poll_interval = poll_interval
        self.send_timeout = send_timeout
        self.max_in_flight = max_in_flight
        self._lease_us = int((send_timeout + LEASE_MARGIN_S) * 1_000_000)
        # httpx's own timeouts are per phase (connect, read, ...), so they are
        # turned off; asyncio.timeout(send_timeout) in _send is the one total deadline.
        self._client = httpx.AsyncClient(follow_redirects=False, timeout=None)
        self._sends: set[asyncio.Task] = set()
        self._stopping = asyncio.Event()
        self._loop_task: asyncio.Task | None = None

    # Starts the background loop as an asyncio task.
    def start(self) -> None:
        self._loop_task = asyncio.create_task(self._run())

    # Waits poll_interval, runs one check, and repeats until stop() is called.
    # The wait comes first, so the first check happens poll_interval after start().
    async def _run(self) -> None:
        while True:
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), self.poll_interval)
            if self._stopping.is_set():
                return
            await self.tick()

    # One check: fails expired leases, then claims only as many due reminders as
    # can be sent right away and starts a send task for each. Any error is logged
    # and the loop carries on. Store calls run in a thread so sqlite3 never
    # blocks the event loop.
    async def tick(self) -> None:
        try:
            await asyncio.to_thread(self.store.fail_expired_leases, now_us())
            free = self.max_in_flight - len(self._sends)
            if free <= 0:
                return
            rows = await asyncio.to_thread(self.store.claim_due, now_us(), free, self._lease_us)
            for row in rows:
                task = asyncio.create_task(self._send(row))
                self._sends.add(task)
                task.add_done_callback(self._sends.discard)
        except Exception:
            log.exception("dispatcher check failed")

    # Sends one claimed reminder once and records the outcome. The body is the
    # stored JSON text exactly as saved, never re-serialized.
    async def _send(self, row: dict) -> None:
        reminder_id = row["id"]
        try:
            try:
                async with asyncio.timeout(self.send_timeout):
                    response = await self._client.post(
                        row["callback_url"],
                        content=row["payload"].encode(),
                        headers={"Content-Type": "application/json"},
                    )
            except TimeoutError:
                recorded = await self._mark_failed(reminder_id, None, f"timed out after {self.send_timeout}s")
            except httpx.HTTPError as exc:
                recorded = await self._mark_failed(reminder_id, None, f"{type(exc).__name__}: {exc}")
            else:
                status = response.status_code
                if 200 <= status < 300:
                    recorded = await asyncio.to_thread(self.store.mark_sent, reminder_id, status)
                else:  # includes 3xx: redirects are not followed
                    recorded = await self._mark_failed(reminder_id, status, f"HTTP {status}")
            if not recorded:
                log.warning("reminder %s was no longer 'sending' (lease expired?); outcome not saved", reminder_id)
        except Exception:
            # Left in 'sending'; its lease expiry will mark it failed. Never resent.
            log.exception("sending reminder %s failed unexpectedly", reminder_id)

    # Records a failed attempt in the store (in a thread). Returns False if the
    # reminder was no longer 'sending'.
    async def _mark_failed(self, reminder_id: str, response_status: int | None, error: str) -> bool:
        return await asyncio.to_thread(self.store.mark_failed, reminder_id, response_status, error)

    # Stops claiming, lets the loop finish its current check, waits for every
    # send already started (each ends within send_timeout), then closes the HTTP client.
    async def stop(self) -> None:
        self._stopping.set()
        await self._loop_task
        await asyncio.gather(*self._sends)
        await self._client.aclose()
