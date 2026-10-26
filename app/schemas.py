"""Request and response models for the reminders API, plus the time and payload helpers they share.

All times are UTC. Inside the app they are integer microseconds since the Unix
epoch; the API shows them as ISO 8601 text ending in "Z".
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, HttpUrl, JsonValue, field_validator

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MAX_PAYLOAD_BYTES = 64 * 1024
FIRE_AT_ERROR = "fire_at must be an ISO 8601 UTC time ending in Z or +00:00"


# The payload as compact JSON text. Used for both the size check and what gets
# stored, so the two always agree.
def dump_payload(value: JsonValue) -> str:
    return json.dumps(value, separators=(",", ":"))


# A UTC datetime as integer microseconds since the epoch (exact, no floats).
def datetime_to_us(dt: datetime) -> int:
    return (dt - EPOCH) // timedelta(microseconds=1)


# Integer microseconds since the epoch as a UTC datetime (exact, no floats).
def us_to_datetime(us: int) -> datetime:
    return EPOCH + timedelta(microseconds=us)


class ReminderCreate(BaseModel):
    """The body of POST /reminders."""

    model_config = ConfigDict(extra="forbid")

    fire_at: datetime
    callback_url: HttpUrl
    payload: JsonValue

    # Accepts only a text timestamp in UTC. Pydantic's own parsing would also
    # take Unix timestamps (numbers, or numeric text like "1700000000"), so we
    # parse it ourselves first.
    @field_validator("fire_at", mode="before")
    @classmethod
    def fire_at_must_be_utc_text(cls, value):
        if not isinstance(value, str):
            raise ValueError(FIRE_AT_ERROR)
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(FIRE_AT_ERROR) from None
        if parsed.utcoffset() != timedelta(0):  # also true when there is no timezone at all
            raise ValueError(FIRE_AT_ERROR)
        return parsed

    # Refuses payloads whose stored JSON text is over the size limit.
    @field_validator("payload")
    @classmethod
    def payload_must_fit(cls, value):
        if len(dump_payload(value).encode()) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload must be at most {MAX_PAYLOAD_BYTES} bytes of JSON")
        return value


class Reminder(BaseModel):
    """A reminder as returned by GET and DELETE."""

    id: str
    fire_at: datetime
    callback_url: str
    payload: JsonValue
    status: Literal["pending", "sending", "sent", "failed", "cancelled"]
    created_at: datetime
    cancelled_at: datetime | None
    attempted_at: datetime | None
    response_status: int | None
    error: str | None

    # Builds a Reminder from a store row: microsecond integers become UTC
    # datetimes and the payload text becomes JSON again.
    @classmethod
    def from_row(cls, row: dict) -> Self:
        # A time column that may be empty (NULL) as a datetime or None.
        def optional_time(us: int | None) -> datetime | None:
            return None if us is None else us_to_datetime(us)

        return cls(
            id=row["id"],
            fire_at=us_to_datetime(row["fire_at_us"]),
            callback_url=row["callback_url"],
            payload=json.loads(row["payload"]),
            status=row["status"],
            created_at=us_to_datetime(row["created_at_us"]),
            cancelled_at=optional_time(row["cancelled_at_us"]),
            attempted_at=optional_time(row["attempted_at_us"]),
            response_status=row["response_status"],
            error=row["error"],
        )
