"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional,Literal
from annotated_types import doc
from pymongo.errors import DuplicateKeyError,PyMongoError
from decimal import Decimal, ROUND_HALF_UP

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException,Query
from pydantic import BaseModel, Field, StrictInt, EmailStr
from pymongo import MongoClient


load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
@app.on_event("startup")
def create_indexes():
    db.employees.create_index("emp_code", unique=True)

    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))

    shift_start_time = punch_in.replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0
    )

    if shift_start_time - punch_in > timedelta(hours=12):
        shift_start_time -= timedelta(days=1)
    elif punch_in - shift_start_time > timedelta(hours=12):
        shift_start_time += timedelta(days=1)

    elapsed_seconds = (punch_in - shift_start_time).total_seconds()

    if elapsed_seconds <= 600:
        return 0

    return int(elapsed_seconds // 60)


def epoch_ms_to_datetime(value: int) -> datetime:
    return datetime.fromtimestamp(
        value / 1000,
        tz=timezone.utc
    ).astimezone(IST).replace(microsecond=0)

def to_epoch_ms(value):
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return int(value)

    if isinstance(value, str):
        value = datetime.fromisoformat(value)

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return int(value.timestamp() * 1000)

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(
        str((punch_out - punch_in).total_seconds())
    )
    hours = seconds / Decimal("3600")

    return float(
        hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    date_str: str
) -> int:
    from datetime import datetime, timedelta

    attendance_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    start_time = datetime.strptime(shift_start, "%H:%M").time()
    end_time = datetime.strptime(shift_end, "%H:%M").time()

    shift_end_dt = datetime.combine(attendance_date, end_time, tzinfo=IST)

    if end_time <= start_time:
        shift_end_dt += timedelta(days=1)

    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=timezone.utc)

    punch_out = punch_out.astimezone(IST)

    elapsed_seconds = (punch_out - shift_end_dt).total_seconds()

    if elapsed_seconds < 1800:
        return 0

    return int(elapsed_seconds // 60)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    joined_on: str = Field(
        pattern=r"^\d{4}-\d{2}-\d{2}$"
    )


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
    default=None,
    ge=100000000000,
    le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000
    )

