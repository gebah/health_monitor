#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import time
from garth import data
import requests
import zipfile

from datetime import datetime, timedelta, date, UTC
from typing import Any
from garminconnect import Garmin
from dotenv import load_dotenv
from readiness import compute_strava_readiness_series

from fitparse import FitFile 

load_dotenv("/etc/health_monitor.env")


# ----------------------------
# Config
# ----------------------------
DB_PATH = os.environ.get(
    "HEALTH_DB",
    os.environ.get(
        "GARMIN_DB_PATH",
        os.path.expanduser("/opt/health_monitor/health.sqlite"),
    ),
)

SYNC_DAYS = int(os.environ.get("GARMIN_SYNC_DAYS", "21"))

STRAVA_CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET")
STRAVA_FTP = float(os.environ.get("STRAVA_FTP", "0") or 0)


# ----------------------------
# Helpers
# ----------------------------
def safe_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        print(f"[warn] {fn.__name__}({args}) failed: {e}")
        return None


def pick_first(d: dict, paths: list[str]):
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
    token_dir = os.getenv("GARMIN_TOKEN_DIR", "/home/gba/.garminconnect")
    api = Garmin()
    api.login(token_dir)
    return api


# ----------------------------
# SQLite Schema
# ----------------------------
def ensure_schema(conn: sqlite3.Connection) -> None:
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
        tcl REAL,
        raw_json TEXT,
        synced_at TEXT
    )
    """)
    conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_strava_acts_start_time
    ON strava_activities(start_time_local)
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS strava_daily_load (
        day TEXT PRIMARY KEY,
        daily_load REAL
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS readiness_daily (
        day TEXT PRIMARY KEY,
        daily_load REAL,
        fatigue REAL,
        fitness REAL,
        form REAL,
        readiness_score INTEGER,
        recovery_gauge INTEGER,
        source TEXT DEFAULT 'strava'
    )
    """)

    # Voor bestaande DB's waar tcl nog ontbreekt
    cols = {row[1] for row in conn.execute("PRAGMA table_info(strava_activities)").fetchall()}
    if "tcl" not in cols:
        conn.execute("ALTER TABLE strava_activities ADD COLUMN tcl REAL")
        
    conn.commit()

