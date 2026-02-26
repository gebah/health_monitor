#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, date
from typing import Any

import requests
from garminconnect import Garmin

# ----------------------------
# Config
# ----------------------------
DB_PATH = os.environ.get(
    "GARMIN_DB_PATH",
    os.path.expanduser("/home/gba/Documenten/PycharmProjects/health_monitor/garmin.sqlite"),
)

TOKEN_DIR = os.environ.get(
    "GARMIN_TOKEN_DIR",
    os.path.expanduser("~/.config/health_monitor"),
)
TOKEN_PATH = os.path.join(TOKEN_DIR, "garmin_tokens.json")

SYNC_DAYS = int(os.environ.get("GARMIN_SYNC_DAYS", "21"))

# Strava env vars (needed for refresh during sync; initial connect happens via Flask app)
STRAVA_CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET")
STRAVA_FTP = float(os.environ.get("STRAVA_FTP", "0") or 0)

# ----------------------------
# Helpers
# ----------------------------
def safe_call(fn, *args, **kwargs):
    """Call Garmin API function and return None on failure."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        print(f"[warn] {fn.__name__}({args}) failed: {e}")
        return None


def pick_first(d: dict, paths: list[str]):
    """Pick first existing nested value from dict using dot-paths."""
    for p in paths:
        cur: Any = d
        ok = True
        for part in p.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return None


# ----------------------------
# Garmin Client
# ----------------------------
def get_client() -> Garmin:
    email = os.environ["GARMIN_EMAIL"]
    password = os.environ["GARMIN_PASSWORD"]

    os.makedirs(TOKEN_DIR, exist_ok=True)

    # Try token-based session first
    if os.path.exists(TOKEN_PATH):
        try:
            with open(TOKEN_PATH, "r", encoding="utf-8") as f:
                tokens = json.load(f)
            api = Garmin(email, password, **tokens)
            api.login()
            return api
        except Exception as e:
            print(f"[warn] token login failed, falling back to password login: {e}")

    # Fallback: password login
    api = Garmin(email, password)
    api.login()

    # Save tokens for next runs
    try:
        tokens = api.get_tokens()
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            json.dump(tokens, f)
        os.chmod(TOKEN_PATH, 0o600)
    except Exception as e:
        print(f"[warn] could not save tokens: {e}")

    return api


# ----------------------------
# SQLite Schema
# ----------------------------
def ensure_schema(conn: sqlite3.Connection) -> None:
    # Garmin activities
    conn.execute("""
    CREATE TABLE IF NOT EXISTS activities (
        activity_id INTEGER PRIMARY KEY,
        start_time_local TEXT,
        activity_name TEXT,
        activity_type TEXT,
        distance_m REAL,
        duration_s REAL,
        avg_hr REAL,
        max_hr REAL,
        avg_power REAL,
        training_effect REAL,
        vo2max_value REAL,
        raw_json TEXT,
        synced_at TEXT
    )
    """)
    conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_activities_start_time
    ON activities(start_time_local)
    """)

    # Garmin daily metrics
    conn.execute("""
    CREATE TABLE IF NOT EXISTS daily_metrics (
        day TEXT PRIMARY KEY,
        sleep_seconds REAL,
        sleep_score REAL,
        resting_hr REAL,
        avg_stress REAL,
        hrv_rmssd REAL,
        body_battery_high REAL,
        body_battery_low REAL,
        raw_json TEXT,
        synced_at TEXT
    )
    """)

    # Strava tokens (populated by Flask /strava/connect + /strava/callback)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS strava_tokens (
        athlete_id INTEGER PRIMARY KEY,
        access_token TEXT NOT NULL,
        refresh_token TEXT NOT NULL,
        expires_at INTEGER NOT NULL,
        scope TEXT,
        synced_at TEXT
    )
    """)

    # Strava activities
    conn.execute("""
    CREATE TABLE IF NOT EXISTS strava_activities (
        strava_activity_id INTEGER PRIMARY KEY,
        start_time_local TEXT,
        name TEXT,
        type TEXT,
        distance_m REAL,
        moving_time_s REAL,
        elapsed_time_s REAL,
        suffer_score REAL,
        raw_json TEXT,
        synced_at TEXT
    )
    """)
    conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_strava_acts_start_time
    ON strava_activities(start_time_local)
    """)

    conn.commit()


