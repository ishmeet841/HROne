"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
from __future__ import annotations

import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Annotated, Any

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

load_dotenv(override=False)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"
EMP_CODE_PATTERN = r"^EMP\d{4,6}$"
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")
TRACKED_CORRECTION_FIELDS = (
    "status",
    "punch_in",
    "punch_out",
    "work_hours",
    "late_minutes",
    "overtime_minutes",
    "half_day",
)

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=5000,
    tz_aware=True,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    try:
        ensure_indexes()
        fastapi_app.state.index_error = None
    except PyMongoError as exc:
        fastapi_app.state.index_error = str(exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


EpochMillis = Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    emp_code: str = Field(pattern=EMP_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=EMAIL_PATTERN)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=TIME_PATTERN)
    shift_end: str = Field(default="18:30", pattern=TIME_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_date(cls, value: str) -> str:
        validate_date_value(value, "joined_on")
        return value

    @model_validator(mode="after")
    def shift_bounds_differ(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    emp_code: str
    punched_at: EpochMillis | None = None
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    emp_code: str
    punched_at: EpochMillis | None = None


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    status: Status | None = None
    punch_in: EpochMillis | None = None
    punch_out: EpochMillis | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def punch_times_cannot_be_null_when_present(self) -> "RegularizeRequest":
        if "punch_in" in self.model_fields_set and self.punch_in is None:
            raise ValueError("punch_in cannot be null")
        if "punch_out" in self.model_fields_set and self.punch_out is None:
            raise ValueError("punch_out cannot be null")
        return self


def validation_error(message: str) -> None:
    raise HTTPException(status_code=422, detail=message)


def database_error(exc: PyMongoError) -> None:
    raise HTTPException(status_code=503, detail=f"database unavailable: {exc.__class__.__name__}") from exc


def validate_date_value(value: str, field_name: str = "date") -> Date:
    if not re.fullmatch(DATE_PATTERN, value):
        raise ValueError(f"{field_name} must be YYYY-MM-DD")
    try:
        return Date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid date") from exc


def parse_date_param(value: str, field_name: str = "date") -> Date:
    try:
        return validate_date_value(value, field_name)
    except ValueError as exc:
        validation_error(str(exc))
        raise AssertionError("unreachable")


def parse_month(month: str) -> tuple[Date, Date, str, str]:
    if not re.fullmatch(MONTH_PATTERN, month):
        validation_error("month must be YYYY-MM")
    year, month_number = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_number)[1]
    start = Date(year, month_number, 1)
    end = Date(year, month_number, last_day)
    return start, end, start.isoformat(), end.isoformat()


def parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def time_to_seconds(value: str) -> int:
    hour, minute = parse_hhmm(value)
    return hour * 3600 + minute * 60


def is_overnight_shift(employee: dict[str, Any]) -> bool:
    return time_to_seconds(employee["shift_end"]) <= time_to_seconds(employee["shift_start"])


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def truncate_to_second(dt: datetime) -> datetime:
    return ensure_utc(dt).replace(microsecond=0)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def epoch_ms_to_utc(ms: int) -> datetime:
    seconds, _milliseconds = divmod(ms, 1000)
    return datetime.fromtimestamp(seconds, tz=UTC)


def utc_to_epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    return int(truncate_to_second(value).timestamp()) * 1000


def attendance_date_for_punch(punch_in: datetime, employee: dict[str, Any]) -> str:
    local = truncate_to_second(punch_in).astimezone(IST)
    attendance_day = local.date()
    if is_overnight_shift(employee) and (
        local.hour * 3600 + local.minute * 60 + local.second
    ) < time_to_seconds(employee["shift_end"]):
        attendance_day -= timedelta(days=1)
    return attendance_day.isoformat()


def shift_instant_utc(date_str: str, shift_time: str, next_day: bool = False) -> datetime:
    day = Date.fromisoformat(date_str)
    if next_day:
        day += timedelta(days=1)
    hour, minute = parse_hhmm(shift_time)
    local = datetime.combine(day, time(hour, minute), IST)
    return local.astimezone(UTC)


def shift_start_utc(date_str: str, employee: dict[str, Any]) -> datetime:
    return shift_instant_utc(date_str, employee["shift_start"])


def shift_end_utc(date_str: str, employee: dict[str, Any]) -> datetime:
    return shift_instant_utc(date_str, employee["shift_end"], is_overnight_shift(employee))


def round_half_up(value: Decimal | float | int, places: int) -> float:
    quant = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in: datetime, employee: dict[str, Any], date_str: str) -> int:
    punch_in = truncate_to_second(punch_in)
    elapsed_seconds = (punch_in - shift_start_utc(date_str, employee)).total_seconds()
    if elapsed_seconds <= 10 * 60:
        return 0
    return max(0, int(elapsed_seconds // 60))


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((truncate_to_second(punch_out) - truncate_to_second(punch_in)).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return round_half_up(hours, 2)


def compute_overtime_minutes(punch_out: datetime, employee: dict[str, Any], date_str: str) -> int:
    elapsed_seconds = (truncate_to_second(punch_out) - shift_end_utc(date_str, employee)).total_seconds()
    minutes = max(0, int(elapsed_seconds // 60))
    return minutes if minutes >= 30 else 0


def derived_attendance_values(
    employee: dict[str, Any],
    date_str: str,
    status: str,
    punch_in: datetime | None,
    punch_out: datetime | None,
) -> dict[str, Any]:
    if status in ("ABSENT", "LEAVE"):
        return {
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }

    if punch_in is None:
        validation_error("presence status requires punch_in")
    punch_in = truncate_to_second(punch_in)
    if attendance_date_for_punch(punch_in, employee) != date_str:
        validation_error("punch_in must belong to the record attendance date")

    punch_out = truncate_to_second(punch_out) if punch_out is not None else None
    if punch_out is not None:
        validate_punch_out_time(punch_in, punch_out)
        work_hours = compute_work_hours(punch_in, punch_out)
        overtime_minutes = compute_overtime_minutes(punch_out, employee, date_str)
        half_day = work_hours < 4.50
    else:
        work_hours = None
        overtime_minutes = 0
        half_day = False

    return {
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": work_hours,
        "late_minutes": compute_late_minutes(punch_in, employee, date_str),
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }


def validate_punch_out_time(punch_in: datetime, punch_out: datetime) -> None:
    delta = truncate_to_second(punch_out) - truncate_to_second(punch_in)
    if delta.total_seconds() <= 0:
        validation_error("punched_at must be after punch_in")
    if delta > timedelta(hours=24):
        validation_error("punched_at must be within 24 hours of punch_in")


def working_days_in_month(joined_on: str, month_start: Date, month_end: Date) -> int:
    joined = Date.fromisoformat(joined_on)
    current = max(joined, month_start)
    if current > month_end:
        return 0
    count = 0
    while current <= month_end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def serialize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return utc_to_epoch_ms(value)
    if isinstance(value, list):
        return [serialize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: serialize_value(item) for key, item in value.items()}
    return value


def serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": utc_to_epoch_ms(doc["created_at"]),
    }


def serialize_history_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "at": utc_to_epoch_ms(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": serialize_value(entry.get("changes", {})),
    }


def serialize_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": utc_to_epoch_ms(doc.get("punch_in")),
        "punch_out": utc_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history_entry(entry) for entry in doc.get("history", [])],
    }


def bson_to_jsonable(value: Any) -> Any:
    return json_util.loads(json_util.dumps(value))


def get_employee_or_404(emp_code: str) -> dict[str, Any]:
    try:
        employee = db.employees.find_one({"emp_code": emp_code})
    except PyMongoError as exc:
        database_error(exc)
        raise AssertionError("unreachable")
    if employee is None:
        raise HTTPException(status_code=404, detail="unknown employee")
    return employee


def ensure_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_employee_emp_code")
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="idx_employee_department_emp_code",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)],
        name="idx_employee_department_joined_emp_code",
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING), ("emp_code", ASCENDING)],
        name="idx_employee_joined_department_emp_code",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uniq_attendance_emp_date",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="idx_attendance_emp_punch_in",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_attendance_date_desc_emp",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_attendance_status_date_desc_emp",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_attendance_date_late_emp",
    )


