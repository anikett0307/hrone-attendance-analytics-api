"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError


load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
@app.on_event("startup")
def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index("joined_on")

    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    h, m = map(int, shift_start.split(":"))
    day = datetime.strptime(date_str, "%Y-%m-%d")
    start_dt = datetime(day.year, day.month, day.day, h, m, tzinfo=IST)

    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)

    seconds = (punch_in - start_dt).total_seconds()

    if seconds <= 600:
        return 0

    return int(seconds // 60)


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


def parse_date_or_422(value: str, name: str = "date") -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HTTPException(422, f"invalid {name}")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, f"invalid {name}")
    return value


def serialize_record(doc: dict) -> dict:
    doc.pop("_id", None)
    doc["punch_in"] = to_epoch_ms(doc.get("punch_in"))
    doc["punch_out"] = to_epoch_ms(doc.get("punch_out"))
    doc["work_hours"] = doc.get("work_hours")
    doc["late_minutes"] = doc.get("late_minutes", 0)
    doc["overtime_minutes"] = doc.get("overtime_minutes", 0)
    doc["half_day"] = doc.get("half_day", False)
    history = doc.get("history") or []
    for entry in history:
        entry["at"] = to_epoch_ms(entry.get("at"))
        for field in ("punch_in", "punch_out"):
            change = entry.get("changes", {}).get(field)
            if change:
                for key in ("from", "to"):
                    if change.get(key) is not None:
                        change[key] = to_epoch_ms(change[key])
    doc["history"] = history
    return doc


PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]


def round_half_up(value, places: int = 2) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def month_bounds(month: str):
    first = datetime.strptime(month, "%Y-%m")
    if first.month == 12:
        nxt = datetime(first.year + 1, 1, 1)
    else:
        nxt = datetime(first.year, first.month + 1, 1)
    last = nxt - timedelta(days=1)
    return first, last