class RegularizeIn(BaseModel):
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[
        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    ] = None
    punch_in: Optional[StrictInt] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000
    )
    punch_out: Optional[StrictInt] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000
    )

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="MongoDB unavailable"
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start and shift_end must differ")

    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")

    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = int(doc["created_at"].timestamp() * 1000)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
    db.employees.find(q, {"_id": 0})
    .sort("emp_code", 1)
    .skip(skip)
    .limit(page_size)
    )
    for doc in items:
        value = doc.get("created_at")

        if value is not None:
            if isinstance(value, str):
                value = datetime.fromisoformat(value)

            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)

            doc["created_at"] = int(value.timestamp() * 1000)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(404, "employee not found")

    if body.status not in ["PRESENT", "WFH", "ON_DUTY"]:
        raise HTTPException(422, "invalid attendance status for punch-in")

    if body.punched_at is not None:
        ts = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc
        ).astimezone(IST).replace(microsecond=0)
    else:
        ts = datetime.now(IST).replace(microsecond=0)

    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    punch_time = ts.time()
    shift_end_time = datetime.strptime(shift_end, "%H:%M").time()

    attendance_date = ts.date()

    if shift_end <= shift_start and punch_time < shift_end_time:
        attendance_date -= timedelta(days=1)

    attendance_date = attendance_date.isoformat()

    if db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "date": attendance_date
    }):
        raise HTTPException(409, "already punched in for this date")

    late_minutes = compute_late_minutes(ts, shift_start)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    doc.pop("_id", None)

    for field in ["punch_in", "punch_out"]:
        value = doc.get(field)

        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)

            doc[field] = int(value.timestamp() * 1000)

    return doc


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        ts = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc
        ).astimezone(IST).replace(microsecond=0)
    else:
        ts = datetime.now(IST).replace(microsecond=0)

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts},
            "punch_out": None,
            "status": {"$in": ["PRESENT", "WFH", "ON_DUTY"]}
        },
        sort=[("punch_in", -1)]
    )

    if not record:
        any_record = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "status": {"$in": ["PRESENT", "WFH", "ON_DUTY"]}
            },
            sort=[("date", -1)]
        )

        if any_record:
            punch_in = any_record.get("punch_in")
            punch_out = any_record.get("punch_out")

            if punch_in is not None:
                if punch_in.tzinfo is None:
                    punch_in = punch_in.replace(tzinfo=timezone.utc)

                punch_in = punch_in.astimezone(IST)

                if ts < punch_in:
                    raise HTTPException(422, "invalid punch-out time")

                if punch_out is not None:
                    if punch_in.date() == ts.date():
                        raise HTTPException(409, "already punched out")

                else:
                    elapsed = (ts - punch_in).total_seconds()

                    if elapsed > 86400:
                        raise HTTPException(422, "invalid punch-out time")

        raise HTTPException(404, "no punch-in found")

    punch_in_time = record["punch_in"]

    if punch_in_time.tzinfo is None:
        punch_in_time = punch_in_time.replace(tzinfo=timezone.utc)

    punch_in_time = punch_in_time.astimezone(IST)

    elapsed = (ts - punch_in_time).total_seconds()

    if elapsed <= 0 or elapsed > 86400:
        raise HTTPException(422, "invalid punch-out time")

    work_hours = compute_work_hours(punch_in_time, ts)
    
    overtime_minutes = compute_overtime(
        ts,
        emp["shift_start"],
        emp["shift_end"],
        record["date"]
    )

    if overtime_minutes < 30:
        overtime_minutes = 0

    half_day = work_hours < 4.50

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day
            }
        }
    )

    if result.modified_count == 0:
        raise HTTPException(409, "already punched out")

    updated = db.attendance_logs.find_one(
        {"_id": record["_id"]},
        {"_id": 0}
    )

    for field in ["punch_in", "punch_out"]:
        value = updated.get(field)

        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)

            updated[field] = int(value.timestamp() * 1000)

    return updated


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}

    if emp_code:
        q["emp_code"] = emp_code

    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be after date_to")

    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)

    page_docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    for doc in page_docs:
        doc.pop("_id", None)

        for field in ["punch_in", "punch_out"]:
            doc[field] = to_epoch_ms(doc.get(field))

        for entry in doc.get("history", []):
            entry["at"] = to_epoch_ms(entry.get("at"))

            for field in ["punch_in", "punch_out"]:
                change = entry.get("changes", {}).get(field, {})

                for key in ["from", "to"]:
                    value = change.get(key)

                    if value is not None:
                        change[key] = to_epoch_ms(value)

    return {
        "items": page_docs,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn
):
    from pymongo import ReturnDocument

    emp = db.employees.find_one({"emp_code": emp_code})

    if not emp:
        raise HTTPException(404, "employee not found")

    try:
        attendance_date = datetime.strptime(
            date, "%Y-%m-%d"
        ).date()
    except ValueError:
        raise HTTPException(422, "invalid attendance date")

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date
    })

    if not record:
        raise HTTPException(404, "attendance record not found")

    supplied = body.model_fields_set

    if body.status in ["ABSENT", "LEAVE"]:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(
                422,
                "ABSENT and LEAVE cannot have punch times"
            )

    def to_ist(value):
        if value is None:
            return None

        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)

        return value.astimezone(IST).replace(microsecond=0)

    old_in = to_ist(record.get("punch_in"))
    old_out = to_ist(record.get("punch_out"))

    new_status = (
        body.status if "status" in supplied
        else record["status"]
    )

    new_in = (
        epoch_ms_to_datetime(body.punch_in)
        if "punch_in" in supplied
        else old_in
    )

    new_out = (
        epoch_ms_to_datetime(body.punch_out)
        if "punch_out" in supplied
        else old_out
    )

    if new_status in ["ABSENT", "LEAVE"]:
        new_in = None
        new_out = None

    if new_status in ["PRESENT", "WFH", "ON_DUTY"]:
        if new_in is None:
            raise HTTPException(
                422,
                "presence status requires punch_in"
            )

    if new_in is not None:
        shift_end = emp["shift_end"]
        shift_start = emp["shift_start"]
        expected_date = new_in.date()

        if shift_end <= shift_start:
            if new_in.strftime("%H:%M") < shift_end:
                expected_date -= timedelta(days=1)

        if expected_date != attendance_date:
            raise HTTPException(
                422,
                "punch_in must match the attendance date"
            )

    if new_out is not None:
        if new_in is None:
            raise HTTPException(
                422,
                "punch_out requires punch_in"
            )

        elapsed = (new_out - new_in).total_seconds()

        if elapsed <= 0 or elapsed > 86400:
            raise HTTPException(
                422,
                "invalid punch-out time"
            )

    if new_in is not None:
        h, m = map(int, emp["shift_start"].split(":"))

        shift_start_time = datetime.combine(
            attendance_date,
            datetime.min.time()
        ).replace(hour=h, minute=m, tzinfo=IST)

        late_seconds = (
            new_in - shift_start_time
        ).total_seconds()

        late_minutes = (
            int(late_seconds // 60)
            if late_seconds > 600
            else 0
        )
    else:
        late_minutes = 0

    if new_in is not None and new_out is not None:
        work_hours = compute_work_hours(new_in, new_out)

        overtime_minutes = compute_overtime(
            new_out,
            emp["shift_start"],
            emp["shift_end"],
            date
        )

        half_day = work_hours < 4.50
    else:
        work_hours = None
        overtime_minutes = 0
        half_day = False

    def timestamp_ms(value):
        if value is None:
            return None

        return int(value.timestamp() * 1000)

    old_values = {
        "status": record["status"],
        "punch_in": timestamp_ms(old_in),
        "punch_out": timestamp_ms(old_out),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False)
    }

    new_values = {
        "status": new_status,
        "punch_in": timestamp_ms(new_in),
        "punch_out": timestamp_ms(new_out),
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day
    }

    changes = {
        field: {
            "from": old_values[field],
            "to": new_values[field]
        }
        for field in old_values
        if old_values[field] != new_values[field]
    }

    if not changes:
        raise HTTPException(422, "no changes supplied")

    history = record.get("history", [])

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    update_filter = {
        "_id": record["_id"],
        "history": history,
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out")
    }

    updated = db.attendance_logs.find_one_and_update(
        update_filter,
        {
            "$set": {
                "status": new_status,
                "punch_in": new_in,
                "punch_out": new_out,
                "work_hours": work_hours,
                "late_minutes": late_minutes,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day
            },
            "$push": {"history": history_entry}
        },
        return_document=ReturnDocument.AFTER
    )

    if updated is None:
        raise HTTPException(409, "attendance record changed concurrently")

    updated.pop("_id", None)

    for field in ["punch_in", "punch_out"]:
        value = updated.get(field)

        if value is not None:
            updated[field] = timestamp_ms(to_ist(value))

    for entry in updated.get("history", []):
        entry["at"] = timestamp_ms(to_ist(entry["at"]))

    return updated

# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
