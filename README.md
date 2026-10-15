# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the HROne Software Engineer Trainee assignment. The app follows `openapi.yaml`, stores data in the supplied `employees` and `attendance_logs` collections, and keeps all application code in `app/main.py` as requested.

## Prerequisites

- Python 3.11 or newer
- MongoDB 6.0 or newer, local or Atlas
- macOS terminal or VS Code terminal

## Installation

```bash
cd /Users/ishmeetsingh/Desktop/candidate_kit
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` if needed:

```bash
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Real environment variables take priority over `.env`.

## MongoDB Setup

Local MongoDB with Homebrew:

```bash
brew tap mongodb/brew
brew install mongodb-community@7.0
brew services start mongodb/brew/mongodb-community
```

Atlas also works. Set `MONGO_URI` to the Atlas connection string and `MONGO_DB` to the target database name. Do not commit `.env` or credentials.

Load the supplied sample data:

```bash
python3 sample_seed.py
```

## Run

```bash
uvicorn app.main:app --port 8000 --reload
```

API docs are available at:

- `http://127.0.0.1:8000/docs`
- `http://127.0.0.1:8000/openapi.json`

Readiness check:

```bash
curl http://127.0.0.1:8000/health
```

## Tests

Unit/API tests use `mongomock` and do not require a running MongoDB server:

```bash
python3 -m pytest -q
```

Real MongoDB aggregation integration test:

```bash
RUN_MONGO_TESTS=1 MONGO_URI=mongodb://localhost:27017 python3 -m pytest -q -m integration
```

## Implemented Features

- Employee creation and listing with validation, sorting, pagination, and duplicate handling.
- Punch-in and punch-out with IST attendance dates, overnight shifts, duplicate prevention, and valid state transitions.
- Manual attendance regularization with recomputed derived fields and append-only audit history.
- Attendance listing with indexed filtering and pagination.
- Monthly employee summary, department summary, late leaderboard with competition ranking, and department trend aggregation.
- `/admin/explain/{endpoint}` for the main query or aggregation behind each analytics/list endpoint.
- Startup index creation and MongoDB-backed health check.

## Known Limitations

- Transactions are not required for the current single-document attendance updates; regularization uses an atomic optimistic update and `$push` for history.
- The default test run skips the real MongoDB analytics integration test unless `RUN_MONGO_TESTS=1` is set.
- No authentication is implemented because the assignment contract treats `regularized_by` as free text.
- No Dockerfile is included, per the assignment instruction.