def count_working_days(start: datetime, end: datetime) -> int:
    count = 0
    d = start
    while d <= end:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


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
    
    @field_validator("joined_on")
    @classmethod
    def _valid_joined_on(cls, v):
        datetime.strptime(v, "%Y-%m-%d")
        return v


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

    late_minutes = compute_late_minutes(ts, shift_start, attendance_date)

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

    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        ts = datetime.fromtimestamp(
            body.punched_at / 1000, tz=timezone.utc
        ).astimezone(IST).replace(microsecond=0)
    else:
        ts = datetime.now(IST).replace(microsecond=0)

    presence = {"$in": ["PRESENT", "WFH", "ON_DUTY"]}

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "status": presence,
            "punch_in": {"$ne": None, "$lte": ts},
        },
        sort=[("punch_in", -1)],
    )

    if not record:
        later = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "status": presence,
                "punch_in": {"$gt": ts},
            }
        )
        if later:
            raise HTTPException(422, "punched_at must be after punch_in")
        raise HTTPException(404, "no punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    punch_in_time = record["punch_in"]
    if punch_in_time.tzinfo is None:
        punch_in_time = punch_in_time.replace(tzinfo=timezone.utc)
    punch_in_time = punch_in_time.astimezone(IST)

    elapsed = (ts - punch_in_time).total_seconds()
    if elapsed <= 0 or elapsed > 86400:
        raise HTTPException(422, "invalid punch-out time")

    work_hours = compute_work_hours(punch_in_time, ts)
    overtime_minutes = compute_overtime(
        ts, emp["shift_start"], emp["shift_end"], record["date"]
    )
    half_day = work_hours < 4.50

    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )

    if result.matched_count == 0:
        raise HTTPException(409, "already punched out")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}

    if emp_code:
        q["emp_code"] = emp_code

    if date_from:
        parse_date_or_422(date_from, "date_from")
    if date_to:
        parse_date_or_422(date_to, "date_to")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be after date_to")

    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)

    docs = list(
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [serialize_record(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    parse_date_or_422(date)

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "attendance record not found")

    if body.status is None and body.punch_in is None and body.punch_out is None:
        raise HTTPException(422, "no changes supplied")

    if body.status in ("ABSENT", "LEAVE") and (
        body.punch_in is not None or body.punch_out is not None
    ):
        raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")

    def to_ist(value):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(IST).replace(microsecond=0)

    old_in = to_ist(record.get("punch_in"))
    old_out = to_ist(record.get("punch_out"))

    new_status = body.status if body.status is not None else record["status"]
    new_in = epoch_ms_to_datetime(body.punch_in) if body.punch_in is not None else old_in
    new_out = epoch_ms_to_datetime(body.punch_out) if body.punch_out is not None else old_out

    if new_status in ("ABSENT", "LEAVE"):
        new_in = None
        new_out = None
    else:
        if new_in is None:
            raise HTTPException(422, "presence status requires punch_in")

        expected_date = new_in.date()
        if emp["shift_end"] <= emp["shift_start"] and new_in.strftime("%H:%M") < emp["shift_end"]:
            expected_date -= timedelta(days=1)
        if expected_date.isoformat() != date:
            raise HTTPException(422, "punch_in must match the attendance date")

    if new_out is not None:
        if new_in is None:
            raise HTTPException(422, "punch_out requires punch_in")
        elapsed = (new_out - new_in).total_seconds()
        if elapsed <= 0 or elapsed > 86400:
            raise HTTPException(422, "invalid punch-out time")

    if new_in is not None:
        late_minutes = compute_late_minutes(new_in, emp["shift_start"], date)
    else:
        late_minutes = 0

    if new_in is not None and new_out is not None:
        work_hours = compute_work_hours(new_in, new_out)
        overtime_minutes = compute_overtime(
            new_out, emp["shift_start"], emp["shift_end"], date
        )
        half_day = work_hours < 4.50
    else:
        work_hours = None
        overtime_minutes = 0
        half_day = False

    old_values = {
        "status": record["status"],
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }

    new_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }

    changes = {
        f: {"from": old_values[f], "to": new_values[f]}
        for f in old_values
        if old_values[f] != new_values[f]
    }

    if not changes:
        raise HTTPException(422, "no changes supplied")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    if isinstance(record.get("history"), list):
        history_filter = {"$size": len(record["history"])}
    else:
        history_filter = {"$exists": False}

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": history_filter,
            "status": record["status"],
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_in,
                "punch_out": new_out,
                "work_hours": work_hours,
                "late_minutes": late_minutes,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(409, "attendance record changed concurrently")

    return serialize_record(updated)



# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
def employee_monthly_pipeline(emp_code: str, date_from: str, date_to: str):
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": date_from, "$lte": date_to},
            }
        },
        {
            "$addFields": {
                "_weekday": {
                    "$dayOfWeek": {
                        "$dateFromString": {
                            "dateString": "$date",
                            "format": "%Y-%m-%d",
                        }
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", PRESENCE_STATUSES]},
                                    {"$gte": ["$_weekday", 2]},
                                    {"$lte": ["$_weekday", 6]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    try:
        first, last = month_bounds(month)
    except ValueError:
        raise HTTPException(422, "invalid month")

    joined = datetime.strptime(emp["joined_on"], "%Y-%m-%d")
    start = max(first, joined)
    working_days = count_working_days(start, last) if start <= last else 0

    rows = list(
        db.attendance_logs.aggregate(
            employee_monthly_pipeline(
                emp_code,
                first.strftime("%Y-%m-%d"),
                last.strftime("%Y-%m-%d"),
            )
        )
    )
    r = rows[0] if rows else {}

    present_days = r.get("present_days", 0)

    if working_days > 0:
        attendance_pct = float(
            (Decimal(str(present_days)) * 100 / Decimal(working_days)).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
        )
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": r.get("leave_days", 0),
        "late_count": r.get("late_count", 0),
        "total_late_minutes": r.get("total_late_minutes", 0),
        "total_overtime_minutes": r.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }

def department_summary_pipeline(date_from: str, date_to: str, department: Optional[str] = None):
    match = {"joined_on": {"$lte": date_to}}
    if department:
        match["department"] = department

    present_filter = {
        "$filter": {
            "input": "$logs",
            "as": "l",
            "cond": {"$in": ["$$l.status", PRESENCE_STATUSES]},
        }
    }

    wh_filter = {
        "$filter": {
            "input": "$logs",
            "as": "l",
            "cond": {
                "$and": [
                    {"$in": ["$$l.status", PRESENCE_STATUSES]},
                    {"$ne": [{"$ifNull": ["$$l.work_hours", None]}, None]},
                ]
            },
        }
    }

    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
                    {
                        "$addFields": {
                            "_wd": {
                                "$dayOfWeek": {
                                    "$dateFromString": {
                                        "dateString": "$date",
                                        "format": "%Y-%m-%d",
                                    }
                                }
                            }
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", PRESENCE_STATUSES]},
                                            {"$gte": ["$$l._wd", 2]},
                                            {"$lte": ["$$l._wd", 6]},
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$eq": [{"$ifNull": ["$$l.half_day", False]}, True]},
                                            0.5,
                                            1,
                                        ]
                                    },
                                    0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
                "wh_sum": {
                    "$sum": {
                        "$map": {
                            "input": wh_filter,
                            "as": "l",
                            "in": "$$l.work_hours",
                        }
                    }
                },
                "wh_cnt": {"$size": wh_filter},
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "wh_sum": {"$sum": "$wh_sum"},
                "wh_cnt": {"$sum": "$wh_cnt"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)

    rows = list(
        db.employees.aggregate(
            department_summary_pipeline(
                first.strftime("%Y-%m-%d"),
                last.strftime("%Y-%m-%d"),
                department,
            )
        )
    )

    items = []
    for r in rows:
        if r["wh_cnt"] > 0:
            avg = float(
                (
                    Decimal(str(round(r["wh_sum"], 6))) / Decimal(r["wh_cnt"])
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            )
        else:
            avg = None

        items.append(
            {
                "department": r["_id"],
                "headcount": r["headcount"],
                "present_days": round_half_up(r["present_days"], 2),
                "avg_work_hours": avg,
                "late_count": r["late_count"],
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": r["leave_count"],
                "on_duty_count": r["on_duty_count"],
            }
        )

    return {"month": month, "items": items}


def late_leaderboard_pipeline(date_from: str, date_to: str, limit: int, department: Optional[str] = None):
    pipeline = [
        {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline += [
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]

    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)

    items = list(
        db.attendance_logs.aggregate(
            late_leaderboard_pipeline(
                first.strftime("%Y-%m-%d"),
                last.strftime("%Y-%m-%d"),
                limit,
                department,
            )
        )
    )

    return {"month": month, "items": items}

def department_trend_pipeline(codes: list, joined: list, date_from: str, date_to: str):
    start = datetime.strptime(date_from, "%Y-%m-%d")
    end_excl = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)

    return [
        {
            "$match": {
                "emp_code": {"$in": codes},
                "date": {"$gte": date_from, "$lte": date_to},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", PRESENCE_STATUSES]},
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$addFields": {
                "_d": {
                    "$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}
                }
            }
        },
        {
            "$densify": {
                "field": "_d",
                "range": {"step": 1, "unit": "day", "bounds": [start, end_excl]},
            }
        },
        {
            "$addFields": {
                "date": {"$dateToString": {"date": "$_d", "format": "%Y-%m-%d"}},
                "_wd": {"$dayOfWeek": "$_d"},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$and": [{"$gte": ["$_wd", 2]}, {"$lte": ["$_wd", 6]}]
                },
                "headcount": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": {"$literal": joined},
                                    "as": "e",
                                    "cond": {"$lte": ["$$e.j", "$date"]},
                                }
                            },
                            "as": "e",
                            "in": "$$e.n",
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {"$divide": ["$present_count", "$headcount"]},
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_d": 1},
                "output": {
                    "_avg": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$addFields": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$_avg", None]},
                        None,
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [{"$multiply": ["$_avg", 10000]}, 0.5]
                                    }
                                },
                                10000,
                            ]
                        },
                    ]
                }
            }
        },
        {"$sort": {"_d": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    parse_date_or_422(date_from, "from")
    parse_date_or_422(date_to, "to")

    d1 = datetime.strptime(date_from, "%Y-%m-%d")
    d2 = datetime.strptime(date_to, "%Y-%m-%d")

    if d2 < d1:
        raise HTTPException(422, "to cannot be before from")

    if (d2 - d1).days + 1 > 92:
        raise HTTPException(422, "range cannot exceed 92 days")

    groups = list(
        db.employees.aggregate(
            [
                {"$match": {"department": department}},
                {
                    "$group": {
                        "_id": "$joined_on",
                        "n": {"$sum": 1},
                        "codes": {"$push": "$emp_code"},
                    }
                },
            ]
        )
    )

    if not groups:
        raise HTTPException(404, "department not found")

    codes = [c for g in groups for c in g["codes"]]
    joined = [{"j": g["_id"], "n": g["n"]} for g in groups]

    items = list(
        db.attendance_logs.aggregate(
            department_trend_pipeline(codes, joined, date_from, date_to)
        )
    )

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to_: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    import json
    from bson import json_util

    def need(**kw):
        missing = [k for k, v in kw.items() if v is None]
        if missing:
            raise HTTPException(
                422, "missing required parameter(s): " + ", ".join(missing)
            )

    if endpoint == "attendance_list":
        if date_from:
            parse_date_or_422(date_from, "date_from")
        if date_to:
            parse_date_or_422(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from cannot be after date_to")

        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status

        collection = "attendance_logs"
        inner = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }

    elif endpoint == "employee_monthly":
        need(emp_code=emp_code, month=month)
        first, last = month_bounds(month)
        collection = "attendance_logs"
        inner = {
            "aggregate": "attendance_logs",
            "pipeline": employee_monthly_pipeline(
                emp_code,
                first.strftime("%Y-%m-%d"),
                last.strftime("%Y-%m-%d"),
            ),
            "cursor": {},
        }

    elif endpoint == "department_summary":
        need(month=month)
        first, last = month_bounds(month)
        collection = "employees"
        inner = {
            "aggregate": "employees",
            "pipeline": department_summary_pipeline(
                first.strftime("%Y-%m-%d"),
                last.strftime("%Y-%m-%d"),
                department,
            ),
            "cursor": {},
        }

    elif endpoint == "late_leaderboard":
        need(month=month)
        first, last = month_bounds(month)
        collection = "attendance_logs"
        inner = {
            "aggregate": "attendance_logs",
            "pipeline": late_leaderboard_pipeline(
                first.strftime("%Y-%m-%d"),
                last.strftime("%Y-%m-%d"),
                limit,
                department,
            ),
            "cursor": {},
        }

    else:
        need(**{"department": department, "from": from_, "to": to_})
        parse_date_or_422(from_, "from")
        parse_date_or_422(to_, "to")

        d1 = datetime.strptime(from_, "%Y-%m-%d")
        d2 = datetime.strptime(to_, "%Y-%m-%d")
        if d2 < d1:
            raise HTTPException(422, "to cannot be before from")
        if (d2 - d1).days + 1 > 92:
            raise HTTPException(422, "range cannot exceed 92 days")

        groups = list(
            db.employees.aggregate(
                [
                    {"$match": {"department": department}},
                    {
                        "$group": {
                            "_id": "$joined_on",
                            "n": {"$sum": 1},
                            "codes": {"$push": "$emp_code"},
                        }
                    },
                ]
            )
        )
        if not groups:
            raise HTTPException(404, "department not found")

        codes = [c for g in groups for c in g["codes"]]
        joined = [{"j": g["_id"], "n": g["n"]} for g in groups]

        collection = "attendance_logs"
        inner = {
            "aggregate": "attendance_logs",
            "pipeline": department_trend_pipeline(codes, joined, from_, to_),
            "cursor": {},
        }

    result = db.command({"explain": inner, "verbosity": "executionStats"})

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(result)),
    }


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
