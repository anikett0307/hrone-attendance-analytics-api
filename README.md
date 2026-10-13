# Employee Attendance & Analytics API

FastAPI + MongoDB backend for recording attendance, applying business rules, manual corrections with an audit trail, and monthly analytics.

## Requirements
- Python 3.11+
- MongoDB 6.0+ (needed for `$setWindowFields` and `$densify`)

## Setup

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

## Configuration

The app reads two environment variables. Real environment variables take priority over a local `.env` file.

| Variable | Default |
|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` |
| `MONGO_DB` | `attendance_db` |

For local development you can create a `.env` file (not committed):

```
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

A local MongoDB can be started with Docker: `docker run -p 27017:27017 mongo:7`

## Run

From the repository root:

```bash
uvicorn app.main:app --port 8000
```

Indexes are created automatically and idempotently at startup. Swagger UI is at http://127.0.0.1:8000/docs.

Optional: load the 9 sample documents with `python sample_seed.py`.

## Endpoints

| Method | Path |
|---|---|
| GET | `/health` |
| POST, GET | `/employees` |
| POST | `/attendance/punch-in` |
| POST | `/attendance/punch-out` |
| GET | `/attendance` |
| PATCH | `/attendance/{emp_code}/{date}` |
| GET | `/analytics/employees/{emp_code}/monthly` |
| GET | `/analytics/departments/summary` |
| GET | `/analytics/leaderboard/late` |
| GET | `/analytics/departments/{department}/trend` |
| GET | `/admin/explain/{endpoint}` |

## Other files
- `REVIEW.md`: defects found in the starter code and how they were fixed.
- `DECISIONS.md`: design decisions.

## Known limitations
- No authentication (the `regularized_by` value is free text, as the assignment specifies).
- Tested manually through Swagger and a small explain-check script. There is no automated test suite.
- Explain output was checked on a small dataset, not on the full 100,000-record dataset.
- Tested manually through Swagger and a small explain-check script. There is no automated test suite.
