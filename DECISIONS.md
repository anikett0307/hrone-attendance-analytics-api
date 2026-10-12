# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
5. **One thing you would change** if this had to serve 100x the data.



-------------------------MY ANSWER-----------------------
# DECISIONS.md

1. **Indexes.** Employees: unique `emp_code`, and `(department, joined_on)` for headcount. Attendance: unique `(emp_code, date)` guarantees one record per day; `(date desc, emp_code)` serves the default list sort; `(status, date desc, emp_code)` serves status filters; `(emp_code, punch_in desc)` finds the record to punch out. I rejected a separate `emp_code` index because the unique one already covers it.

2. **Punch-in race.** Both requests call `insert_one`. The unique `(emp_code, date)` index lets the database accept only one. The other raises `DuplicateKeyError`, which I return as 409. So exactly one request gets 201.

3. **Ties.** I rank with `$rank`, so ties share a rank (1, 2, 2, 4). I filter `rank <= limit` after ranking, so every employee tied at the cutoff is returned, even if that is more than `limit` rows.

4. **Headcount.** The summary starts from `employees`, not from logs. It keeps employees who joined on or before month end and uses `$lookup` to attach logs, so someone with zero logs still counts.

5. **At 100x data.** I would store pre-computed monthly totals per employee and read those, instead of aggregating raw logs on every request.