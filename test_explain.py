import json
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000/admin/explain/"

TESTS = [
    ("attendance_list", {"page": 1, "page_size": 20}),
    ("attendance_list", {"emp_code": "EMP0001", "date_from": "2026-07-01", "date_to": "2026-07-31"}),
    ("attendance_list", {"status": "PRESENT", "date_from": "2026-07-01", "date_to": "2026-07-31"}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Engineering"}),
    ("late_leaderboard", {"month": "2026-07"}),
    ("late_leaderboard", {"month": "2026-07", "department": "Engineering", "limit": 5}),
    ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"}),
]

for name, params in TESTS:
    url = BASE + name + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url) as r:
            text = json.dumps(json.load(r))
        ix = "IXSCAN" in text
        coll = "COLLSCAN" in text
        verdict = "OK" if ix and not coll else "FAIL"
        print(f"{verdict:5} {name} {params}  IXSCAN={ix} COLLSCAN={coll}")
    except Exception as e:
        print(f"ERROR {name} {params}  {e}")