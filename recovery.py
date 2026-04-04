
def row_to_dict(row):
    return dict(row) if row else {}

def get_latest_manual_recovery(conn):
    row = conn.execute("""
        SELECT day, hrv_rmssd, stress, sleep_score, notes, created_at
        FROM manual_recovery_entries
        ORDER BY day DESC
        LIMIT 1
    """).fetchone()
    return row_to_dict(row)

def get_manual_recovery_series(conn, days=30):
    rows = conn.execute("""
        SELECT day, hrv_rmssd, stress, sleep_score, notes
        FROM manual_recovery_entries
        ORDER BY day DESC
        LIMIT ?
    """, (days,)).fetchall()
    return [dict(r) for r in rows]

def get_best_recovery_for_day(conn, day):
    row = conn.execute("""
        SELECT day, hrv_rmssd, stress, sleep_score, notes
        FROM manual_recovery_entries
        WHERE day = ?
        LIMIT 1
    """, (day,)).fetchone()

    if row:
        data = dict(row)
        data["source"] = "garmin_manual"
        return data

    return None

def get_latest_recovery_input(conn):
    row = conn.execute("""
        SELECT day, hrv_rmssd, stress, sleep_score, notes
        FROM manual_recovery_entries
        ORDER BY day DESC
        LIMIT 1
    """).fetchone()

    if not row:
        return None

    data = dict(row)
    data["source"] = "garmin_manual"
    return data

def get_recovery_baselines(conn):
    rows = conn.execute("""
        SELECT hrv_rmssd, stress, sleep_score
        FROM manual_recovery_entries
        ORDER BY day DESC
        LIMIT 14
    """).fetchall()

    rows = [dict(r) for r in rows]
    if not rows:
        return {
            "hrv_baseline": None,
            "stress_baseline": None,
            "sleep_baseline": None,
            "n_days": 0,
        }

    def avg(values):
        vals = [v for v in values if v is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    return {
        "hrv_baseline": avg([r["hrv_rmssd"] for r in rows]),
        "stress_baseline": avg([r["stress"] for r in rows]),
        "sleep_baseline": avg([r["sleep_score"] for r in rows]),
        "n_days": len(rows),
    }