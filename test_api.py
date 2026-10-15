import os
import uuid
from datetime import datetime, timezone

import mongomock
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from app import main


UTC = timezone.utc


def epoch_ms(year, month, day, hour, minute, second=0, millisecond=0):
    value = datetime(year, month, day, hour, minute, second, millisecond * 1000, tzinfo=UTC)
    return int(value.timestamp() * 1000)


def utc_dt(year, month, day, hour, minute, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def employee_doc(
    emp_code="EMP0001",
    name="Asha Rao",
    department="Engineering",
    shift_start="09:30",
    shift_end="18:30",
    joined_on="2026-07-01",
):
    return {
        "emp_code": emp_code,
        "name": name,
        "email": f"{emp_code.lower()}@example.com",
        "department": department,
        "shift_start": shift_start,
        "shift_end": shift_end,
        "joined_on": joined_on,
        "created_at": utc_dt(2026, 7, 1, 4, 0),
    }


@pytest.fixture()
def api(monkeypatch):
    mongo_client = mongomock.MongoClient(tz_aware=True)
    database = mongo_client["attendance_test"]
    monkeypatch.setattr(main, "client", mongo_client)
    monkeypatch.setattr(main, "db", database)
    main.ensure_indexes()
    with TestClient(main.app) as client:
        yield client, database


def test_create_employee_duplicate_and_list_contract(api):
    client, _database = api
    payload = {
        "emp_code": "EMP1001",
        "name": "Ira Sen",
        "email": "ira@example.com",
        "department": "Engineering",
        "joined_on": "2026-07-01",
    }

    response = client.post("/employees", json=payload)
    assert response.status_code == 201
    body = response.json()
    assert body["emp_code"] == "EMP1001"
    assert isinstance(body["created_at"], int)
    assert "_id" not in body

    assert client.post("/employees", json=payload).status_code == 409

    client.post(
        "/employees",
        json={
            "emp_code": "EMP1002",
            "name": "Dev Mehta",
            "email": "dev@example.com",
            "department": "Sales",
            "joined_on": "2026-07-02",
        },
    )
    listed = client.get("/employees", params={"department": "Engineering"}).json()
    assert listed["total"] == 1
    assert [item["emp_code"] for item in listed["items"]] == ["EMP1001"]

    invalid_shift = dict(payload, emp_code="EMP1003", shift_start="09:30", shift_end="09:30")
    assert client.post("/employees", json=invalid_shift).status_code == 422


def test_punch_in_validates_timestamp_status_and_duplicate(api):
    client, database = api
    database.employees.insert_one(employee_doc())

    punched_at = epoch_ms(2026, 7, 6, 4, 10, 1, 900)
    response = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punched_at})
    assert response.status_code == 201
    body = response.json()
    assert body["date"] == "2026-07-06"
    assert body["punch_in"] == epoch_ms(2026, 7, 6, 4, 10, 1)
    assert body["late_minutes"] == 10
    assert body["punch_out"] is None
    assert body["history"] == []
    assert "_id" not in body

    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punched_at}).status_code == 409
    assert client.post("/attendance/punch-in", json={"emp_code": "MISSING", "punched_at": punched_at}).status_code == 404
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500}).status_code == 422
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": str(punched_at)}).status_code == 422
    assert (
        client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": punched_at, "status": "ABSENT"},
        ).status_code
        == 422
    )


def test_punch_out_success_duplicate_and_invalid_transitions(api):
    client, database = api
    database.employees.insert_many(
        [
            employee_doc("EMP0001", "Asha Rao", "Engineering"),
            employee_doc("EMP0002", "Vikram Shah", "Engineering"),
            employee_doc("EMP0003", "Neha Iyer", "Sales"),
        ]
    )

    punch_in = epoch_ms(2026, 7, 6, 3, 58)
    punch_out = epoch_ms(2026, 7, 6, 13, 5)
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punch_in}).status_code == 201
    response = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": punch_out})
    assert response.status_code == 200
    body = response.json()
    assert body["work_hours"] == 9.12
    assert body["overtime_minutes"] == 0
    assert body["half_day"] is False
    assert body["history"] == []

    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": punch_out}).status_code == 409
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0002", "punched_at": punch_out}).status_code == 404

    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": punch_in}).status_code == 201
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": punch_in}).status_code == 422
    too_late = epoch_ms(2026, 7, 7, 3, 58, 1)
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": too_late}).status_code == 422


