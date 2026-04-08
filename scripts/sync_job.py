import sqlite3
import time
import requests
import json
from datetime import datetime

DB = "/home/gba/Documenten/PycharmProjects/health_monitor/health.sqlite"
STRAVA_FTP = 250  # Set your FTP here or via env vars

def get_strava_access_token(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("""
        SELECT athlete_id, access_token, refresh_token, expires_at
        FROM strava_tokens
        LIMIT 1
    """).fetchone()

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

    r = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }, timeout=30)
    r.raise_for_status()
    tok = r.json()

    conn.execute("""
        UPDATE strava_tokens
        SET access_token = ?,
            refresh_token = ?,
            expires_at = ?,
            synced_at = datetime('now')
        WHERE athlete_id = ?
    """, (
        tok["access_token"],
        tok["refresh_token"],
        int(tok["expires_at"]),
        athlete_id
    ))
    conn.commit()

    return tok["access_token"]


def get_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


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
        params = {
            "page": page,
            "per_page": per_page,
        }

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

            conn.execute("""
                INSERT OR REPLACE INTO strava_activities (
                    strava_activity_id, start_time_local, name, type,
                    distance_m, moving_time_s, elapsed_time_s, suffer_score,
                    tcl, raw_json, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                a["id"],
                a.get("start_date_local"),
                a.get("name"),
                a.get("type"),
                a.get("distance"),
                a.get("moving_time"),
                a.get("elapsed_time"),
                a.get("suffer_score"),
                tcl,
                json.dumps(a, ensure_ascii=False),
                now,
            ))
            saved += 1

        if len(acts) < per_page:
            break

        page += 1

    conn.commit()
    return saved

conn = get_conn()
sync_strava_activities(conn, None)