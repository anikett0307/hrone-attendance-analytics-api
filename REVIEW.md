# REVIEW.md

## Defects found

| # | Where | What is wrong | How I'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `/health` | Always said "ok" without checking MongoDB. | Stop MongoDB and call `/health`. It still returned 200. | It now pings MongoDB and returns 503 if that fails. |
| 2 | Startup | There were no indexes, so duplicates were possible and queries scanned everything. | Two identical requests at once could both succeed. The explain output showed COLLSCAN. | Indexes are created at startup. Two of them are unique: `emp_code`, and `(emp_code, date)`. |
| 3 | `POST /employees` | It checked first and then inserted, so two requests at the same moment could both pass. | Send the same `emp_code` twice at once. | The unique index decides, and a duplicate returns 409. |
| 4 | `EmployeeIn` | Nothing was validated, so bad dates, bad times and bad emails went through. | `joined_on` = `2026-13-45`, or `shift_start` = `99:99`. | Added checks for the code, email, shift times and a real date. These return 422. |
| 5 | `GET /employees` | The skip was `page * page_size`, so page 1 skipped the first employees. | Create 3 employees and ask for page 1 with size 2. | Changed to `(page - 1) * page_size`. |
| 6 | `GET /employees` | `total` ignored the department filter, there was no sort, and `page` and `page_size` were not checked. | Filter by department and look at `total`. Try `page=0`. | Total uses the same filter, results are sorted by `emp_code`, and bad paging returns 422. |
| 7 | `POST /attendance/punch-in` | It used the server's own time zone instead of IST, and kept microseconds. | Run the server in another time zone. The date came out wrong. | The time is converted to IST and cut to whole seconds. |
| 8 | `PunchInIn` | It accepted seconds instead of milliseconds, floats, and any status. | `punched_at` = `1783311000`, or `status` = `BANANA`. | The timestamp must be a whole number in a valid range, and the status must be one of the allowed ones. |
| 9 | `punch_in` | An unknown employee crashed with a 500. | Punch in with an `emp_code` that doesn't exist. | Returns 404. |
| 10 | `punch_in` | Overnight shifts were put on the wrong date. | Shift 22:00 to 06:00, punch in at 02:00. | If the shift is overnight and the punch is before shift end, the date is the day before. |
| 11 | `punch_in` | Same check-then-insert race as employees. | Two identical punch-ins at once. | The unique `(emp_code, date)` index lets one succeed and the rest get 409. |
| 12 | `compute_late_minutes` | It floored the minutes before checking the 10-minute limit, so 10 min 1 sec counted as on time. It also got very late punch-ins wrong. | Shift 09:30, punch in at 09:40:01. | It compares exact seconds to 600 and measures from the shift start on the attendance date. |
| 13 | `compute_work_hours` | Python `round()` on a float can round the wrong way. | A work time of exactly 4.495 hours. | Uses `Decimal` with half-up rounding. |
| 14 | `compute_overtime` | There was no 30-minute minimum, overnight shifts were ignored, and it crashed on naive times. | Punch out 10 minutes after shift end returned 10 instead of 0. | Overtime counts only when it is at least 30 minutes, and handles overnight shifts. |
| 15 | `GET /attendance` | It loaded every record into Python and sorted there. | Slow on 100k records. | Sorting and paging are done in MongoDB, using indexes. |
| 16 | `GET /attendance` | Dates, status and paging were not validated, and it returned raw `_id`, raw datetimes and missing fields on old records. | `date_from` = `2026-13-45`, or an old record with no `history`. | Bad input returns 422. Every record is cleaned up, with times in milliseconds and defaults filled in. |
| 17 | Missing endpoints | Punch-out, PATCH, the four analytics routes and explain did not exist. | They were missing from Swagger. | I built them all. Punch-out and PATCH use conditional updates so two requests can't both win. |

## Checked and fine as it was

| Item | Why |
|---|---|
| The 10-minute late limit | It matches the rule. Only the maths around it was wrong. |
| Department filter on employees | It is an exact, case-sensitive match, like the contract says. |
| New attendance document in `punch_in` | `punch_out`, `work_hours`, `overtime_minutes`, `half_day` and `history` already started with the right defaults. |
| Comparing `date` as a string | `YYYY-MM-DD` sorts in date order, so `$gte` and `$lte` work. |
| Dates and shift times stored as strings | The data model asks for this. |