"""The reminders web app: creates the FastAPI app and defines POST, GET and DELETE /reminders."""

import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.schemas import Reminder, ReminderCreate, datetime_to_us, dump_payload
from app.store import ReminderStore, now_us

NOT_FOUND = "Reminder not found"


# Builds the app on top of one database file. Routes are defined inside so they
# share this app's store without any module-level state.
def create_app(db_path: str) -> FastAPI:
    store = ReminderStore(db_path)

    # Creates the database table when the server starts (not when this module is imported).
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        store.init_schema()
        yield

    app = FastAPI(lifespan=lifespan)

    # Returns FastAPI's usual 422 error list, minus each error's copy of the bad
    # input. The default list repeats the input, and a NaN or Infinity in it
    # cannot be written as JSON (that turned the 422 into a 500). Dropping it
    # also stops a 64 KB+ payload from being echoed back.
    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{k: v for k, v in e.items() if k != "input"} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})

    # Saves a new reminder and returns its id, plus a Location header pointing at it.
    @app.post("/reminders", status_code=201)
    def create_reminder(body: ReminderCreate, response: Response) -> dict:
        reminder_id = store.create(
            datetime_to_us(body.fire_at), str(body.callback_url), dump_payload(body.payload), now_us()
        )
        response.headers["Location"] = f"/reminders/{reminder_id}"
        return {"id": reminder_id}

    # Returns one reminder and its status, or 404 if the id is unknown.
    @app.get("/reminders/{reminder_id}", response_model=Reminder)
    def get_reminder(reminder_id: uuid.UUID) -> Reminder:
        row = store.get(str(reminder_id))
        if row is None:
            raise HTTPException(status_code=404, detail=NOT_FOUND)
        return Reminder.from_row(row)

    # Cancels a reminder. Succeeds (200) if it is now or was already cancelled;
    # returns 409 if sending has already started; 404 if the id is unknown.
    @app.delete("/reminders/{reminder_id}", response_model=Reminder)
    def cancel_reminder(reminder_id: uuid.UUID):
        row = store.cancel(str(reminder_id), now_us())
        if row is None:
            raise HTTPException(status_code=404, detail=NOT_FOUND)
        if row["status"] != "cancelled":
            return JSONResponse(
                status_code=409,
                content={
                    "detail": f"Reminder can no longer be cancelled; current status is '{row['status']}'.",
                    "status": row["status"],
                },
            )
        return Reminder.from_row(row)

    return app


# What uvicorn loads. The database file comes from REMINDERS_DB.
app = create_app(os.environ.get("REMINDERS_DB", "reminders.db"))