def ensure_strength_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strength_sessions (
            activity_id TEXT PRIMARY KEY,
            day TEXT,
            name TEXT,
            activity_type TEXT,
            duration_s INTEGER,
            total_volume REAL,
            total_reps INTEGER,
            total_sets INTEGER,
            raw_json TEXT,
            synced_at TEXT
        )
    """)
    conn.commit()

def ensure_manual_recovery_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS manual_recovery_entries (
            day TEXT PRIMARY KEY,
            hrv_rmssd REAL,
            stress REAL,
            sleep_score REAL,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
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

        atype = a.get("activityType")
        if isinstance(atype, dict):
            atype = atype.get("typeKey") or atype.get("typeId") or atype.get("name")
        if atype is not None and not isinstance(atype, str):
            atype = str(atype)

        avg_power = a.get("averagePower")
        if avg_power is None:
            avg_power = a.get("avgPower")

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

        if isinstance(stress, dict):
            payload["avg_stress"] = pick_first(stress, [
                "avgStressLevel",
                "averageStressLevel",
            ])

        if payload["hrv_rmssd"] is None and isinstance(hrv, dict):
            payload["hrv_rmssd"] = pick_first(hrv, [
                "hrvSummary.rmssd",
                "hrvSummary.avgRmssd",
                "hrvSummary.overallRmssd",
            ])

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
        else:
            print("nothing usable for", d, flush=True)

    return saved

def sync_strength_fit_data(api, conn, activities):
    ensure_strength_schema(conn)

    os.makedirs(FIT_DIR, exist_ok=True)
    synced_at = datetime.now().isoformat(timespec="seconds")
    saved = 0

    for activity in activities:
        if not isinstance(activity, dict):
            continue

        # Alleen echte Garmin strength_training activiteiten
        if not is_strength_activity(activity):
            continue

        activity_id = get_activity_id(activity)
        if not activity_id:
            print("SKIP strength: no activity_id", flush=True)
            continue

        activity_type_obj = activity.get("activityType") or activity.get("activitytype") or {}
        type_key = ""
        if isinstance(activity_type_obj, dict):
            type_key = (
                activity_type_obj.get("typeKey")
                or activity_type_obj.get("typekey")
                or ""
            ).lower()

        start_time_local = (
            activity.get("startTimeLocal")
            or activity.get("start_time_local")
            or activity.get("start_date_local")
            or activity.get("startTimeGMT")
        )

        day = str(start_time_local)[:10] if start_time_local else None

        name = (
            activity.get("activityName")
            or activity.get("activityname")
            or activity.get("name")
            or ""
        )

        duration_s = (
            activity.get("duration")
            or activity.get("moving_time_s")
            or activity.get("movingDuration")
            or activity.get("movingduration")
        )

        print(
            "DOWNLOADING STRENGTH FIT:",
            activity_id,
            name,
            start_time_local,
            type_key,
            flush=True,
        )

        fit_path = os.path.join(FIT_DIR, f"{activity_id}.fit")
        fit_path = download_fit_file(api, activity_id, fit_path)

        if not fit_path:
            print(f"SKIP strength {activity_id}: no fit file", flush=True)
            continue

        parsed = parse_strength_fit(fit_path)
        duration_min = int(duration_s or 0) / 60.0

        strength_load = (
            float(parsed.get("total_sets") or 0) * 5.0 +
            float(parsed.get("total_reps") or 0) * 0.5 +
            duration_min * 0.3
        )

        if parsed.get("error"):
            print(f"[warn] FIT parse failed for {activity_id}: {parsed['error']}", flush=True)

        conn.execute("""
            INSERT OR REPLACE INTO strength_sessions (
                activity_id,
                day,
                name,
                activity_type,
                duration_s,
                total_volume,
                total_reps,
                total_sets,
                strength_load,
                raw_json,
                synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(activity_id),
            day,
            name,
            type_key,
            int(duration_s or 0),
            parsed.get("total_volume"),
            parsed.get("total_reps"),
            parsed.get("total_sets"),
            strength_load,
            json.dumps(parsed, ensure_ascii=False),
            synced_at,
        ))

        saved += 1

    conn.commit()
    return saved


# ----------------------------
# Strava Sync
# ----------------------------
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

def update_last_strava_sync(conn):
    conn.execute("""
        INSERT INTO app_cache(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at=excluded.updated_at
    """, (
        "last_strava_sync",
        datetime.now(UTC).isoformat(),
        datetime.now(UTC).isoformat()
    ))
    conn.commit()

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

            conn.execute("""
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
            """, (
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
            ))
            saved += 1

        if len(acts) < per_page:
            break
        page += 1

    conn.commit()
    return saved