# ----------------------------
# Garmin Activities Sync
# ----------------------------
def fetch_activities(api: Garmin, days: int) -> list[dict[str, Any]]:
    end = date.today()
    start = end - timedelta(days=days)
    return api.get_activities_by_date(start.isoformat(), end.isoformat())


def upsert_activities(conn: sqlite3.Connection, acts: list[dict[str, Any]]) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    n = 0

    for a in acts:
        activity_id = a.get("activityId")
        if not activity_id:
            continue

        # activityType can be string or dict depending on endpoint/version
        atype = a.get("activityType")
        if isinstance(atype, dict):
            atype = atype.get("typeKey") or atype.get("typeId") or atype.get("name")
        if atype is not None and not isinstance(atype, str):
            atype = str(atype)

        # avg_power key can vary
        avg_power = a.get("averagePower")
        if avg_power is None:
            avg_power = a.get("avgPower")

        # Your JSON keys (as you printed): aerobicTrainingEffect + vO2MaxValue
        training_effect = a.get("aerobicTrainingEffect")
        vo2max_value = a.get("vO2MaxValue")

        conn.execute("""
        INSERT OR REPLACE INTO activities (
            activity_id, start_time_local, activity_name, activity_type,
            distance_m, duration_s, avg_hr, max_hr, avg_power,
            training_effect, vo2max_value, raw_json, synced_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            activity_id,
            a.get("startTimeLocal"),
            a.get("activityName"),
            atype,
            a.get("distance"),
            a.get("duration"),
            a.get("averageHR"),
            a.get("maxHR"),
            avg_power,
            training_effect,
            vo2max_value,
            json.dumps(a, ensure_ascii=False),
            now,
        ))
        n += 1

    conn.commit()
    return n


# ----------------------------
# Garmin Daily Metrics Sync
# ----------------------------
def upsert_daily_metrics(
    conn: sqlite3.Connection,
    day: str,
    payload: dict[str, Any],
    synced_at: str
) -> None:
    conn.execute("""
    INSERT OR REPLACE INTO daily_metrics (
        day, sleep_seconds, sleep_score, resting_hr, avg_stress, hrv_rmssd,
        body_battery_high, body_battery_low, raw_json, synced_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        day,
        payload.get("sleep_seconds"),
        payload.get("sleep_score"),
        payload.get("resting_hr"),
        payload.get("avg_stress"),
        payload.get("hrv_rmssd"),
        payload.get("bb_high"),
        payload.get("bb_low"),
        payload.get("raw_json"),
        synced_at,
    ))
    conn.commit()


