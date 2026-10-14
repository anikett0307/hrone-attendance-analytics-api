# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
5. **One thing you would change** if this had to serve 100x the data.



-------------------------MY ANSWER-----------------------
# DECISIONS.md

1. **Indexes.** On employees I made a unique index on emp_code, plus (department, joined_on) so headcount lookups are quick. On attendance_logs I made a unique (emp_code, date) so one person gets one record per day, (date, emp_code) for the default list order, (status, date, emp_code) for the status filter, and (emp_code, punch_in) to find the record to punch out. I did not add a separate emp_code index on attendance because (emp_code, date) already covers it.

2. **Same punch-in twice.** Both requests try to insert. The unique index lets only one of them in. The other gets a duplicate key error and I return 409. So one request gets 201 and the rest get 409.

3. **Ties.** I rank people by late minutes (1, 2, 2, 4) and then keep everyone whose rank is within the limit. If two people tie at the cutoff, both come back, even if that gives more rows than the limit.

4. **Headcount.** The summary starts from the employees collection, not the logs. Anyone who joined on or before the end of the month is counted, even with zero logs. People who join later are left out.

5. **100x more data.** I would save monthly totals per employee and read those, instead of adding up all the raw logs on every request.