def rebuild_strava_daily_load(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM strava_daily_load")

    conn.execute("""
        INSERT INTO strava_daily_load (day, daily_load)
        SELECT
            date(start_time_local) AS day,
            SUM(COALESCE(tcl, 0)) AS daily_load
        FROM strava_activities
        WHERE start_time_local IS NOT NULL
        GROUP BY date(start_time_local)
        ORDER BY day
    """)

    conn.commit()


def rebuild_readiness(conn: sqlite3.Connection) -> None:
    rows = conn.execute("""
        SELECT day, daily_load
        FROM strava_daily_load
        ORDER BY day
    """).fetchall()

    if not rows:
        conn.execute("DELETE FROM readiness_daily")
        conn.commit()
        return

    data = [
        (datetime.fromisoformat(r[0]).date(), float(r[1] or 0.0))
        for r in rows
    ]

    series = compute_strava_readiness_series(data)

    conn.execute("DELETE FROM readiness_daily")
    conn.executemany("""
        INSERT INTO readiness_daily (
            day, daily_load, fatigue, fitness, form,
            readiness_score, recovery_gauge, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (
            row["day"],
            row["daily_load"],
            row["fatigue"],
            row["fitness"],
            row["form"],
            row["readiness_score"],
            row["recovery_gauge"],
            row["source"],
        )
        for row in series
    ])
    conn.commit()


def sync_garmin(days: int = SYNC_DAYS) -> tuple[int, int]:
    api = get_client()

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        ensure_schema(conn)
        ensure_strength_schema(conn)

        acts = fetch_activities(api, days)
        n_acts = upsert_activities(conn, acts)
        n_days = fetch_and_store_daily_metrics(api, conn, days)

        n_strength = sync_strength_fit_data(api, conn, acts)
        print(f"[ok] Strength FIT sync: {n_strength} sessions")

    return n_acts, n_days


def sync_strava(days: int = SYNC_DAYS) -> int:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        ensure_schema(conn)
        n_strava = sync_strava_activities(conn, days)
        rebuild_strava_daily_load(conn)
        rebuild_readiness(conn)
        update_last_strava_sync(conn)

    return n_strava


def sync_all(days: int = SYNC_DAYS) -> dict[str, int]:
    result = {
        "garmin_activities": 0,
        "daily_metrics": 0,
        "strava_activities": 0,
    }

    try:
        n_acts, n_days = sync_garmin(days=days)
        result["garmin_activities"] = n_acts
        result["daily_metrics"] = n_days
    except Exception as e:
        print(f"[warn] Garmin sync skipped: {e}")

    try:
        result["strava_activities"] = sync_strava(days=days)
    except Exception as e:
        print(f"[warn] Strava sync failed: {e}")
        raise

    return result


def clamp_score(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    try:
        x = float(x)
    except Exception:
        return 50.0
    return max(lo, min(hi, x))


def fuel_score(calories: float | None, target: float) -> float | None:
    if calories is None or target <= 0:
        return None

    ratio = float(calories) / float(target)

    if 0.8 <= ratio <= 1.2:
        return 100.0

    if ratio < 0.8:
        s = 100.0 * (ratio - 0.5) / (0.8 - 0.5)
        return clamp_score(s)

    s = 100.0 - (ratio - 1.2) * (30.0 / (1.4 - 1.2))
    return clamp_score(max(50.0, s))


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

FIT_DIR = "/opt/health_monitor/data/fit"


def ensure_strength_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strength_sessions (
            activity_id TEXT PRIMARY KEY,
            day TEXT,
            name TEXT,
            activity_type TEXT,
            duration_s INTEGER,
            total_volume REAL,
            total_reps INTEGER,
            total_sets INTEGER,
            raw_json TEXT,
            synced_at TEXT
        )
    """)
    conn.commit()

def is_strength_activity(activity: dict) -> bool:
    activity_type = activity.get("activityType") or activity.get("activitytype") or {}

    if not isinstance(activity_type, dict):
        return False

    type_key = (
        activity_type.get("typeKey")
        or activity_type.get("typekey")
        or ""
    ).lower()

    return type_key == "strength_training"

def get_activity_id(activity: dict):
    return (
        activity.get("activityId")
        or activity.get("activity_id")
        or activity.get("id")
    )


def download_fit_file(api, activity_id, fit_path):
    os.makedirs(os.path.dirname(fit_path), exist_ok=True)

    # Als er al een geldige FIT staat: hergebruiken
    if os.path.exists(fit_path):
        try:
            with open(fit_path, "rb") as f:
                header = f.read(20)

            if b".FIT" in header:
                return fit_path

            print(f"[warn] existing file is not a valid FIT, redownloading: {fit_path}", flush=True)
            os.remove(fit_path)

        except Exception as e:
            print(f"[warn] could not validate existing FIT {fit_path}: {e}", flush=True)
            try:
                os.remove(fit_path)
            except Exception:
                pass

    try:
        data = api.download_activity(
            activity_id,
            dl_fmt=api.ActivityDownloadFormat.ORIGINAL
        )
    except Exception as e:
        print(f"[warn] ORIGINAL download failed for {activity_id}: {e}", flush=True)
        return None

    if not data:
        print(f"[warn] empty download for {activity_id}", flush=True)
        return None

    if isinstance(data, str):
        data = data.encode("utf-8")

    print(f"download bytes header for {activity_id}: {data[:8]!r}", flush=True)

    # ORIGINAL is vaak een ZIP
    if data[:2] == b"PK":
        zip_path = fit_path.replace(".fit", ".zip")

        try:
            with open(zip_path, "wb") as f:
                f.write(data)

            with zipfile.ZipFile(zip_path, "r") as z:
                fit_names = [
                    n for n in z.namelist()
                    if n.lower().endswith(".fit")
                ]

                if not fit_names:
                    print(f"[warn] no .fit found in zip for {activity_id}: {z.namelist()}", flush=True)
                    return None

                # Meestal is er maar één FIT. Als er meerdere zijn, pak de grootste.
                fit_names = sorted(
                    fit_names,
                    key=lambda n: z.getinfo(n).file_size,
                    reverse=True,
                )

                with z.open(fit_names[0]) as src:
                    fit_bytes = src.read()

                if b".FIT" not in fit_bytes[:20]:
                    print(f"[warn] extracted file is not valid FIT for {activity_id}: {fit_names[0]}", flush=True)
                    return None

                with open(fit_path, "wb") as dst:
                    dst.write(fit_bytes)

            return fit_path

        except Exception as e:
            print(f"[warn] unzip failed for {activity_id}: {e}", flush=True)
            return None

    # Soms krijg je direct een FIT
    if b".FIT" in data[:20]:
        with open(fit_path, "wb") as f:
            f.write(data)
        return fit_path

    print(f"[warn] downloaded file is not FIT/ZIP for {activity_id}: {data[:120]!r}", flush=True)
    return None

def parse_strength_fit(fit_path):
    total_volume = 0.0
    total_reps = 0
    total_sets = 0
    sets = []

    try:
        fitfile = FitFile(fit_path)
        
    except Exception as e:
        return {
            "total_volume": 0.0,
            "total_reps": 0,
            "total_sets": 0,
            "sets": [],
            "error": str(e),
        }

    for msg in fitfile.get_messages():        
        if msg.name not in ("set", "record"):
            continue

        data = {field.name: field.value for field in msg}

        if msg.name == "set" and data.get("set_type") == "rest":
            continue

        if msg.name == "set":
            print("ACTIVE SET DATA:", data, flush=True)

            reps = (
                data.get("repetitions")
                or data.get("reps")
                or data.get("num_repetitions")
            )

        weight = (
            data.get("weight")
            or data.get("weight_display_unit")
        )

        exercise = (
            data.get("exercise_name")
            or data.get("wkt_step_name")
            or data.get("category")
        )

        try:
            reps_val = int(reps) if reps is not None else None
        except Exception:
            reps_val = None

        try:
            weight_val = float(weight) if weight is not None else None
        except Exception:
            weight_val = None

        if reps_val is None:
            continue

        total_sets += 1
        total_reps += reps_val

        volume = None
        if weight_val is not None:
            volume = reps_val * weight_val
            total_volume += volume

        sets.append({
            "exercise": str(exercise) if exercise is not None else None,
            "reps": reps_val,
            "weight": weight_val,
            "volume": volume,
        })

    return {
        "total_volume": round(total_volume, 1),
        "total_reps": total_reps,
        "total_sets": total_sets,
        "sets": sets,
    }
    
def debug_fit_messages(fit_path, limit=80):
    fitfile = FitFile(fit_path)

    seen = 0
    for msg in fitfile.get_messages():
        data = {field.name: field.value for field in msg}

        interesting = any(
            k in data
            for k in [
                "repetitions",
                "weight",
                "exercise_name",
                "category",
                "wkt_step_name",
                "set_type",
                "message_index",
            ]
        )

        if msg.name in ("set", "record", "lap", "session") or interesting:
            print("MSG:", msg.name, data, flush=True)
            seen += 1

        if seen >= limit:
            break    


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    print("DB_PATH =", DB_PATH)

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        ensure_schema(conn)

        n_strava = sync_strava_activities(conn, SYNC_DAYS)
        rebuild_strava_daily_load(conn)
        rebuild_readiness(conn)

    print(f"[{datetime.now().isoformat(timespec='seconds')}] synced strava activities: {n_strava}")


if __name__ == "__main__":
    main()