def fetch_and_store_daily_metrics(api: Garmin, conn: sqlite3.Connection, days: int) -> int:
    now = datetime.now()
    synced_at = now.isoformat(timespec="seconds")
    saved = 0

    for i in range(days):
        d = (now.date() - timedelta(days=i)).isoformat()

        sleep = safe_call(api.get_sleep_data, d)
        stress = safe_call(api.get_stress_data, d)
        hrv = safe_call(api.get_hrv_data, d)
        bb = safe_call(api.get_body_battery, d)
        rhr = safe_call(api.get_rhr_day, d)

        payload: dict[str, Any] = {
            "sleep_seconds": None,
            "sleep_score": None,
            "resting_hr": None,
            "avg_stress": None,
            "hrv_rmssd": None,
            "bb_high": None,
            "bb_low": None,
            "raw_json": None,
        }

        raw = {"sleep": sleep, "stress": stress, "hrv": hrv, "bb": bb, "rhr": rhr}

        # ---- Sleep (primary source for sleep + RHR + overnight HRV) ----
        if isinstance(sleep, dict):
            payload["sleep_seconds"] = pick_first(sleep, [
                "dailySleepDTO.sleepTimeSeconds",
                "dailySleepDTO.totalSleepSeconds",
            ])
            payload["sleep_score"] = pick_first(sleep, [
                "dailySleepDTO.sleepScore",
                "dailySleepDTO.overallSleepScore",
                "dailySleepDTO.sleepScores.overall.value",
            ])
            payload["resting_hr"] = pick_first(sleep, [
                "restingHeartRate",
                "dailySleepDTO.restingHeartRate",
            ])
            payload["hrv_rmssd"] = pick_first(sleep, [
                "avgOvernightHrv",
                "dailySleepDTO.avgOvernightHrv",
            ])

        # ---- Stress ----
        if isinstance(stress, dict):
            payload["avg_stress"] = pick_first(stress, [
                "avgStressLevel",
                "averageStressLevel",
            ])

        # ---- HRV fallback (if not found in sleep) ----
        if payload["hrv_rmssd"] is None and isinstance(hrv, dict):
            payload["hrv_rmssd"] = pick_first(hrv, [
                "hrvSummary.rmssd",
                "hrvSummary.avgRmssd",
                "hrvSummary.overallRmssd",
            ])

        # ---- Body Battery ----
        if isinstance(bb, list) and bb and isinstance(bb[0], dict):
            arr = bb[0].get("bodyBatteryValuesArray")
            if isinstance(arr, list) and arr:
                vals = []
                for item in arr:
                    if isinstance(item, (int, float)):
                        vals.append(float(item))
                    elif isinstance(item, (list, tuple)) and len(item) >= 2 and isinstance(item[1], (int, float)):
                        vals.append(float(item[1]))
                    elif isinstance(item, dict):
                        for k in ("value", "bodyBattery", "bb"):
                            if k in item and isinstance(item[k], (int, float)):
                                vals.append(float(item[k]))
                                break
                if vals:
                    payload["bb_high"] = max(vals)
                    payload["bb_low"] = min(vals)

        # ---- RHR fallback (if not found in sleep) ----
        if payload["resting_hr"] is None and isinstance(rhr, dict):
            payload["resting_hr"] = pick_first(rhr, [
                "allMetrics.restingHeartRate.value",
                "allMetrics.RESTING_HEART_RATE.value",
            ])

        payload["raw_json"] = json.dumps(raw, ensure_ascii=False)

        if any(payload[k] is not None for k in (
            "sleep_seconds", "sleep_score", "resting_hr", "avg_stress", "hrv_rmssd", "bb_high", "bb_low"
        )):
            upsert_daily_metrics(conn, d, payload, synced_at)
            saved += 1

    return saved


# ----------------------------
# Strava Sync
# ----------------------------
def get_strava_access_token(conn: sqlite3.Connection) -> str | None:
    """
    Reads token from DB (inserted by Flask OAuth callback).
    Refreshes token if expired.
    """
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
    })
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


def sync_strava_activities(conn: sqlite3.Connection, days: int) -> int:
    """
    Pulls athlete activities from Strava and stores them.
    Weekly load will be computed in Flask from suffer_score with fallback.
    """
    access_token = get_strava_access_token(conn)
    if not access_token:
        return 0

    headers = {"Authorization": f"Bearer {access_token}"}
    after = int(time.time()) - days * 86400

    per_page = 200
    page = 1
    saved = 0
    now = datetime.now().isoformat(timespec="seconds")

    while True:
        r = requests.get(
            "https://www.strava.com/api/v3/athlete/activities",
            headers=headers,
            params={"after": after, "page": page, "per_page": per_page},
            timeout=30,
        )
        r.raise_for_status()
        acts = r.json()
        if not acts:
            break

        for a in acts:
            moving = a.get("moving_time")          # sec
            dist_m = a.get("distance")             # meters
            suffer = a.get("suffer_score")         # may be None
            avg_hr = a.get("average_heartrate")    # may be None

            tcl = None

            # 1) power-based if available
            weighted = a.get("weighted_average_watts")
            if STRAVA_FTP > 0 and weighted is not None and moving is not None:
                intensity = float(weighted) / STRAVA_FTP
                tcl = (intensity * intensity) * (float(moving) / 36.0)

            # 2) else: use suffer_score as TCL proxy
            elif suffer is not None:
                tcl = float(suffer)

            # 3) else: simple fallback
            else:
                mins = (float(moving) / 60.0) if moving else 0.0
                km = (float(dist_m) / 1000.0) if dist_m else 0.0
                tcl = mins + km * 0.5

            # Optional HR weighting (only if no suffer_score and HR exists)
            # if suffer is None and avg_hr is not None:
            #     tcl *= (float(avg_hr) / 140.0)

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

