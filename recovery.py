def row_to_dict(row):
    return dict(row) if row else {}


def get_latest_manual_recovery(conn):
    row = conn.execute("""
        SELECT day, notes, created_at
        FROM manual_recovery_entries
        ORDER BY day DESC
        LIMIT 1
    """).fetchone()
    return row_to_dict(row)


def get_manual_recovery_series(conn, days=30):
    rows = conn.execute("""
        SELECT day, notes, created_at
        FROM manual_recovery_entries
        ORDER BY day DESC
        LIMIT ?
    """, (days,)).fetchall()
    return [dict(r) for r in rows]


def get_best_recovery_for_day(conn, day):
    garmin_row = conn.execute("""
        SELECT
            day,
            hrv_rmssd,
            avg_stress,
            sleep_score,
            body_battery_high,
            resting_hr
        FROM daily_metrics
        WHERE day = ?
        LIMIT 1
    """, (day,)).fetchone()

    manual_row = conn.execute("""
        SELECT day, notes, created_at
        FROM manual_recovery_entries
        WHERE day = ?
        LIMIT 1
    """, (day,)).fetchone()

    if not garmin_row and not manual_row:
        return None

    data = row_to_dict(garmin_row)
    if manual_row:
        data["notes"] = manual_row["notes"]
        data["manual_created_at"] = manual_row["created_at"]
    else:
        data["notes"] = None
        data["manual_created_at"] = None

    data["source"] = "garmin_daily_metrics"
    return data


def get_latest_recovery_input(conn):
    latest_day_row = conn.execute("""
        SELECT day
        FROM daily_metrics
        ORDER BY day DESC
        LIMIT 1
    """).fetchone()

    if not latest_day_row:
        return None

    return get_best_recovery_for_day(conn, latest_day_row["day"])


def get_recovery_baselines(conn):
    rows = conn.execute("""
        SELECT
            hrv_rmssd,
            avg_stress,
            sleep_score,
            body_battery_high,
            resting_hr
        FROM daily_metrics
        ORDER BY day DESC
        LIMIT 14
    """).fetchall()

    rows = [dict(r) for r in rows]
    if not rows:
        return {
            "hrv_baseline": None,
            "stress_baseline": None,
            "sleep_baseline": None,
            "bb_baseline": None,
            "rhr_baseline": None,
            "n_days": 0,
        }

    def avg(values):
        vals = [v for v in values if v is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    return {
        "hrv_baseline": avg([r["hrv_rmssd"] for r in rows]),
        "stress_baseline": avg([r["avg_stress"] for r in rows]),
        "sleep_baseline": avg([r["sleep_score"] for r in rows]),
        "bb_baseline": avg([r["body_battery_high"] for r in rows]),
        "rhr_baseline": avg([r["resting_hr"] for r in rows]),
        "n_days": len(rows),
    }


def build_recovery_dashboard(conn):
    latest = get_latest_recovery_input(conn)
    baselines = get_recovery_baselines(conn)

    if not latest:
        return {
            "latest": None,
            "baselines": baselines,
            "gauge": None,
            "deltas": {},
        }

    latest_hrv = latest.get("hrv_rmssd")
    latest_stress = latest.get("avg_stress")
    latest_sleep = latest.get("sleep_score")
    latest_bb = latest.get("body_battery_high")
    latest_rhr = latest.get("resting_hr")

    def delta(cur, base, digits=1):
        if cur is None or base is None:
            return None
        return round(float(cur) - float(base), digits)

    deltas = {
        "hrv_delta": delta(latest_hrv, baselines.get("hrv_baseline")),
        "stress_delta": delta(latest_stress, baselines.get("stress_baseline")),
        "sleep_delta": delta(latest_sleep, baselines.get("sleep_baseline")),
        "bb_delta": delta(latest_bb, baselines.get("bb_baseline")),
        "rhr_delta": delta(latest_rhr, baselines.get("rhr_baseline")),
    }

    return {
        "latest": latest,
        "baselines": baselines,
        "deltas": deltas,
    }
    