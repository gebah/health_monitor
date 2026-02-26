import sqlite3, json, os

DB_PATH = os.environ.get("GARMIN_DB_PATH", os.path.expanduser("~/garmin-sync/garmin.sqlite"))
print(DB_PATH)

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
row = conn.execute("SELECT day, raw_json FROM daily_metrics ORDER BY day DESC LIMIT 1").fetchone()
print("DAY:", row["day"])
data = json.loads(row["raw_json"])
for k in ["sleep","stress","hrv","bb","rhr"]:
    v = data.get(k)
    print("\n==", k, "==")
    if isinstance(v, dict):
        print("keys:", list(v.keys())[:40])
    elif isinstance(v, list):
        print("list len:", len(v))
        if v:
            print("first keys:", list(v[0].keys())[:40] if isinstance(v[0], dict) else type(v[0]))
    else:
        print(type(v), v)