def test_overnight_shift_date_and_overtime(api):
    client, database = api
    database.employees.insert_one(
        employee_doc(
            "EMP0005",
            "Meera Nair",
            "Support",
            shift_start="22:00",
            shift_end="06:00",
            joined_on="2026-03-10",
        )
    )

    response = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0005", "punched_at": epoch_ms(2026, 7, 6, 16, 25)},
    )
    assert response.status_code == 201
    assert response.json()["date"] == "2026-07-06"

    response = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0005", "punched_at": epoch_ms(2026, 7, 7, 1, 10)},
    )
    assert response.status_code == 200
    assert response.json()["work_hours"] == 8.75
    assert response.json()["overtime_minutes"] == 40


def test_regularize_recomputes_and_appends_audit_history(api):
    client, database = api
    database.employees.insert_one(employee_doc())
    database.attendance_logs.insert_one(
        {
            "emp_code": "EMP0001",
            "date": "2026-07-07",
            "status": "PRESENT",
            "punch_in": utc_dt(2026, 7, 7, 4, 35),
            "punch_out": utc_dt(2026, 7, 7, 13, 0),
            "work_hours": 8.42,
            "late_minutes": 35,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
    )

    response = client.patch(
        "/attendance/EMP0001/2026-07-07",
        json={
            "punch_in": epoch_ms(2026, 7, 7, 3, 58),
            "reason": "biometric glitch",
            "regularized_by": "hr.admin",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["late_minutes"] == 0
    assert body["work_hours"] == 9.03
    assert len(body["history"]) == 1
    changes = body["history"][0]["changes"]
    assert changes["punch_in"]["from"] == epoch_ms(2026, 7, 7, 4, 35)
    assert changes["punch_in"]["to"] == epoch_ms(2026, 7, 7, 3, 58)
    assert changes["late_minutes"] == {"from": 35, "to": 0}
    assert changes["work_hours"] == {"from": 8.42, "to": 9.03}

    stored = database.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-07"})
    assert isinstance(stored["history"][0]["changes"]["punch_in"]["from"], datetime)

    no_change = client.patch(
        "/attendance/EMP0001/2026-07-07",
        json={"reason": "already correct", "regularized_by": "hr.admin"},
    )
    assert no_change.status_code == 422


def test_regularize_absent_clears_times_and_rejects_conflicting_times(api):
    client, database = api
    database.employees.insert_one(employee_doc())
    database.attendance_logs.insert_one(
        {
            "emp_code": "EMP0001",
            "date": "2026-07-08",
            "status": "PRESENT",
            "punch_in": utc_dt(2026, 7, 8, 4, 0),
            "punch_out": utc_dt(2026, 7, 8, 8, 0),
            "work_hours": 4.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": True,
            "history": [],
        }
    )

    conflict = client.patch(
        "/attendance/EMP0001/2026-07-08",
        json={
            "status": "ABSENT",
            "punch_in": epoch_ms(2026, 7, 8, 4, 0),
            "reason": "approved correction",
            "regularized_by": "hr.admin",
        },
    )
    assert conflict.status_code == 422

    response = client.patch(
        "/attendance/EMP0001/2026-07-08",
        json={"status": "ABSENT", "reason": "approved correction", "regularized_by": "hr.admin"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ABSENT"
    assert body["punch_in"] is None
    assert body["punch_out"] is None
    assert body["work_hours"] is None
    assert body["late_minutes"] == 0
    assert body["half_day"] is False
    assert "punch_in" in body["history"][0]["changes"]


def test_list_attendance_filters_sorting_and_pagination(api):
    client, database = api
    database.employees.insert_many(
        [
            employee_doc("EMP0001", "Asha Rao", "Engineering"),
            employee_doc("EMP0002", "Vikram Shah", "Engineering"),
        ]
    )
    database.attendance_logs.insert_many(
        [
            {
                "emp_code": "EMP0002",
                "date": "2026-07-07",
                "status": "WFH",
                "punch_in": utc_dt(2026, 7, 7, 4, 0),
                "punch_out": utc_dt(2026, 7, 7, 7, 30),
                "work_hours": 3.5,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": True,
            },
            {
                "emp_code": "EMP0001",
                "date": "2026-07-07",
                "status": "PRESENT",
                "punch_in": utc_dt(2026, 7, 7, 3, 58),
                "punch_out": utc_dt(2026, 7, 7, 13, 0),
                "work_hours": 9.03,
                "late_minutes": 0,
                "overtime_minutes": 0,
            },
            {
                "emp_code": "EMP0001",
                "date": "2026-07-06",
                "status": "PRESENT",
                "punch_in": utc_dt(2026, 7, 6, 4, 35),
                "punch_out": utc_dt(2026, 7, 6, 13, 40),
                "work_hours": 9.08,
                "late_minutes": 35,
                "overtime_minutes": 40,
                "history": [],
            },
        ]
    )

    response = client.get(
        "/attendance",
        params={"date_from": "2026-07-06", "date_to": "2026-07-07", "page_size": 2},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert [(item["date"], item["emp_code"]) for item in body["items"]] == [
        ("2026-07-07", "EMP0001"),
        ("2026-07-07", "EMP0002"),
    ]
    assert body["items"][0]["half_day"] is False
    assert body["items"][0]["history"] == []

    filtered = client.get("/attendance", params={"status": "WFH"}).json()
    assert filtered["total"] == 1
    assert filtered["items"][0]["emp_code"] == "EMP0002"
    assert client.get("/attendance", params={"date_from": "2026-07-08", "date_to": "2026-07-07"}).status_code == 422


def test_analytics_pipeline_builders_use_mongodb_aggregation_stages():
    monthly = main.employee_monthly_pipeline("EMP0001", "2026-07-01", "2026-07-31")
    summary = main.department_summary_pipeline("2026-07-01", "2026-07-31", None)
    leaderboard = main.late_leaderboard_pipeline("2026-07-01", "2026-07-31", 10, None)
    trend = main.department_trend_pipeline("Engineering", "2026-07-01", 7)

    assert monthly[0]["$match"]["emp_code"] == "EMP0001"
    assert any("$lookup" in stage for stage in summary)
    assert any("$setWindowFields" in stage for stage in leaderboard)
    assert any("$lookup" in stage for stage in trend)
    assert any("$setWindowFields" in stage for stage in trend)
    assert any("$range" in str(stage) for stage in trend)


@pytest.mark.integration
def test_analytics_endpoints_against_real_mongodb(monkeypatch):
    if os.getenv("RUN_MONGO_TESTS") != "1":
        pytest.skip("set RUN_MONGO_TESTS=1 to run real MongoDB analytics integration tests")

    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    mongo_client = MongoClient(mongo_uri, serverSelectionTimeoutMS=2000, tz_aware=True)
    try:
        mongo_client.admin.command("ping")
    except PyMongoError as exc:
        mongo_client.close()
        pytest.skip(f"MongoDB is not available: {exc}")

    database_name = f"attendance_api_test_{uuid.uuid4().hex}"
    database = mongo_client[database_name]
    monkeypatch.setattr(main, "client", mongo_client)
    monkeypatch.setattr(main, "db", database)
    try:
        main.ensure_indexes()
        database.employees.insert_many(
            [
                employee_doc("EMP0001", "Asha Rao", "Engineering", joined_on="2026-07-01"),
                employee_doc("EMP0002", "Vikram Shah", "Engineering", joined_on="2026-07-15"),
                employee_doc("EMP0003", "Neha Iyer", "Sales", joined_on="2026-07-01"),
            ]
        )
        database.attendance_logs.insert_many(
            [
                {
                    "emp_code": "EMP0001",
                    "date": "2026-07-06",
                    "status": "PRESENT",
                    "punch_in": utc_dt(2026, 7, 6, 4, 35),
                    "punch_out": utc_dt(2026, 7, 6, 13, 0),
                    "work_hours": 8.42,
                    "late_minutes": 35,
                    "overtime_minutes": 0,
                    "half_day": False,
                    "history": [],
                },
                {
                    "emp_code": "EMP0001",
                    "date": "2026-07-07",
                    "status": "PRESENT",
                    "punch_in": utc_dt(2026, 7, 7, 4, 0),
                    "punch_out": utc_dt(2026, 7, 7, 7, 30),
                    "work_hours": 3.5,
                    "late_minutes": 0,
                    "overtime_minutes": 0,
                    "half_day": True,
                    "history": [],
                },
                {
                    "emp_code": "EMP0001",
                    "date": "2026-07-08",
                    "status": "LEAVE",
                    "punch_in": None,
                    "punch_out": None,
                    "work_hours": None,
                    "late_minutes": 0,
                    "overtime_minutes": 0,
                    "half_day": False,
                    "history": [],
                },
                {
                    "emp_code": "EMP0001",
                    "date": "2026-07-11",
                    "status": "PRESENT",
                    "punch_in": utc_dt(2026, 7, 11, 4, 20),
                    "punch_out": utc_dt(2026, 7, 11, 9, 20),
                    "work_hours": 5.0,
                    "late_minutes": 20,
                    "overtime_minutes": 60,
                    "half_day": False,
                    "history": [],
                },
                {
                    "emp_code": "EMP0002",
                    "date": "2026-07-16",
                    "status": "PRESENT",
                    "punch_in": utc_dt(2026, 7, 16, 4, 55),
                    "punch_out": utc_dt(2026, 7, 16, 13, 0),
                    "work_hours": 8.08,
                    "late_minutes": 55,
                    "overtime_minutes": 0,
                    "half_day": False,
                    "history": [],
                },
            ]
        )

        with TestClient(main.app) as client:
            monthly = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"})
            assert monthly.status_code == 200
            monthly_body = monthly.json()
            assert monthly_body["present_days"] == 1.5
            assert monthly_body["leave_days"] == 1
            assert monthly_body["late_count"] == 2
            assert monthly_body["total_late_minutes"] == 55
            assert monthly_body["total_overtime_minutes"] == 60
            assert monthly_body["attendance_pct"] == main.round_half_up(
                main.Decimal("1.5") * main.Decimal("100") / main.Decimal(str(monthly_body["working_days"])),
                2,
            )

            summary = client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Engineering"})
            assert summary.status_code == 200
            item = summary.json()["items"][0]
            assert item["headcount"] == 2
            assert item["present_days"] == 2.5
            assert item["avg_work_hours"] == 6.25
            assert item["late_count"] == 3
            assert item["total_late_minutes"] == 110
            assert item["leave_count"] == 1

            leaderboard = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 1})
            assert leaderboard.status_code == 200
            assert [row["emp_code"] for row in leaderboard.json()["items"]] == ["EMP0001", "EMP0002"]
            assert [row["rank"] for row in leaderboard.json()["items"]] == [1, 1]

            trend = client.get(
                "/analytics/departments/Engineering/trend",
                params={"from": "2026-07-06", "to": "2026-07-08"},
            )
            assert trend.status_code == 200
            assert trend.json()["items"] == [
                {
                    "date": "2026-07-06",
                    "is_working_day": True,
                    "headcount": 1,
                    "present_count": 1.0,
                    "late_count": 1,
                    "attendance_rate": 1.0,
                    "moving_avg_7d": 1.0,
                },
                {
                    "date": "2026-07-07",
                    "is_working_day": True,
                    "headcount": 1,
                    "present_count": 0.5,
                    "late_count": 0,
                    "attendance_rate": 0.5,
                    "moving_avg_7d": 0.75,
                },
                {
                    "date": "2026-07-08",
                    "is_working_day": True,
                    "headcount": 1,
                    "present_count": 0.0,
                    "late_count": 0,
                    "attendance_rate": 0.0,
                    "moving_avg_7d": 0.5,
                },
            ]
    finally:
        mongo_client.drop_database(database_name)
        mongo_client.close()
