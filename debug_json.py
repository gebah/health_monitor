import json
import sqlite3

conn = sqlite3.connect("garmin.sqlite")

row = conn.execute("""

SELECT raw_json
FROM activities
WHERE raw_json IS NOT NULL
LIMIT 1

""").fetchone()

conn.close()

raw_json_string = row[0]

a = json.loads(raw_json_string)

print("KEYS:")
print(sorted(a.keys()))