def maybe_hint_cursor(cursor: Any, hint_name: str | None) -> Any:
    if not hint_name:
        return cursor
    try:
        return cursor.hint(hint_name)
    except (AttributeError, OperationFailure, TypeError):
        return cursor


def count_documents(collection: Any, query: dict[str, Any], hint_name: str | None = None) -> int:
    if hint_name:
        try:
            return collection.count_documents(query, hint=hint_name)
        except (OperationFailure, TypeError):
            pass
    return collection.count_documents(query)


def attendance_list_query(
    emp_code: str | None,
    date_from: str | None,
    date_to: str | None,
    status: Status | None,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status.value
    return query


def attendance_list_hint(emp_code: str | None, status: Status | None) -> str:
    if status is not None:
        return "idx_attendance_status_date_desc_emp"
    if emp_code is not None:
        return "uniq_attendance_emp_date"
    return "idx_attendance_date_desc_emp"


def employee_monthly_pipeline(emp_code: str, month_start: str, month_end: str) -> list[dict[str, Any]]:
    weekday_expr = {
        "$lte": [
            {
                "$isoDayOfWeek": {
                    "date": {
                        "$dateFromString": {
                            "dateString": "$date",
                            "timezone": "Asia/Kolkata",
                        }
                    },
                    "timezone": "Asia/Kolkata",
                }
            },
            5,
        ]
    }
    present_day_value = {
        "$cond": [
            {"$ifNull": ["$half_day", False]},
            0.5,
            1,
        ]
    }
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, weekday_expr]},
                            present_day_value,
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
        {"$project": {"_id": 0}},
    ]


