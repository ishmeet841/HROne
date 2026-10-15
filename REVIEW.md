# REVIEW.md

Review of the supplied starter `app/main.py`.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health` | Always returned 200 without checking MongoDB. | Stop MongoDB and call `GET /health`; starter still says ok. | Ping MongoDB and return 503 on failure. |
| 2 | module database setup | Client used default timezone behavior and no short server selection timeout. | Seeded BSON datetimes come back naive; bad config can hang endpoints. | Use `tz_aware=True`, a bounded timeout, and UTC conversion helpers. |
| 3 | no startup indexes | Required uniqueness and query indexes were missing. | Parallel duplicate employee or attendance creates could both succeed. | Create idempotent unique/supporting indexes in lifespan startup and health recovery. |
| 4 | `compute_late_minutes` | Replaced the UTC hour on the punch instant, not the IST shift date; grace rule was incomplete for seconds. | `09:40:01 IST` should be 10 late minutes; overnight shifts break. | Compute shift instants from attendance date in IST, truncate seconds, and apply R2 exactly. |
| 5 | `compute_work_hours` | Used Python `round`, which is banker's/float rounding, not half-up decimal rounding. | Durations exactly at a .005 boundary round incorrectly. | Use `Decimal` with `ROUND_HALF_UP`. |
| 6 | `compute_overtime` | Ignored the 30-minute threshold, handled overnight shift end incorrectly, and mixed naive/aware datetimes. | `18:35` produced 5 minutes but should be 0; `22:00-06:00` shifts miscompute. | Compute IST shift end, move overnight end to the next day, and apply R3. |
| 7 | `EmployeeIn` / `create_employee` | Missing contract validation for `emp_code`, email, date, shift time, and `shift_start != shift_end`. | Invalid employees such as `BAD` or equal shifts were accepted. | Added Pydantic constraints and validators. |
| 8 | `create_employee` | Used check-then-insert for duplicates and stored naive `created_at`. | Concurrent creates can both pass the check; response timestamp is not epoch ms. | Rely on MongoDB unique index, catch `DuplicateKeyError`, store UTC, return epoch ms. |
| 9 | `list_employees` | Pagination skip was `page * page_size`, total ignored the filter, and results were not sorted by `emp_code`. | Page 1 skips the first page; filtered total is wrong. | Use `(page - 1) * page_size`, filtered count, sort, and page-size validation. |
| 10 | `punch_in` | Unknown employees caused a 500, status accepted any string, date used server/local date, and duplicate handling was check-then-insert. | Punch unknown `emp_code`; send `ABSENT`; punch overnight before shift end. | Return 404, restrict to presence statuses, derive IST attendance date, and catch unique-index conflicts. |
| 11 | `punch_in` response | Added an `id` field and leaked Mongo identity semantics not in the contract. | Response schema does not allow `id` or `_id`. | Serialize only contract fields and never expose `_id`. |
| 12 | `list_attendance` | Loaded all matching documents into Python and sorted in memory. | Slow on 100k logs and violates the contract's performance requirement. | Use indexed MongoDB find, sort, skip, limit, and filtered count. |
| 13 | `list_attendance` | Did not validate date ranges, status values, page bounds, or page size max. | `date_from > date_to` returned 200; large page sizes were accepted. | Added FastAPI/Pydantic query validation and explicit boundary checks. |
| 14 | missing endpoints | Punch-out, regularization, analytics, and explain endpoints were absent from the starter. | Contract paths returned 404. | Implemented every path from `openapi.yaml`. |
| 15 | audit trail | Manual corrections and history behavior were absent. | PATCH could not preserve original values or correction reason. | Added correction validation, derived-field recomputation, and append-only history. |

Looked at and kept: using FastAPI and Pydantic was the right foundation, and `emp_code` was already present in the starter request models. The issue was not the choice of framework or identifier, but the missing validation, persistence guarantees, and contract serialization.