def sync_garmin(days: int = SYNC_DAYS) -> tuple[int, int]:
    """Sync Garmin activities + daily metrics. Returns (n_acts, n_days)."""
    api = get_client()

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        ensure_schema(conn)

        acts = fetch_activities(api, days)
        n_acts = upsert_activities(conn, acts)
        n_days = fetch_and_store_daily_metrics(api, conn, days)

    return n_acts, n_days


def sync_strava(days: int = SYNC_DAYS) -> int:
    """Sync Strava activities. Returns n_strava."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        ensure_schema(conn)
        n_strava = sync_strava_activities(conn, days)
    return n_strava


def sync_all(days: int = SYNC_DAYS) -> dict[str, int]:
    """Sync Garmin + Strava. Returns counts."""
    n_acts, n_days = sync_garmin(days=days)
    n_strava = sync_strava(days=days)
    return {"garmin_activities": n_acts, "daily_metrics": n_days, "strava_activities": n_strava}

def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    try:
        x = float(x)
    except Exception:
        return 50.0
    return max(lo, min(hi, x))


def fuel_score(calories: float | None, target: float) -> float | None:
    """
    Score 0..100 op basis van calorie-inname vs target.
    80-120% target => 100
    <80% => lineair naar 0 bij 50%
    >120% => lichte penalty, naar 70 bij 140%
    """
    if calories is None or target <= 0:
        return None

    ratio = float(calories) / float(target)

    if 0.8 <= ratio <= 1.2:
        return 100.0

    if ratio < 0.8:
        # 0.5 -> 0, 0.8 -> 100
        s = 100.0 * (ratio - 0.5) / (0.8 - 0.5)
        return clamp(s)

    # ratio > 1.2: 1.2->100, 1.4->70 (cap op 50)
    s = 100.0 - (ratio - 1.2) * (30.0 / (1.4 - 1.2))
    return clamp(max(50.0, s))

def get_fitatu_calories(conn, day: str) -> float | None:
    row = conn.execute("SELECT calories FROM fitatu_daily WHERE day = ?", (day,)).fetchone()
    if not row:
        return None
    v = row[0]
    return None if v is None else float(v)


def weighted_score(parts: dict[str, float | None], weights: dict[str, float]) -> int:
    used = [(k, v) for k, v in parts.items() if v is not None and k in weights]
    if not used:
        return 0
    wsum = sum(weights[k] for k, _ in used)
    score = sum((weights[k] / wsum) * float(v) for k, v in used)
    return int(round(score))


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    print("DB_PATH =", DB_PATH)
    api = get_client()

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        ensure_schema(conn)

        # Garmin
        acts = fetch_activities(api, SYNC_DAYS)
        n_acts = upsert_activities(conn, acts)
        n_days = fetch_and_store_daily_metrics(api, conn, SYNC_DAYS)

        # Strava (requires tokens already stored by Flask OAuth flow)
        n_strava = sync_strava_activities(conn, SYNC_DAYS)

    print(f"[{datetime.now().isoformat(timespec='seconds')}] synced garmin activities: {n_acts}")
    print(f"[{datetime.now().isoformat(timespec='seconds')}] synced daily_metrics days: {n_days}")
    print(f"[{datetime.now().isoformat(timespec='seconds')}] synced strava activities: {n_strava}")



if __name__ == "__main__":
    main()