def department_summary_pipeline(
    month_start: str,
    month_end: str,
    department: str | None,
) -> list[dict[str, Any]]:
    match: dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        match["department"] = department
    log_weekday_expr = {
        "$cond": [
            {"$ne": ["$logs.date", None]},
            {
                "$lte": [
                    {
                        "$isoDayOfWeek": {
                            "date": {
                                "$dateFromString": {
                                    "dateString": "$logs.date",
                                    "timezone": "Asia/Kolkata",
                                }
                            },
                            "timezone": "Asia/Kolkata",
                        }
                    },
                    5,
                ]
            },
            False,
        ]
    }
    present_status_expr = {"$in": ["$logs.status", list(PRESENT_STATUSES)]}
    present_day_value = {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}
    has_work_hours = {
        "$and": [
            present_status_expr,
            {"$ne": ["$logs.work_hours", None]},
        ]
    }
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [{"$match": {"date": {"$gte": month_start, "$lte": month_end}}}],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [present_status_expr, log_weekday_expr]},
                            present_day_value,
                            0,
                        ]
                    }
                },
                "work_hours_total": {"$sum": {"$cond": [has_work_hours, "$logs.work_hours", 0]}},
                "work_hours_count": {"$sum": {"$cond": [has_work_hours, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": 1,
                "avg_work_hours_raw": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_total", "$work_hours_count"]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(
    month_start: str,
    month_end: str,
    limit: int,
    department: str | None,
) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
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
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


def department_trend_pipeline(
    department: str,
    from_date: str,
    day_count: int,
) -> list[dict[str, Any]]:
    present_value = {
        "$cond": [
            {"$in": ["$log.status", list(PRESENT_STATUSES)]},
            {"$cond": [{"$ifNull": ["$log.half_day", False]}, 0.5, 1]},
            0,
        ]
    }
    return [
        {"$match": {"department": department}},
        {
            "$project": {
                "emp_code": 1,
                "joined_on": 1,
                "date_obj": {
                    "$map": {
                        "input": {"$range": [0, day_count]},
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": {
                                    "$dateFromString": {
                                        "dateString": from_date,
                                        "timezone": "Asia/Kolkata",
                                    }
                                },
                                "unit": "day",
                                "amount": "$$offset",
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$date_obj"},
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date_obj",
                        "timezone": "Asia/Kolkata",
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code", "day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$eq": ["$date", "$$day"]},
                                ]
                            }
                        }
                    },
                    {"$limit": 1},
                ],
                "as": "log",
            }
        },
        {"$unwind": {"path": "$log", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$date",
                "date_obj": {"$first": "$date_obj"},
                "headcount": {"$sum": {"$cond": [{"$lte": ["$joined_on", "$date"]}, 1, 0]}},
                "present_count": {"$sum": present_value},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$log.late_minutes", 0]}, 0]}, 1, 0]}},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$set": {
                "is_working_day": {
                    "$lte": [
                        {
                            "$isoDayOfWeek": {
                                "date": "$date_obj",
                                "timezone": "Asia/Kolkata",
                            }
                        },
                        5,
                    ]
                }
            }
        },
        {
            "$set": {
                "attendance_rate_raw": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "moving_avg_7d_raw": {
                        "$avg": "$attendance_rate_raw",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate_raw": 1,
                "moving_avg_7d_raw": 1,
            }
        },
    ]


def run_aggregation(collection: Any, pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        return list(collection.aggregate(pipeline, allowDiskUse=True))
    except TypeError:
        return list(collection.aggregate(pipeline))
    except PyMongoError as exc:
        database_error(exc)
        raise AssertionError("unreachable")


@app.get("/health")
def health() -> dict[str, str]:
    try:
        client.admin.command("ping")
        ensure_indexes()
        app.state.index_error = None
    except PyMongoError as exc:
        app.state.index_error = str(exc)
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate) -> dict[str, Any]:
    document = body.model_dump()
    document["created_at"] = now_utc()
    try:
        db.employees.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    except PyMongoError as exc:
        database_error(exc)
    return serialize_employee(document)


@app.get("/employees")
def list_employees(
    department: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    hint = "uniq_employee_emp_code"
    if department is not None:
        query["department"] = department
        hint = "idx_employee_department_emp_code"

    try:
        total = count_documents(db.employees, query, hint)
        cursor = db.employees.find(query).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
        cursor = maybe_hint_cursor(cursor, hint)
        items = [serialize_employee(doc) for doc in cursor]
    except PyMongoError as exc:
        database_error(exc)
        raise AssertionError("unreachable")
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest) -> dict[str, Any]:
    employee = get_employee_or_404(body.emp_code)
    punched_at = epoch_ms_to_utc(body.punched_at) if body.punched_at is not None else now_utc()
    attendance_date = attendance_date_for_punch(punched_at, employee)
    derived = derived_attendance_values(employee, attendance_date, body.status.value, punched_at, None)
    document = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status.value,
        "punch_in": derived["punch_in"],
        "punch_out": None,
        "work_hours": None,
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    except PyMongoError as exc:
        database_error(exc)
    return serialize_attendance(document)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest) -> dict[str, Any]:
    employee = get_employee_or_404(body.emp_code)
    punched_at = epoch_ms_to_utc(body.punched_at) if body.punched_at is not None else now_utc()
    try:
        record = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
            sort=[("punch_in", DESCENDING)],
            hint="idx_attendance_emp_punch_in",
        )
    except (OperationFailure, TypeError):
        record = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
            sort=[("punch_in", DESCENDING)],
        )
    except PyMongoError as exc:
        database_error(exc)
        raise AssertionError("unreachable")

    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found")

    punch_in_value = record.get("punch_in")
    if punch_in_value is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    punch_in_value = truncate_to_second(punch_in_value)
    validate_punch_out_time(punch_in_value, punched_at)

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")

    date_str = record["date"]
    work_hours = compute_work_hours(punch_in_value, punched_at)
    update = {
        "punch_out": punched_at,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime_minutes(punched_at, employee, date_str),
        "half_day": work_hours < 4.50,
    }
    try:
        updated = db.attendance_logs.find_one_and_update(
            {"_id": record["_id"], "punch_out": None},
            {"$set": update},
            return_document=ReturnDocument.AFTER,
        )
    except PyMongoError as exc:
        database_error(exc)
        raise AssertionError("unreachable")
    if updated is None:
        raise HTTPException(status_code=409, detail="record is already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Annotated[str | None, Query()] = None,
    date_from: Annotated[str | None, Query(pattern=DATE_PATTERN)] = None,
    date_to: Annotated[str | None, Query(pattern=DATE_PATTERN)] = None,
    status: Annotated[Status | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    if date_from is not None:
        parse_date_param(date_from, "date_from")
    if date_to is not None:
        parse_date_param(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        validation_error("date_from must be on or before date_to")

    query = attendance_list_query(emp_code, date_from, date_to, status)
    hint = attendance_list_hint(emp_code, status)
    try:
        total = count_documents(db.attendance_logs, query, hint)
        cursor = (
            db.attendance_logs.find(query)
            .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        cursor = maybe_hint_cursor(cursor, hint)
        items = [serialize_attendance(doc) for doc in cursor]
    except PyMongoError as exc:
        database_error(exc)
        raise AssertionError("unreachable")
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def normalized_record_values(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": doc["status"],
        "punch_in": truncate_to_second(doc["punch_in"]) if doc.get("punch_in") is not None else None,
        "punch_out": truncate_to_second(doc["punch_out"]) if doc.get("punch_out") is not None else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)),
    }


def correction_filter(doc: dict[str, Any]) -> dict[str, Any]:
    filters: list[dict[str, Any]] = []
    for field in TRACKED_CORRECTION_FIELDS:
        if field in doc:
            filters.append({field: doc[field]})
        else:
            filters.append({field: {"$exists": False}})
    return {"_id": doc["_id"], "$and": filters}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    body: RegularizeRequest,
    emp_code: Annotated[str, Path()],
    date: Annotated[str, Path(pattern=DATE_PATTERN)],
) -> dict[str, Any]:
    parse_date_param(date, "date")
    employee = get_employee_or_404(emp_code)
    try:
        record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    except PyMongoError as exc:
        database_error(exc)
        raise AssertionError("unreachable")
    if record is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    before = normalized_record_values(record)
    final_status = body.status.value if body.status is not None else before["status"]

    if final_status in ("ABSENT", "LEAVE"):
        if "punch_in" in body.model_fields_set or "punch_out" in body.model_fields_set:
            validation_error("ABSENT and LEAVE records cannot include punch times")
        final_punch_in = None
        final_punch_out = None
    else:
        final_punch_in = before["punch_in"]
        final_punch_out = before["punch_out"]
        if "punch_in" in body.model_fields_set:
            final_punch_in = epoch_ms_to_utc(body.punch_in)  # type: ignore[arg-type]
        if "punch_out" in body.model_fields_set:
            final_punch_out = epoch_ms_to_utc(body.punch_out)  # type: ignore[arg-type]

    derived = derived_attendance_values(employee, date, final_status, final_punch_in, final_punch_out)
    after = {
        "status": final_status,
        **derived,
    }

    changes: dict[str, dict[str, Any]] = {}
    for field in TRACKED_CORRECTION_FIELDS:
        if before[field] != after[field]:
            changes[field] = {"from": before[field], "to": after[field]}

    if not changes:
        validation_error("regularization request changes nothing")

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    update_set = {field: after[field] for field in TRACKED_CORRECTION_FIELDS}
    try:
        updated = db.attendance_logs.find_one_and_update(
            correction_filter(record),
            {"$set": update_set, "$push": {"history": history_entry}},
            return_document=ReturnDocument.AFTER,
        )
    except PyMongoError as exc:
        database_error(exc)
        raise AssertionError("unreachable")
    if updated is None:
        raise HTTPException(status_code=409, detail="attendance record changed; retry regularization")
    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: Annotated[str, Path()],
    month: Annotated[str, Query(pattern=MONTH_PATTERN)],
) -> dict[str, Any]:
    employee = get_employee_or_404(emp_code)
    month_start_date, month_end_date, month_start, month_end = parse_month(month)
    pipeline = employee_monthly_pipeline(emp_code, month_start, month_end)
    rows = run_aggregation(db.attendance_logs, pipeline)
    row = rows[0] if rows else {}
    working_days = working_days_in_month(employee["joined_on"], month_start_date, month_end_date)
    present_days = round_half_up(row.get("present_days", 0), 2)
    attendance_pct = None
    if working_days:
        attendance_pct = round_half_up(Decimal(str(present_days)) * Decimal(100) / Decimal(working_days), 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(row.get("leave_days", 0) or 0),
        "late_count": int(row.get("late_count", 0) or 0),
        "total_late_minutes": int(row.get("total_late_minutes", 0) or 0),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0) or 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: Annotated[str, Query(pattern=MONTH_PATTERN)],
    department: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    _month_start_date, _month_end_date, month_start, month_end = parse_month(month)
    rows = run_aggregation(db.employees, department_summary_pipeline(month_start, month_end, department))
    items = []
    for row in rows:
        avg_raw = row.get("avg_work_hours_raw")
        items.append(
            {
                "department": row["department"],
                "headcount": int(row["headcount"]),
                "present_days": round_half_up(row.get("present_days", 0), 2),
                "avg_work_hours": None if avg_raw is None else round_half_up(avg_raw, 2),
                "late_count": int(row.get("late_count", 0) or 0),
                "total_late_minutes": int(row.get("total_late_minutes", 0) or 0),
                "leave_count": int(row.get("leave_count", 0) or 0),
                "on_duty_count": int(row.get("on_duty_count", 0) or 0),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Annotated[str, Query(pattern=MONTH_PATTERN)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    _month_start_date, _month_end_date, month_start, month_end = parse_month(month)
    rows = run_aggregation(db.attendance_logs, late_leaderboard_pipeline(month_start, month_end, limit, department))
    return {
        "month": month,
        "items": [
            {
                "rank": int(row["rank"]),
                "emp_code": row["emp_code"],
                "name": row["name"],
                "department": row["department"],
                "total_late_minutes": int(row["total_late_minutes"]),
                "late_count": int(row["late_count"]),
            }
            for row in rows
        ],
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: Annotated[str, Path()],
    from_date: Annotated[str, Query(alias="from", pattern=DATE_PATTERN)],
    to: Annotated[str, Query(pattern=DATE_PATTERN)],
) -> dict[str, Any]:
    start = parse_date_param(from_date, "from")
    end = parse_date_param(to, "to")
    if end < start:
        validation_error("to must be on or after from")
    day_count = (end - start).days + 1
    if day_count > 92:
        validation_error("date range cannot exceed 92 days")

    try:
        if db.employees.find_one({"department": department}, {"_id": 1}) is None:
            raise HTTPException(status_code=404, detail="unknown department")
    except PyMongoError as exc:
        database_error(exc)
        raise AssertionError("unreachable")

    rows = run_aggregation(db.employees, department_trend_pipeline(department, from_date, day_count))
    items = []
    for row in rows:
        rate = row.get("attendance_rate_raw")
        moving_avg = row.get("moving_avg_7d_raw")
        items.append(
            {
                "date": row["date"],
                "is_working_day": bool(row["is_working_day"]),
                "headcount": int(row["headcount"]),
                "present_count": round_half_up(row.get("present_count", 0), 2),
                "late_count": int(row.get("late_count", 0) or 0),
                "attendance_rate": None if rate is None else round_half_up(rate, 4),
                "moving_avg_7d": None if moving_avg is None else round_half_up(moving_avg, 4),
            }
        )
    return {"department": department, "items": items}


def explain_find(
    collection_name: str,
    query: dict[str, Any],
    sort: dict[str, int] | list[tuple[str, int]],
    page: int,
    page_size: int,
    hint: str | None,
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "find": collection_name,
        "filter": query,
        "sort": dict(sort) if isinstance(sort, list) else sort,
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }
    if hint:
        command["hint"] = hint
    return db.command("explain", command, verbosity="executionStats")


def explain_aggregate(collection_name: str, pipeline: list[dict[str, Any]]) -> dict[str, Any]:
    command = {
        "aggregate": collection_name,
        "pipeline": pipeline,
        "cursor": {},
        "allowDiskUse": True,
    }
    return db.command("explain", command, verbosity="executionStats")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Annotated[
        str,
        Path(pattern="^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    ],
    emp_code: Annotated[str | None, Query()] = None,
    month: Annotated[str | None, Query(pattern=MONTH_PATTERN)] = None,
    department: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Annotated[str | None, Query(pattern=DATE_PATTERN)] = None,
    date_to: Annotated[str | None, Query(pattern=DATE_PATTERN)] = None,
    status: Annotated[Status | None, Query()] = None,
    from_date: Annotated[str | None, Query(alias="from", pattern=DATE_PATTERN)] = None,
    to: Annotated[str | None, Query(pattern=DATE_PATTERN)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    try:
        if endpoint == "attendance_list":
            if date_from is not None:
                parse_date_param(date_from, "date_from")
            if date_to is not None:
                parse_date_param(date_to, "date_to")
            if date_from and date_to and date_from > date_to:
                validation_error("date_from must be on or before date_to")
            query = attendance_list_query(emp_code, date_from, date_to, status)
            hint = attendance_list_hint(emp_code, status)
            explain = explain_find(
                "attendance_logs",
                query,
                [("date", DESCENDING), ("emp_code", ASCENDING)],
                page,
                page_size,
                hint,
            )
            return {"endpoint": endpoint, "collection": "attendance_logs", "explain": bson_to_jsonable(explain)}

        if endpoint == "employee_monthly":
            if emp_code is None or month is None:
                validation_error("emp_code and month are required")
            _start_date, _end_date, month_start, month_end = parse_month(month)
            explain = explain_aggregate("attendance_logs", employee_monthly_pipeline(emp_code, month_start, month_end))
            return {"endpoint": endpoint, "collection": "attendance_logs", "explain": bson_to_jsonable(explain)}

        if endpoint == "department_summary":
            if month is None:
                validation_error("month is required")
            _start_date, _end_date, month_start, month_end = parse_month(month)
            explain = explain_aggregate("employees", department_summary_pipeline(month_start, month_end, department))
            return {"endpoint": endpoint, "collection": "employees", "explain": bson_to_jsonable(explain)}

        if endpoint == "late_leaderboard":
            if month is None:
                validation_error("month is required")
            _start_date, _end_date, month_start, month_end = parse_month(month)
            explain = explain_aggregate("attendance_logs", late_leaderboard_pipeline(month_start, month_end, limit, department))
            return {"endpoint": endpoint, "collection": "attendance_logs", "explain": bson_to_jsonable(explain)}

        if department is None or from_date is None or to is None:
            validation_error("department, from and to are required")
        start = parse_date_param(from_date, "from")
        end = parse_date_param(to, "to")
        if end < start:
            validation_error("to must be on or after from")
        day_count = (end - start).days + 1
        if day_count > 92:
            validation_error("date range cannot exceed 92 days")
        explain = explain_aggregate("employees", department_trend_pipeline(department, from_date, day_count))
        return {"endpoint": endpoint, "collection": "employees", "explain": bson_to_jsonable(explain)}
    except PyMongoError as exc:
        database_error(exc)
        raise AssertionError("unreachable")
