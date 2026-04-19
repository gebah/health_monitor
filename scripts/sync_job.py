import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import time
import requests
import json
from datetime import UTC, datetime
from collector import sync_garmin, DB_PATH

STRAVA_FTP = int(os.getenv("STRAVA_FTP", "250"))
STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_strava_access_token(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT athlete_id, access_token, refresh_token, expires_at
        FROM strava_tokens
        LIMIT 1
        """
    ).fetchone()

    if not row:
        print("[info] No Strava tokens found. First link via Flask: /strava/connect")
        return None

    athlete_id, access_token, refresh_token, expires_at = row
    now = int(time.time())

    if now < int(expires_at) - 60:
        return access_token

    if not STRAVA_CLIENT_ID or not STRAVA_CLIENT_SECRET:
        print("[warn] Strava token expired but STRAVA_CLIENT_ID/SECRET not set.")
        return None

    print("[info] Refreshing Strava token...")

    r = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    r.raise_for_status()
    tok = r.json()

    conn.execute(
        """
        UPDATE strava_tokens
        SET access_token = ?,
            refresh_token = ?,
            expires_at = ?,
            synced_at = datetime('now')
        WHERE athlete_id = ?
        """,
        (
            tok["access_token"],
            tok["refresh_token"],
            int(tok["expires_at"]),
            athlete_id,
        ),
    )
    conn.commit()

    return tok["access_token"]


def sync_strava_activities(conn: sqlite3.Connection, days: int | None = 30) -> int:
    access_token = get_strava_access_token(conn)
    if not access_token:
        return 0

    headers = {"Authorization": f"Bearer {access_token}"}

    per_page = 200
    page = 1
    saved = 0
    now = datetime.now().isoformat(timespec="seconds")

    while True:
        params = {"page": page, "per_page": per_page}
        if days is not None and days > 0:
            params["after"] = int(time.time()) - days * 86400

        r = requests.get(
            "https://www.strava.com/api/v3/athlete/activities",
            headers=headers,
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        acts = r.json()
        if not acts:
            break

        for a in acts:
            moving = a.get("moving_time")
            dist_m = a.get("distance")
            suffer = a.get("suffer_score")
            tcl = None

            sport_type = a.get("sport_type")
            base_type = a.get("type")

            ride_kind = None
            if sport_type == "Ride":
                ride_kind = "road"
            elif sport_type == "GravelRide":
                ride_kind = "gravel"
            elif sport_type == "MountainBikeRide":
                ride_kind = "mountain"

            weighted = a.get("weighted_average_watts")
            if STRAVA_FTP > 0 and weighted is not None and moving is not None:
                intensity = float(weighted) / STRAVA_FTP
                tcl = (intensity * intensity) * (float(moving) / 36.0)
            elif suffer is not None:
                tcl = float(suffer)
            else:
                mins = (float(moving) / 60.0) if moving else 0.0
                km = (float(dist_m) / 1000.0) if dist_m else 0.0
                tcl = mins + km * 0.5

            conn.execute(
                """
                INSERT OR REPLACE INTO strava_activities (
                    strava_activity_id,
                    start_time_local,
                    name,
                    type,
                    sport_type,
                    ride_kind,
                    distance_m,
                    moving_time_s,
                    elapsed_time_s,
                    suffer_score,
                    tcl,
                    raw_json,
                    synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    a["id"],
                    a.get("start_date_local"),
                    a.get("name"),
                    base_type,
                    sport_type,
                    ride_kind,
                    a.get("distance"),
                    a.get("moving_time"),
                    a.get("elapsed_time"),
                    a.get("suffer_score"),
                    tcl,
                    json.dumps(a, ensure_ascii=False),
                    now,
                ),
            )
            saved += 1

        if len(acts) < per_page:
            break
        page += 1

    conn.commit()
    return saved


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/sync_job.py [garmin|strava] [days]")
        return 1

    job = sys.argv[1].lower()
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30

    if job == "garmin":
        with get_conn() as conn:
            log_id = collector_log_start(conn, "garmin")

        try:
            n_acts, n_days = sync_garmin(days)

            with get_conn() as conn:
                collector_log_finish(
                    conn,
                    log_id,
                    "success",
                    f"Garmin sync klaar: {n_acts} activities, {n_days} daily metrics",
                    {
                        "activities": n_acts,
                        "daily_metrics": n_days,
                        "days": days,
                    },
                ),
            print(f"[ok] Garmin sync complete: activities={n_acts}, daily_metrics={n_days}")
            return 0

        except Exception as e:
            with get_conn() as conn:
                collector_log_finish(
                    conn,
                    log_id,
                    "error",
                    str(e),
                    {"days": days},
                )
            raise
        
    if job == "strava":
        with get_conn() as conn:
            log_id = collector_log_start(conn, "strava")

        try:
            with get_conn() as conn:
                saved = sync_strava_activities(conn, days)

            now_iso = datetime.now(UTC).isoformat()

            with get_conn() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS app_cache (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """)
                conn.execute("""
                    INSERT INTO app_cache(key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                """, (
                    "last_strava_sync",
                    json.dumps({"ts": now_iso}),
                    now_iso,
                ))

                collector_log_finish(
                    conn,
                    log_id,
                    "success",
                    f"Strava sync klaar: {saved} activities",
                    {
                        "activities": saved,
                        "days": days,
                    },
                ),
                conn.commit()

            print(f"[ok] Strava sync complete: saved={saved}")
            return 0

        except Exception as e:
            with get_conn() as conn:
                collector_log_finish(
                    conn,
                    log_id,
                    "error",
                    str(e),
                    {"days": days},
                )
            raise

    print(f"Unknown job: {job}")
    return 1

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_collector_log_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collector_run_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collector TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            message TEXT,
            details_json TEXT
        )
    """)


def collector_log_start(conn, collector: str) -> int:
    ensure_collector_log_table(conn)
    cur = conn.execute("""
        INSERT INTO collector_run_log (collector, started_at, status)
        VALUES (?, datetime('now'), 'running')
    """, (collector,))
    conn.commit()
    return cur.lastrowid


def collector_log_finish(conn, log_id: int, status: str, message: str = "", details: dict | None = None):
    ensure_collector_log_table(conn)
    conn.execute("""
        UPDATE collector_run_log
        SET finished_at = datetime('now'),
            status = ?,
            message = ?,
            details_json = ?
        WHERE id = ?
    """, (
        status,
        message,
        json.dumps(details or {}, ensure_ascii=False),
        log_id,
    ))
    conn.commit()

if __name__ == "__main__":
    raise SystemExit(main())
