# DECISIONS.md

1. **Data model and indexes.** I kept the two supplied collections: `employees` and `attendance_logs`. API identity is always `emp_code`; `_id` is internal. I created unique indexes on `employees.emp_code` and `(emp_code, date)` for attendance, plus read indexes for employee listing, attendance listing, punch-out lookup, monthly analytics, late leaderboard, and department/date headcount queries. I considered an index on `history`, but rejected it because no endpoint searches audit entries.

2. **Attendance state and races.** Punch-in creates only `PRESENT`, `WFH`, or `ON_DUTY`. Duplicate punch-ins rely on the `(emp_code, date)` unique index: one insert wins with 201, the duplicate gets 409. Punch-out finds the most recent prior punch-in and closes it with an atomic `find_one_and_update` requiring `punch_out: None`, so simultaneous punch-outs produce one 200 and one 409.

3. **Time and derived fields.** API instants are strict epoch milliseconds; MongoDB stores UTC BSON datetimes. Punch instants are truncated to seconds before storage and calculation. Attendance dates and shifts are interpreted in IST, including overnight shifts. Work hours and reported rates use decimal half-up rounding.

4. **Analytics and ties.** Analytics use MongoDB aggregation pipelines. Department summary starts from employees so zero-log employees count in headcount. The late leaderboard uses `$rank`, so ties share a rank; if the cutoff is tied, every employee with `rank <= limit` is returned.

5. **Corrections, testing, and scale.** Manual corrections append one audit entry with before/after values and use an optimistic atomic update so concurrent edits do not lose history. Tests use FastAPI `TestClient` with `mongomock` for API behavior and an opt-in real MongoDB integration test for aggregation calculations. At 100x data, I would add precomputed monthly summary collections and consider sharding attendance by date or employee.
