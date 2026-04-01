from flask import Flask, render_template, request, jsonify, redirect, current_app, url_for, flash
from datetime import date, datetime, timedelta
from collector import sync_garmin, sync_strava, sync_all
from models import avg_ignore_none, get_daily_metrics_history, calc_body_deltas

import sqlite3
import json
import csv
import io

app = Flask(__name__)
app.secret_key = "secret"

DB = "/home/gba/Documenten/PycharmProjects/health_monitor/health.sqlite"

USER_NAME = "Gé"
USER_HEIGHT = 1.92

def row_to_dict(row):
    return dict(row) if row else {}


def to_float(value, default=None):
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", ".")
    if s == "":
        return default
    try:
        return float(s)
    except ValueError:
        return default


def clamp(value, low=0, high=100):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = low
    return max(low, min(high, value))


def avg_ignore_none(values):
    vals = [to_float(v) for v in values if to_float(v) is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)

def get_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_cache_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_cache (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)


def cache_set(conn, key, obj):
    ensure_cache_table(conn)
    conn.execute("""
        INSERT INTO app_cache(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at=excluded.updated_at
    """, (key, json.dumps(obj), datetime.utcnow().isoformat()))
    conn.commit()

def cache_get(conn, key, default=None):
    ensure_cache_table(conn)
    row = conn.execute("SELECT value FROM app_cache WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return default

def row_to_dict(row):
    return dict(row) if row else {}

def readiness_score(
    latest,
    *,
    tcl_7d=0.0,
    tcl_target_7d=300.0,
    tsb=None,
    atl=None,
    ctl=None,
    flags=None
):
    latest = latest or {}
    flags = flags or {"hrv_drop": False, "sleep_low": False, "stress_high": False}

    hrv = to_float(latest.get("hrv_rmssd"))
    sleep = to_float(latest.get("sleep_score"))
    stress = to_float(latest.get("avg_stress"))
    bb = to_float(latest.get("body_battery_high"))

    hrv_score = clamp((hrv - 20) * 1.5) if hrv is not None else 50
    sleep_score = sleep if sleep is not None else 50
    stress_score = clamp(100 - stress * 2) if stress is not None else 50
    bb_score = bb if bb is not None else 50

    # ---- Training load score (ratio tcl_7d vs target) ----
    target = max(1.0, float(tcl_target_7d))
    ratio = float(tcl_7d) / target

    if ratio <= 0.60:
        load_score = 80.0
    elif ratio <= 1.00:
        load_score = 80.0 + (ratio - 0.60) / 0.40 * 20.0
    elif ratio <= 1.40:
        load_score = 100.0 - (ratio - 1.00) / 0.40 * 40.0
    else:
        load_score = 40.0

    load_score = clamp(load_score)

    # ---- Core readiness ----
    score = round(
        0.30 * hrv_score +
        0.30 * sleep_score +
        0.15 * stress_score +
        0.10 * bb_score +
        0.15 * load_score
    )

    label = "Good" if score >= 75 else "OK" if score >= 50 else "Low"
        # ------------------------
    # VISUAL META (tone + icon)
    # ------------------------

    if tsb is not None:
        tsb_val = float(tsb)

        if tsb_val >= 10:
            tone = "good"
            icon = "🟢"
            state = "Fresh"

        elif tsb_val >= 0:
            tone = "good"
            icon = "🟢"
            state = "Ready"

        elif tsb_val >= -10:
            tone = "ok"
            icon = "🟠"
            state = "Normal"

        elif tsb_val >= -25:
            tone = "ok"
            icon = "🟠"
            state = "Heavy fatigue"

        else:
            tone = "bad"
            icon = "🔴"
            state = "Recovery needed"

    else:
        tone = "ok"
        icon = "⚪"
        state = "Unknown"

    # ------------------------
    # ULTRA ADVICE LOGIC
    # ------------------------
    # Base recommendation from TSB (form)
    # TSB bands (TrainingPeaks-ish): >=10 race-ready, 0-10 fresh, -10-0 normal, -25--10 heavy, <=-25 too much
    tsb_val = None if tsb is None else float(tsb)

    def downgrade(msg):
        # degrade intensity one step
        if "interval" in msg.lower() or "hard" in msg.lower() or "kwaliteit" in msg.lower():
            return "Advies: tempo/threshold (kort) of stevige duur; geen max-intervals."
        if "tempo" in msg.lower() or "threshold" in msg.lower():
            return "Advies: normale duur/kracht, houd het gecontroleerd."
        return "Advies: rustige duur (Z1–Z2) of herstel."

    # start with TSB-based advice
    if tsb_val is None:
        advice = "Advies: normale training; gebruik readiness als leidraad."
    elif tsb_val <= -25:
        advice = "Advies: rust / herstel (wandelen, mobiliteit, evt. 30–45 min Z1)."
    elif tsb_val <= -10:
        advice = "Advies: rustige duur (Z2) of techniek; geen intensiteit."
    elif tsb_val < 0:
        advice = "Advies: normale training (duur/kracht); intensiteit liever kort en gecontroleerd."
    elif tsb_val < 10:
        advice = "Advies: goede dag voor kwaliteit (tempo/threshold of intervals) als je je goed voelt."
    else:
        advice = "Advies: topdag voor hard (intervals), PR-poging of wedstrijd."

    # combine with readiness (override extremes)
    if score < 40:
        advice = "Advies: rustdag aanbevolen. Focus op herstel."
    elif score < 50 and tsb_val is not None and tsb_val < 10:
        advice = "Advies: easy day (Z1–Z2); readiness is laag."

    # recovery flags can downgrade intensity
    if flags.get("sleep_low") or flags.get("stress_high") or flags.get("hrv_drop"):
        # only downgrade if we planned intensity
        if tsb_val is not None and tsb_val >= 0 and score >= 60:
            advice = downgrade(advice)
        elif score < 60:
            advice = "Advies: herstel/duur Z1–Z2; herstel-signalen zijn minder (HRV/sleep/stress)."

    # a short “why” string (nice in UI/tooltips)
    why_bits = []
    if tsb_val is not None:
        why_bits.append(f"TSB {tsb_val:.1f}")
    if atl is not None and ctl is not None:
        why_bits.append(f"ATL {float(atl):.0f} • CTL {float(ctl):.0f}")
    if flags.get("hrv_drop"):
        why_bits.append("HRV↓")
    if flags.get("sleep_low"):
        why_bits.append("Sleep↓")
    if flags.get("stress_high"):
        why_bits.append("Stress↑")
    why = " • ".join(why_bits) if why_bits else None

    return {
        "score": score,
        "label": label,
        "advice": advice,
        "why": why,

        "components": {
            "hrv": round(hrv_score),
            "sleep": round(sleep_score),
            "stress": round(stress_score),
            "bb": round(bb_score),
            "load": round(load_score),
        },

        "tcl_7d": round(float(tcl_7d), 1),
        "tcl_target_7d": round(float(tcl_target_7d), 1),

        "atl": None if atl is None else round(float(atl), 1),
        "ctl": None if ctl is None else round(float(ctl), 1),
        "tsb": None if tsb is None else round(float(tsb), 1),

        # template compat
        "fuel": None,
        "kcal": None,
        "kcal_target": None,

        "tone": tone,
        "icon": icon,
        "state": state,
    }

def calculate_recovery_gauge(
    latest_hrv=None,
    baseline_hrv=None,
    sleep_score=None,
    avg_stress=None,
    body_battery_high=None,
    resting_hr=None,
    baseline_rhr=None,
    tcl_status=None,
    manual_penalty=0,
):
    # Component scores
    hrv_score = None
    hrv_ratio = None
    if latest_hrv is not None and baseline_hrv not in (None, 0):
        hrv_ratio = latest_hrv / baseline_hrv
        if hrv_ratio >= 1.05:
            hrv_score = 100
        elif hrv_ratio >= 1.00:
            hrv_score = 95
        elif hrv_ratio >= 0.95:
            hrv_score = 80
        elif hrv_ratio >= 0.90:
            hrv_score = 60
        elif hrv_ratio >= 0.85:
            hrv_score = 40
        else:
            hrv_score = 20
    elif latest_hrv is not None:
        # fallback zonder baseline
        if latest_hrv >= 80:
            hrv_score = 95
        elif latest_hrv >= 70:
            hrv_score = 80
        elif latest_hrv >= 60:
            hrv_score = 65
        elif latest_hrv >= 50:
            hrv_score = 45
        else:
            hrv_score = 25

    sleep_score_val = None
    if sleep_score is not None:
        sleep_score_val = clamp(round(sleep_score))

    stress_score = None
    if avg_stress is not None:
        if avg_stress <= 15:
            stress_score = 95
        elif avg_stress <= 20:
            stress_score = 85
        elif avg_stress <= 25:
            stress_score = 70
        elif avg_stress <= 30:
            stress_score = 55
        elif avg_stress <= 35:
            stress_score = 40
        else:
            stress_score = 25

    bb_score = None
    if body_battery_high is not None:
        if body_battery_high >= 90:
            bb_score = 100
        elif body_battery_high >= 80:
            bb_score = 90
        elif body_battery_high >= 70:
            bb_score = 75
        elif body_battery_high >= 60:
            bb_score = 60
        elif body_battery_high >= 50:
            bb_score = 45
        else:
            bb_score = 30

    rhr_score = None
    rhr_delta = None
    if resting_hr is not None and baseline_rhr is not None:
        rhr_delta = resting_hr - baseline_rhr
        if rhr_delta <= 0:
            rhr_score = 95
        elif rhr_delta <= 2:
            rhr_score = 80
        elif rhr_delta <= 4:
            rhr_score = 65
        elif rhr_delta <= 6:
            rhr_score = 45
        else:
            rhr_score = 25
    elif resting_hr is not None:
        if resting_hr <= 50:
            rhr_score = 90
        elif resting_hr <= 55:
            rhr_score = 75
        elif resting_hr <= 60:
            rhr_score = 60
        elif resting_hr <= 65:
            rhr_score = 45
        else:
            rhr_score = 30

    tcl_score = 60
    tcl_state = str(tcl_status).strip().lower() if tcl_status else ""
    if tcl_state:
        if "fresh" in tcl_state:
            tcl_score = 95
        elif "balanced" in tcl_state or "productive" in tcl_state:
            tcl_score = 85
        elif "loaded" in tcl_state:
            tcl_score = 60
        elif "fatigued" in tcl_state or "vermoeid" in tcl_state:
            tcl_score = 35
        elif "very" in tcl_state or "zeer" in tcl_state:
            tcl_score = 20

    parts = [
        (hrv_score, 0.35),
        (sleep_score_val, 0.20),
        (stress_score, 0.20),
        (bb_score, 0.10),
        (rhr_score, 0.10),
        (tcl_score, 0.05),
    ]

    valid = [(v, w) for v, w in parts if v is not None]
    if valid:
        total_w = sum(w for _, w in valid)
        score = round(sum(v * w for v, w in valid) / total_w)
    else:
        score = None

    reasons = []

    if score is not None and manual_penalty:
        score = max(0, score - manual_penalty)
        reasons.append(f"Handmatige herstelpenalty: -{manual_penalty}")

    # Strengere caps
    if score is not None:
    # Handmatige penalty / herstelstatus => nooit train
        if manual_penalty and manual_penalty >= 10:
            score = min(score, 59)
            reasons.append("Herstelmodus actief")

        # Lage HRV component => max walk
        if hrv_score is not None and hrv_score <= 45:
            score = min(score, 49)
            reasons.append("HRV laag")

        # Vermoeid op TCL => max walk
        if tcl_state and ("fatigued" in tcl_state or "vermoeid" in tcl_state):
            score = min(score, 49)
            reasons.append("TCL vermoeid")

        # Zeer vermoeid => rest
        if tcl_state and (("very" in tcl_state and "fatigued" in tcl_state) or ("zeer" in tcl_state and "vermoeid" in tcl_state)):
            score = min(score, 35)
            reasons.append("TCL zeer vermoeid")

        # HRV ratio onder baseline
        if hrv_ratio is not None and hrv_ratio < 0.95:
            score = min(score, 49)
            reasons.append("HRV onder 95% van baseline")

        if hrv_ratio is not None and hrv_ratio < 0.90:
            score = min(score, 39)
            reasons.append("HRV onder 90% van baseline")

        # Verhoogde rusthartslag
        if rhr_delta is not None and rhr_delta >= 5:
            score = min(score, 49)
            reasons.append("Rusthartslag verhoogd")

        # Slechte slaap
        if sleep_score is not None and sleep_score < 65:
            score = min(score, 59)
            reasons.append("Slaapscore laag")

        # Hoge stress
        if avg_stress is not None and avg_stress > 30:
            score = min(score, 49)
            reasons.append("Stress verhoogd")

    if score is None:
        state = "unknown"
        label = "Unknown"
    elif score >= 75:
        state = "train"
        label = "Train"
    elif score >= 60:
        state = "easy"
        label = "Easy"
    elif score >= 40:
        state = "walk"
        label = "Walk"
    else:
        state = "rest"
        label = "Rest"

    return {
        "score": score,
        "state": state,
        "label": label,
        "reasons": reasons,
        "components": {
            "hrv": hrv_score,
            "sleep": sleep_score_val,
            "stress": stress_score,
            "body_battery": bb_score,
            "resting_hr": rhr_score,
            "tcl": tcl_score,
        }
    }


@app.context_processor
def inject_globals():
    # user: neem jouw globale USER_NAME (niet app.config)
    user_name = USER_NAME

    # readiness: lees uit cache (super snel)
    readiness_hdr = None
    try:
        with get_conn() as conn:
            readiness_hdr = cache_get(conn, "readiness_header", default=None)
    except Exception:
        readiness_hdr = None

    return {
        "USER_NAME": user_name,
        "READINESS_HEADER": readiness_hdr,  # dict of None
    }

def get_tcl_7d(conn):
    """
    Som van TCL over de laatste 7 dagen (incl. vandaag), op basis van start_time_local.
    """
    row = conn.execute("""
        SELECT COALESCE(SUM(tcl), 0) AS tcl_7d
        FROM strava_activities
        WHERE date(start_time_local) >= date('now', '-6 day')
    """).fetchone()
    return float(row["tcl_7d"]) if row and row["tcl_7d"] is not None else 0.0

def get_latest_activities(conn, limit=25, activity_type="all"):
    if activity_type != "all":
        rows = conn.execute("""
            SELECT *
            FROM strava_activities
            WHERE activity_type = ?
            ORDER BY start_time_local DESC
            LIMIT ?
        """, (activity_type, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT *
            FROM strava_activities
            ORDER BY start_time_local DESC
            LIMIT ?
        """, (limit,)).fetchall()

    return [dict(r) for r in rows]


def _parse_yyyy_mm_dd(s: str):
    return datetime.strptime(s, "%Y-%m-%d").date()

def _ema(loads, tau_days: int):
    if not loads:
        return []

    k = 1.0 / float(tau_days)

    seed_n = min(len(loads), tau_days)
    prev = sum(float(x) for x in loads[:seed_n]) / float(seed_n)

    out = [prev]
    for x in loads[1:]:
        x = float(x)
        prev = prev + (x - prev) * k
        out.append(prev)

    return out

def activity_load(relative_effort=None, weighted_power=None, moving_time_s=None, ftp=None, suffer_score=None):
    # 1) Prefer Relative Effort if available
    if relative_effort is not None:
        try:
            return float(relative_effort)
        except (TypeError, ValueError):
            pass

    # 2) Fallback: TSS-like from weighted power + FTP
    try:
        if weighted_power is not None and moving_time_s is not None and ftp and float(ftp) > 0:
            wp = float(weighted_power)
            dur_h = float(moving_time_s) / 3600.0
            if_val = wp / float(ftp)
            return dur_h * (if_val ** 2) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        pass

    # 3) Fallback: suffer score
    if suffer_score is not None:
        try:
            return float(suffer_score)
        except (TypeError, ValueError):
            pass

    return 0.0

def get_training_load_today(conn, days_back: int = 365):
    """
    Geeft ATL/CTL/Form op basis van dagelijkse trainingsload.
    Prioriteit activiteit-load:
    1. relative_effort
    2. TSS-like uit weighted power + FTP
    3. suffer_score

    Geeft ook 'start-of-day' form terug, zodat dashboard beter matcht met
    hoe Strava/TrainingPeaks freshness vaak voelt vóór de training van vandaag.
    """

    ftp = STRAVA_FTP if 'STRAVA_FTP' in globals() else 0

    rows = conn.execute("""
        SELECT
            date(start_time_local) AS day,
            raw_json,
            moving_time_s,
            suffer_score
        FROM strava_activities
        WHERE start_time_local IS NOT NULL
        ORDER BY day ASC
    """).fetchall()

    if not rows:
        return {
            "atl": None,
            "ctl": None,
            "tsb": None,
            "tcl_today": None,
            "tcl_yesterday": None,
            "atl_start": None,
            "ctl_start": None,
            "tsb_start": None,
        }

    daily = {}

    for row in rows:
        day = row["day"]
        raw = {}
        try:
            raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
        except Exception:
            raw = {}

        # Try common Strava keys
        relative_effort = raw.get("relative_effort")
        weighted_power = (
            raw.get("weighted_average_watts")
            or raw.get("weighted_power")
        )

        load = activity_load(
            relative_effort=relative_effort,
            weighted_power=weighted_power,
            moving_time_s=row["moving_time_s"],
            ftp=ftp,
            suffer_score=row["suffer_score"],
        )

        daily[day] = daily.get(day, 0.0) + load

    days_sorted = sorted(daily.keys())
    min_day = _parse_yyyy_mm_dd(days_sorted[0])
    max_day = _parse_yyyy_mm_dd(days_sorted[-1])

    end_day = max_day
    start_day = end_day - timedelta(days=days_back - 1)

    xs = []
    loads = []
    d = start_day
    while d <= end_day:
        ds = d.isoformat()
        xs.append(ds)
        loads.append(float(daily.get(ds, 0.0)))
        d += timedelta(days=1)

    atl_series = _ema(loads, 7)
    ctl_series = _ema(loads, 42)
    tsb_series = [c - a for c, a in zip(ctl_series, atl_series)]

    if not atl_series or not ctl_series or not tsb_series:
        return {
            "atl": None,
            "ctl": None,
            "tsb": None,
            "tcl_today": None,
            "tcl_yesterday": None,
            "atl_start": None,
            "ctl_start": None,
            "tsb_start": None,
        }

    tcl_today = loads[-1] if loads else None
    tcl_yesterday = loads[-2] if len(loads) >= 2 else None

    # End-of-day values (na de load van vandaag)
    atl_end = atl_series[-1]
    ctl_end = ctl_series[-1]
    tsb_end = tsb_series[-1]

    # Start-of-day values (vóór de load van vandaag)
    if len(atl_series) >= 2 and len(ctl_series) >= 2:
        atl_start = atl_series[-2]
        ctl_start = ctl_series[-2]
        tsb_start = ctl_start - atl_start
    else:
        atl_start = atl_end
        ctl_start = ctl_end
        tsb_start = tsb_end

    # Start-of-day values van gisteren
    if len(atl_series) >= 3 and len(ctl_series) >= 3:
        atl_start_prev = atl_series[-3]
        ctl_start_prev = ctl_series[-3]
        tsb_start_prev = ctl_start_prev - atl_start_prev
    else:
        atl_start_prev = atl_start
        ctl_start_prev = ctl_start
        tsb_start_prev = tsb_start

    return {
        "atl": round(float(atl_end), 1),
        "ctl": round(float(ctl_end), 1),
        "tsb": round(float(tsb_end), 1),
        "tcl_today": round(float(tcl_today), 1) if tcl_today is not None else None,
        "tcl_yesterday": round(float(tcl_yesterday), 1) if tcl_yesterday is not None else None,

        "atl_start": round(float(atl_start), 1) if atl_start is not None else None,
        "ctl_start": round(float(ctl_start), 1) if ctl_start is not None else None,
        "tsb_start": round(float(tsb_start), 1) if tsb_start is not None else None,

        "atl_start_prev": round(float(atl_start_prev), 1) if atl_start_prev is not None else None,
        "ctl_start_prev": round(float(ctl_start_prev), 1) if ctl_start_prev is not None else None,
        "tsb_start_prev": round(float(tsb_start_prev), 1) if tsb_start_prev is not None else None,
    }

def get_training_load_series(conn, days_back: int = 365):
    ftp = STRAVA_FTP if 'STRAVA_FTP' in globals() else 0

    rows = conn.execute("""
        SELECT
            date(start_time_local) AS day,
            raw_json,
            moving_time_s,
            suffer_score
        FROM strava_activities
        WHERE start_time_local IS NOT NULL
        ORDER BY day ASC
    """).fetchall()

    if not rows:
        return []

    daily = {}

    for row in rows:
        day = row["day"]
        raw = {}
        try:
            raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
        except Exception:
            raw = {}

        relative_effort = raw.get("relative_effort")
        weighted_power = (
            raw.get("weighted_average_watts")
            or raw.get("weighted_power")
        )

        load = activity_load(
            relative_effort=relative_effort,
            weighted_power=weighted_power,
            moving_time_s=row["moving_time_s"],
            ftp=ftp,
            suffer_score=row["suffer_score"],
        )

        daily[day] = daily.get(day, 0.0) + load

    days_sorted = sorted(daily.keys())
    min_day = _parse_yyyy_mm_dd(days_sorted[0])
    max_day = _parse_yyyy_mm_dd(days_sorted[-1])

    end_day = max_day
    start_day = end_day - timedelta(days=days_back - 1)

    xs = []
    loads = []
    d = start_day
    while d <= end_day:
        ds = d.isoformat()
        xs.append(ds)
        loads.append(float(daily.get(ds, 0.0)))
        d += timedelta(days=1)

    atl_series = _ema(loads, 7)
    ctl_series = _ema(loads, 42)

    CTL_SCALE = 1.60
    ATL_SCALE = 0.84

    out = []
    for i, day in enumerate(xs):
        ctl = float(ctl_series[i]) * CTL_SCALE
        atl = float(atl_series[i]) * ATL_SCALE
        tsb = ctl - atl

        out.append({
            "day": day,
            "fitness": round(ctl, 1),
            "fatigue": round(atl, 1),
            "form": round(tsb, 1),
            "load": round(loads[i], 1),
        })

    return out

def get_recovery_flags(conn):
    """
    Kleine ‘recovery sanity checks’ op basis van daily_metrics:
    - HRV drop (laatste 3 vs vorige 14)
    - sleep low
    - stress high
    """
    rows = conn.execute("""
        SELECT day, hrv_rmssd, sleep_score, avg_stress
        FROM daily_metrics
        ORDER BY day DESC
        LIMIT 20
    """).fetchall()

    data = [dict(r) for r in rows]
    if not data:
        return {"hrv_drop": False, "sleep_low": False, "stress_high": False}

    def avg(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    hrv_last3 = avg([d.get("hrv_rmssd") for d in data[:3]])
    hrv_prev14 = avg([d.get("hrv_rmssd") for d in data[3:17]])

    sleep_today = data[0].get("sleep_score")
    stress_today = data[0].get("avg_stress")

    hrv_drop = (hrv_last3 is not None and hrv_prev14 is not None and hrv_last3 < (hrv_prev14 - 5))
    sleep_low = (sleep_today is not None and sleep_today < 60)
    stress_high = (stress_today is not None and stress_today > 25)

    return {"hrv_drop": hrv_drop, "sleep_low": sleep_low, "stress_high": stress_high}    

def _to_float(x):
    if x is None:
        return None
    s = str(x).strip()
    if s == "":
        return None
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def get_latest_fitatu_week_summary(
    conn,
    weekly_kcal_target=17500,
    protein_target_g_per_day=175,
    fat_target_g_per_day=100,
    carbs_target_g_per_day=225,
    fiber_target_g_per_day=30,
    salt_target_g_per_day=6,
):
    """
    Maakt een korte tekstsamenvatting van de laatste volledige Fitatu-week.
    """
    row = conn.execute("""
        SELECT
            date(day, '-' || ((strftime('%w',day) + 6) % 7) || ' days') AS week_start,
            SUM(calories)  AS kcal_total,
            SUM(protein_g) AS protein_g,
            SUM(carbs_g)   AS carbs_g,
            SUM(fat_g)     AS fat_g,
            SUM(fiber_g)   AS fiber_g,
            SUM(COALESCE(salt_g, 0)) AS salt_g,
            COUNT(*) AS days_logged
        FROM fitatu_daily
        WHERE day IS NOT NULL
        GROUP BY week_start
        ORDER BY week_start DESC
        LIMIT 1
    """).fetchone()

    if not row:
        return None

    row = dict(row)

    week_start = row["week_start"]
    if not week_start:
        return None

    start_dt = datetime.strptime(week_start, "%Y-%m-%d").date()
    end_dt = start_dt + timedelta(days=6)

    kcal_total = float(row.get("kcal_total") or 0)
    protein_g = float(row.get("protein_g") or 0)
    carbs_g = float(row.get("carbs_g") or 0)
    fat_g = float(row.get("fat_g") or 0)
    fiber_g = float(row.get("fiber_g") or 0)
    salt_g = float(row.get("salt_g") or 0)
    days_logged = int(row.get("days_logged") or 0)

    protein_target_week = protein_target_g_per_day * 7
    fat_target_week = fat_target_g_per_day * 7
    carbs_target_week = carbs_target_g_per_day * 7
    fiber_target_week = fiber_target_g_per_day * 7
    salt_target_week = salt_target_g_per_day * 7

    kcal_diff = kcal_total - weekly_kcal_target
    protein_diff = protein_g - protein_target_week
    carbs_diff = carbs_g - carbs_target_week
    fat_diff = fat_g - fat_target_week
    fiber_diff = fiber_g - fiber_target_week
    salt_diff = salt_g - salt_target_week

    def fmt_diff(value, unit="", decimals=0):
        if value is None:
            return "—"
        if decimals == 0:
            value_txt = f"{abs(round(value))}"
        else:
            value_txt = f"{abs(round(value, decimals))}"
        sign = "+" if value > 0 else "-" if value < 0 else "±"
        return f"{sign}{value_txt}{unit}"


    def compare(actual, target, lower_is_better=False, margin=0.05):
        if target <= 0:
            return "onbekend"

        ratio = actual / target

        if lower_is_better:
            if ratio <= 1.00:
                return "goed"
            elif ratio <= 1.10:
                return "licht te hoog"
            else:
                return "duidelijk te hoog"

        if ratio < 0.95:
            return "onder doel"
        elif ratio <= 1.05:
            return "rond doel"
        else:
            return "boven doel"

    kcal_status = compare(kcal_total, weekly_kcal_target)
    protein_status = compare(protein_g, protein_target_week)
    fat_status = compare(fat_g, fat_target_week, lower_is_better=True)
    carbs_status = compare(carbs_g, carbs_target_week)
    fiber_status = compare(fiber_g, fiber_target_week)
    salt_status = compare(salt_g, salt_target_week, lower_is_better=True)

    bullets = []

    if kcal_status == "boven doel":
        bullets.append(f"totaal calorieën overschreden ({fmt_diff(kcal_diff, ' kcal')})")
    elif kcal_status == "onder doel":
        bullets.append(f"totaal calorieën onder doel ({fmt_diff(kcal_diff, ' kcal')})")
    else:
        bullets.append("calorieën rond doel")

    if fat_status == "licht te hoog":
        bullets.append(f"vetten licht te hoog ({fmt_diff(fat_diff, ' g')})")
    elif fat_status == "duidelijk te hoog":
        bullets.append(f"vetten duidelijk te hoog ({fmt_diff(fat_diff, ' g')})")
    elif fat_status == "goed":
        bullets.append("vetten binnen doel")

    if protein_status == "onder doel":
        bullets.append(f"eiwit onder doel ({fmt_diff(protein_diff, ' g')})")
    elif protein_status == "rond doel":
        bullets.append("eiwit rond doel")
    else:
        bullets.append(f"eiwit boven doel ({fmt_diff(protein_diff, ' g')})")

    if carbs_status == "onder doel":
        bullets.append(f"koolhydraten onder doel ({fmt_diff(carbs_diff, ' g')})")
    elif carbs_status == "rond doel":
        bullets.append("koolhydraten rond doel")
    else:
        bullets.append(f"koolhydraten boven doel ({fmt_diff(carbs_diff, ' g')})")

    if fiber_status == "onder doel":
        bullets.append(f"vezels onder doel ({fmt_diff(fiber_diff, ' g')})")
    elif fiber_status == "rond doel":
        bullets.append("vezels goed")
    else:
        bullets.append(f"vezels ruim voldoende ({fmt_diff(fiber_diff, ' g')})")

    if salt_g > 0:
        if salt_status == "licht te hoog":
            bullets.append(f"zout licht te hoog ({fmt_diff(salt_diff, ' g', 1)})")
        elif salt_status == "duidelijk te hoog":
            bullets.append(f"zout duidelijk te hoog ({fmt_diff(salt_diff, ' g', 1)})")
        else:
            bullets.append("zout binnen doel")

    summary_text = f"Week {start_dt.strftime('%d-%m')} t/m {end_dt.strftime('%d-%m')}: " + ", ".join(bullets) + "."

    return {
        "week_start": week_start,
        "week_end": end_dt.isoformat(),
        "days_logged": days_logged,
        "kcal_total": round(kcal_total),
        "protein_g": round(protein_g),
        "carbs_g": round(carbs_g),
        "fat_g": round(fat_g),
        "fiber_g": round(fiber_g),
        "salt_g": round(salt_g, 1),

        "kcal_diff": round(kcal_diff),
        "protein_diff": round(protein_diff),
        "carbs_diff": round(carbs_diff),
        "fat_diff": round(fat_diff),
        "fiber_diff": round(fiber_diff),
        "salt_diff": round(salt_diff, 1),

        "summary_text": summary_text,
    }

def import_fitatu_meal_csv(conn, file_storage) -> dict:
    """
    Fitatu maaltijd CSV:
    - meerdere regels per dag
    - aggregeert naar 1 rij per dag in fitatu_daily
    """

    raw = file_storage.read()
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    COL_DAY = "Datum"
    COL_KCAL = "calorieën (kcal)"
    COL_P = "Eiwitten (g)"
    COL_C = "Koolhydraten (g)"
    COL_F = "Vetten (g)"
    COL_FIBER = "Vezels (g)"
    COL_SALT = "Sól (g)"   # pas aan als jouw CSV een andere naam gebruikt

    per_day = {}
    bad_rows = 0

    for row in reader:
        day = (row.get(COL_DAY) or "").strip()
        if not day:
            continue

        kcal = _to_float(row.get(COL_KCAL))
        p = _to_float(row.get(COL_P))
        c = _to_float(row.get(COL_C))
        f = _to_float(row.get(COL_F))
        fib = _to_float(row.get(COL_FIBER))
        salt = _to_float(row.get(COL_SALT))

        if kcal is None and p is None and c is None and f is None and fib is None and salt is None:
            bad_rows += 1
            continue

        d = per_day.setdefault(day, {
            "calories": 0.0,
            "protein_g": 0.0,
            "carbs_g": 0.0,
            "fat_g": 0.0,
            "fiber_g": 0.0,
            "salt_g": 0.0,
        })

        d["calories"] += kcal or 0.0
        d["protein_g"] += p or 0.0
        d["carbs_g"] += c or 0.0
        d["fat_g"] += f or 0.0
        d["fiber_g"] += fib or 0.0
        d["salt_g"] += salt or 0.0

    upserts = 0
    for day, t in per_day.items():
        conn.execute("""
            INSERT INTO fitatu_daily(day, calories, protein_g, carbs_g, fat_g, fiber_g, salt_g)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
              calories=excluded.calories,
              protein_g=excluded.protein_g,
              carbs_g=excluded.carbs_g,
              fat_g=excluded.fat_g,
              fiber_g=excluded.fiber_g,
              salt_g=excluded.salt_g
        """, (
            day,
            t["calories"],
            t["protein_g"],
            t["carbs_g"],
            t["fat_g"],
            t["fiber_g"],
            t["salt_g"],
        ))
        upserts += 1

    conn.commit()

    days_sorted = sorted(per_day.keys())
    return {
        "days": len(per_day),
        "min_day": days_sorted[0] if days_sorted else None,
        "max_day": days_sorted[-1] if days_sorted else None,
        "bad_rows": bad_rows,
        "upserts": upserts,
    }

def get_latest_strava_readiness(conn):
    row = conn.execute("""
        SELECT
            day,
            daily_load,
            fatigue,
            fitness,
            form,
            readiness_score,
            recovery_gauge,
            source
        FROM readiness_daily
        ORDER BY day DESC
        LIMIT 1
    """).fetchone()

    if not row:
        return None

    row = dict(row)

    readiness_score_val = int(row.get("readiness_score") or 0)
    recovery_gauge_val = int(row.get("recovery_gauge") or 0)
    form_val = float(row.get("form") or 0)

    if readiness_score_val >= 80:
        label = "Zeer goed"
        state = "train"
    elif readiness_score_val >= 65:
        label = "Goed"
        state = "easy"
    elif readiness_score_val >= 50:
        label = "Redelijk"
        state = "easy"
    elif readiness_score_val >= 35:
        label = "Vermoeid"
        state = "walk"
    else:
        label = "Laag"
        state = "rest"

    if form_val >= 10:
        form_label = "Fris"
    elif form_val >= 0:
        form_label = "In balans"
    elif form_val >= -10:
        form_label = "Belast"
    else:
        form_label = "Vermoeid"

    row["label"] = label
    row["state"] = state
    row["form_label"] = form_label
    row["readiness_score"] = readiness_score_val
    row["recovery_gauge"] = recovery_gauge_val
    row["form"] = round(form_val, 1)
    row["fitness"] = round(float(row.get("fitness") or 0), 1)
    row["fatigue"] = round(float(row.get("fatigue") or 0), 1)
    row["daily_load"] = round(float(row.get("daily_load") or 0), 1)

    return row

@app.route("/api/strava_status_trend")
def api_strava_status_trend():
    days = int(request.args.get("days", 7))
    tl = get_training_load_series(get_conn(), days_back=max(days, 365))

    if not tl:
        return jsonify([])

    # Alleen laatste X dagen tonen
    out = tl[-days:]
    return jsonify(out)

def get_live_strava_status(conn):
    tl = get_training_load_today(conn, days_back=365)

    ctl_raw = tl.get("ctl_start")
    atl_raw = tl.get("atl_start")
    ctl_raw_prev = tl.get("ctl_start_prev")
    atl_raw_prev = tl.get("atl_start_prev")
    tcl_today = tl.get("tcl_today")

    if ctl_raw is None or atl_raw is None:
        return None

    CTL_SCALE = 1.60
    ATL_SCALE = 0.84

    fitness = float(ctl_raw) * CTL_SCALE
    fatigue = float(atl_raw) * ATL_SCALE
    form = fitness - fatigue

    if ctl_raw_prev is not None and atl_raw_prev is not None:
        fitness_prev = float(ctl_raw_prev) * CTL_SCALE
        fatigue_prev = float(atl_raw_prev) * ATL_SCALE
        form_prev = fitness_prev - fatigue_prev
    else:
        fitness_prev = fitness
        fatigue_prev = fatigue
        form_prev = form

    fitness_delta = fitness - fitness_prev
    fatigue_delta = fatigue - fatigue_prev
    form_delta = form - form_prev

    if form >= 20:
        form_label = "Fresh"
        form_state = "good"
    elif form >= 8:
        form_label = "Ready"
        form_state = "good"
    elif form >= -5:
        form_label = "Balanced"
        form_state = "ok"
    elif form >= -15:
        form_label = "Loaded"
        form_state = "ok"
    else:
        form_label = "Heavy load"
        form_state = "bad"

    return {
        "fitness": round(fitness, 1),
        "fatigue": round(fatigue, 1),
        "form": round(form, 1),
        "daily_load": None if tcl_today is None else round(float(tcl_today), 1),
        "label": form_label,
        "state": form_state,

        "fitness_delta": round(fitness_delta, 1),
        "fatigue_delta": round(fatigue_delta, 1),
        "form_delta": round(form_delta, 1),
    }

def get_training_advice(form, fitness, fatigue):
    form = float(form or 0)
    fitness = float(fitness or 0)
    fatigue = float(fatigue or 0)

    # Hoofdlogica: Form is leidend
    if form >= 20 and fatigue > 70:
        return {
            "code": "train_easy",
            "label": "Rustig",
            "summary": "Je bent fris, maar belasting is al hoog.",
            "details": "Kies liever een rustige duurtraining i.p.v. maximale intensiteit.",
            "tone": "ok",
            "score": 55,
    }

    if form >= 20:
        return {
            "code": "train_hard",
            "label": "Kwaliteit",
            "summary": "Goede dag voor een stevige training.",
            "details": "Je vorm is hoog. Geschikt voor intervallen, threshold of een zwaardere sessie.",
            "tone": "good",
            "score": 85,
        }

    if form >= 8:
        return {
            "code": "train_normal",
            "label": "Normaal",
            "summary": "Prima dag om normaal te trainen.",
            "details": "Goede balans tussen fitheid en belasting. Duur, tempo of kracht past hier goed.",
            "tone": "good",
            "score": 70,
        }

    if form >= -5:
        return {
            "code": "train_easy",
            "label": "Rustig",
            "summary": "Hou het gecontroleerd vandaag.",
            "details": "Geschikt voor rustige duur, techniek of een lichtere sessie.",
            "tone": "ok",
            "score": 50,
        }

    if form >= -15:
        return {
            "code": "recovery",
            "label": "Herstel",
            "summary": "Je trainingsbelasting loopt op.",
            "details": "Kies herstelduur, wandelen of mobiliteit.",
            "tone": "ok",
            "score": 35,
        }

    return {
        "code": "rest",
        "label": "Rust",
        "summary": "Je systeem vraagt om herstel.",
        "details": "Neem rust of doe alleen mobiliteit / heel lichte beweging.",
        "tone": "ok",
        "score": 20,
    }

def tsb_to_score(tsb):
    tsb = max(-30, min(30, float(tsb or 0)))
    return int((tsb + 30) / 60 * 100)

def tsb_to_score(tsb):
    tsb = max(-30, min(30, float(tsb or 0)))
    return int((tsb + 30) / 60 * 100)

from datetime import datetime, timedelta

def get_hume_weekly_summary(conn):
    rows = conn.execute("""
        SELECT day, weight_kg, body_fat_pct, muscle_mass_kg, lean_mass_kg, visceral_fat_index
        FROM hume_body
        WHERE day IS NOT NULL
        ORDER BY day DESC
        LIMIT 21
    """).fetchall()

    rows = [dict(r) for r in rows]
    if not rows:
        return None

    def avg(values):
        values = [v for v in values if v is not None]
        return round(sum(values) / len(values), 2) if values else None

    this_week = rows[:7]
    prev_week = rows[7:14]

    if not this_week or not prev_week:
        return None

    current = {
        "weight": avg([r["weight_kg"] for r in this_week]),
        "bf": avg([r["body_fat_pct"] for r in this_week]),
        "mm": avg([r["muscle_mass_kg"] for r in this_week]),
        "lm": avg([r["lean_mass_kg"] for r in this_week]),
        "vf": avg([r["visceral_fat_index"] for r in this_week]),
    }

    previous = {
        "weight": avg([r["weight_kg"] for r in prev_week]),
        "bf": avg([r["body_fat_pct"] for r in prev_week]),
        "mm": avg([r["muscle_mass_kg"] for r in prev_week]),
        "lm": avg([r["lean_mass_kg"] for r in prev_week]),
        "vf": avg([r["visceral_fat_index"] for r in prev_week]),
    }

    def delta(cur, prev):
        if cur is None or prev is None:
            return None
        return round(cur - prev, 2)

    return {
        "weight_delta": delta(current["weight"], previous["weight"]),
        "bf_delta": delta(current["bf"], previous["bf"]),
        "mm_delta": delta(current["mm"], previous["mm"]),
        "lm_delta": delta(current["lm"], previous["lm"]),
        "vf_delta": delta(current["vf"], previous["vf"]),
        "current": current,
        "previous": previous,
    }


@app.route("/")
def home():
    days = int(request.args.get("days", 30))

    with get_conn() as conn:
        latest_row = conn.execute(
            "SELECT * FROM daily_metrics ORDER BY day DESC LIMIT 1"
        ).fetchone()
        latest = row_to_dict(latest_row)

        tl = get_training_load_today(conn)
        strava_status = get_live_strava_status(conn)

        latest_hume_row = conn.execute(
            "SELECT * FROM hume_body ORDER BY day DESC LIMIT 1"
        ).fetchone()
        latest_hume = row_to_dict(latest_hume_row)

        if latest_hume:
            for key in [
                "weight_kg",
                "body_fat_pct",
                "muscle_mass_kg",
                "lean_mass_kg",
                "visceral_fat_index",
                "body_water_pct",
                "body_cell_mass_kg",
            ]:
                latest_hume[key] = to_float(latest_hume.get(key))

        if strava_status:
            training_advice = get_training_advice(
                form=strava_status["form"],
                fitness=strava_status["fitness"],
                fatigue=strava_status["fatigue"],
            )
        else:
            training_advice = {
                "code": "unknown",
                "label": "Onbekend",
                "summary": "Nog geen Strava load-data beschikbaar.",
                "details": "Zodra er load-data is, verschijnt hier een advies.",
                "tone": "ok",
                "score": None,
            }

    safe_hume = latest_hume or {}

    deltas = calc_body_deltas(
        safe_hume,
        target_weight=90,
        target_lean=70,
        target_bf=17.5,
        height_m=USER_HEIGHT,
    )

    delta_weight = deltas.get("delta_weight")
    delta_lean = deltas.get("delta_lean")
    delta_bf = deltas.get("delta_bf")
    lbmi = deltas.get("lbmi")
    delta_lbmi = deltas.get("delta_lbmi")
    
    lbmi_display = round(lbmi, 2) if lbmi is not None else None
    delta_lbmi_display = round(delta_lbmi, 2) if delta_lbmi is not None else None

    home_status = {
        "advice": {
            "label": training_advice["label"],
            "state": training_advice["tone"],
            "score": training_advice.get("score"),
        },
        "form": {
            "label": strava_status["label"] if strava_status else "-",
            "state": strava_status["state"] if strava_status else "unknown",
            "score": strava_status["form"] if strava_status else None,
        },
        "fitness": {
            "label": "Fitness",
            "state": "good",
            "score": strava_status["fitness"] if strava_status else None,
        },
        "fatigue": {
            "label": "Vermoeidheid",
            "state": "ok",
            "score": strava_status["fatigue"] if strava_status else None,
        },
    }

    cache_set(conn, "readiness_header", {
            "score": training_advice.get("score"),
            "label": training_advice.get("label"),
            "icon": None,
            "tone": training_advice.get("tone"),
            "state": training_advice.get("code"),
            "why": training_advice.get("summary"),
    })

    return render_template(
        "home.html",
        active_page="home",
        days=days,
        latest_hume=latest_hume,
        delta_weight=delta_weight,
        delta_lean=delta_lean,
        delta_bf=delta_bf,
        lbmi_display=lbmi_display,
        delta_lbmi_display=delta_lbmi_display,
        home_status=home_status,
        training_advice=training_advice,
        advice_score=training_advice.get("score"),
        strava_status=strava_status,
        USER_NAME=USER_NAME,
        USER_HEIGHT=USER_HEIGHT,
    )
    

@app.route("/hume/charts")
def hume_charts():
    days = int(request.args.get("days", 180))
    with get_conn() as conn:
        hume_summary = get_hume_weekly_summary(conn)
    
    return render_template("hume_charts.html", active_page="hume_charts", days=days, USER_HEIGHT=USER_HEIGHT, hume_summary=hume_summary,)

@app.route("/trends")
def trends():
    days = int(request.args.get("days", 60))
    return render_template("trends.html", active_page="trends", days=days)

@app.route("/training-load")
def training_load_page():
    days = int(request.args.get("days", 30))
    return render_template(
        "training_load.html",
        active_page="training_load",
        days=days
    )

@app.route("/activities")
def activities_page():
    act_limit = int(request.args.get("acts", 50))
    activity_type = request.args.get("type", "all")

    with get_conn() as conn:
        if activity_type != "all":
            latest_activities = conn.execute("""
                SELECT
                    strava_activity_id AS activity_id,
                    start_time_local,
                    type AS activity_type,
                    name AS activity_name,
                    distance_m,
                    moving_time_s AS duration_s,
                    NULL AS avg_hr,
                    NULL AS max_hr,
                    NULL AS avg_power,
                    NULL AS training_effect,
                    NULL AS vo2max_value,
                    suffer_score,
                    tcl
                FROM strava_activities
                WHERE type = ?
                ORDER BY start_time_local DESC
                LIMIT ?
            """, (activity_type, act_limit)).fetchall()
        else:
            latest_activities = conn.execute("""
                SELECT
                    strava_activity_id AS activity_id,
                    start_time_local,
                    type AS activity_type,
                    name AS activity_name,
                    distance_m,
                    moving_time_s AS duration_s,
                    NULL AS avg_hr,
                    NULL AS max_hr,
                    NULL AS avg_power,
                    NULL AS training_effect,
                    NULL AS vo2max_value,
                    suffer_score,
                    tcl
                FROM strava_activities
                ORDER BY start_time_local DESC
                LIMIT ?
            """, (act_limit,)).fetchall()

        types = [
            r["t"] for r in conn.execute("""
                SELECT DISTINCT type AS t
                FROM strava_activities
                WHERE type IS NOT NULL AND type != ''
                ORDER BY t
            """).fetchall()
        ]

    return render_template(
        "activities.html",
        active_page="activities",
        latest_activities=[dict(r) for r in latest_activities],
        types=types,
        act_limit=act_limit,
        activity_type=activity_type,
    )

@app.route("/api/activities")
def api_activities():
    limit = int(request.args.get("acts", request.args.get("limit", 25)))
    activity_type = request.args.get("type", "all")

    with get_conn() as conn:
        if activity_type != "all":
            rows = conn.execute("""
                SELECT *
                FROM strava_activities
                WHERE type = ?
                ORDER BY start_time_local DESC
                LIMIT ?
            """, (activity_type, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT *
                FROM strava_activities
                ORDER BY start_time_local DESC
                LIMIT ?
            """, (limit,)).fetchall()

    return jsonify([dict(r) for r in rows])

@app.route("/api/daily_metrics")
def api_daily():
    days = int(request.args.get("days", 30))
    conn = get_conn()
    rows = conn.execute("""
        SELECT *
        FROM daily_metrics
        ORDER BY day DESC
        LIMIT ?
    """, (days,)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/hume")
def api_hume():
    days = int(request.args.get("days", 180))
    conn = get_conn()
    rows = conn.execute("""
        SELECT *
        FROM hume_body
        ORDER BY day DESC
        LIMIT ?
    """, (days,)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/hume_body")
def api_hume_body():
    return api_hume()  # alias

@app.route("/api/fitatu_weekly")
def api_fitatu_weekly():
    conn = get_conn()

    # optioneel: days param, maar we limiteren toch op weken
    # days = int(request.args.get("days", 365))

    rows = conn.execute("""
        SELECT
          date(day, '-' || ((strftime('%w',day) + 6) % 7) || ' days') AS week_start,
          SUM(calories)  AS kcal_total,
          SUM(protein_g) AS protein_g,
          SUM(carbs_g)   AS carbs_g,
          SUM(fat_g)     AS fat_g
        FROM fitatu_daily
        WHERE day IS NOT NULL
        GROUP BY week_start
        ORDER BY week_start DESC
        LIMIT 104
    """).fetchall()

    out = []
    for r in rows:
        d = dict(r)

        # macro kcal (Fitatu grams -> kcal)
        p_g = d.get("protein_g") or 0
        c_g = d.get("carbs_g") or 0
        f_g = d.get("fat_g") or 0

        d["protein_kcal"] = p_g * 4
        d["carbs_kcal"] = c_g * 4
        d["fat_kcal"] = f_g * 9

        # Zorg dat kcal_total niet None is
        d["kcal_total"] = d.get("kcal_total") or 0

        out.append(d)

    return jsonify(out)


@app.route("/api/activity_types")
def api_activity_types():
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT DISTINCT type AS t
            FROM strava_activities
            WHERE type IS NOT NULL AND type != ''
            ORDER BY t
        """).fetchall()

    return jsonify([r["t"] for r in rows])


@app.route("/collectors")
def collectors():
    return render_template("collectors.html")


@app.route("/collectors/run", methods=["POST"])
def collectors_run():
    action = request.form["action"]

    if action == "collector_all":
        sync_all()
    elif action == "collector_garmin":
        sync_garmin()
    elif action == "collector_strava":
        sync_strava()

    flash("Collector gestart", "success")
    return redirect("/collectors")


@app.route("/api/pro_weekly_analysis")
def api_pro_weekly_analysis():
    days = int(request.args.get("days", 180))
    conn = get_conn()

    # per-week aggregaties
    dm = conn.execute("""
        SELECT
          date(day, '-' || ((strftime('%w',day) + 6) % 7) || ' days') AS week_start,
          AVG(hrv_rmssd) AS hrv_avg
        FROM daily_metrics
        WHERE day IS NOT NULL
        GROUP BY week_start
    """).fetchall()

    ft = conn.execute("""
        SELECT
          date(day, '-' || ((strftime('%w',day) + 6) % 7) || ' days') AS week_start,
          SUM(calories) AS kcal_week,
          COUNT(*)      AS days_logged
        FROM fitatu_daily
        WHERE day IS NOT NULL
        GROUP BY week_start
    """).fetchall()

    hb = conn.execute("""
        SELECT
          date(day, '-' || ((strftime('%w',day) + 6) % 7) || ' days') AS week_start,
          AVG(weight_kg) AS weight_avg
        FROM hume_body
        WHERE day IS NOT NULL
        GROUP BY week_start
    """).fetchall()

    dm_map = {r["week_start"]: dict(r) for r in dm}
    ft_map = {r["week_start"]: dict(r) for r in ft}
    hb_map = {r["week_start"]: dict(r) for r in hb}

    weeks = sorted(set(dm_map) | set(ft_map) | set(hb_map), reverse=True)

    out = []
    prev_weight = None

    for w in weeks:
        # filter op days (ongeveer): neem max ~days/7 weken
        # (simpel: we limiteren na de merge)
        row = {"week_start": w}

        row.update(dm_map.get(w, {}))
        row.update(ft_map.get(w, {}))
        row.update(hb_map.get(w, {}))

        kcal_week = row.get("kcal_week")
        days_logged = row.get("days_logged") or 0

        # JS verwacht kcal_day_avg
        if kcal_week is not None:
            row["kcal_day_avg"] = (kcal_week / days_logged) if days_logged > 0 else (kcal_week / 7.0)
        else:
            row["kcal_day_avg"] = None

        # JS verwacht tdee_est (ruwe schatting op basis van gewichtstrend)
        weight = row.get("weight_avg")
        if weight is not None and prev_weight is not None and row["kcal_day_avg"] is not None:
            delta_w = weight - prev_weight  # kg/week
            # tdee ≈ intake - (delta_w * 7700 / 7)
            row["tdee_est"] = row["kcal_day_avg"] - (delta_w * 7700.0 / 7.0)
        else:
            row["tdee_est"] = None

        if weight is not None:
            prev_weight = weight

        out.append(row)

    # limiteren op aantal weken obv days
    max_weeks = max(4, int(days / 7) + 2)
    out = out[:max_weeks]

    return jsonify(out)


@app.route("/api/pro_analysis")
def api_pro_analysis():
    return api_pro_weekly_analysis()


def _parse_yyyy_mm_dd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def _ema_series(xs, loads, tau_days=7):
    if not loads:
        return []

    alpha = 1.0 / float(tau_days)
    ema = [float(loads[0])]

    for load in loads[1:]:
        cur = ema[-1] + alpha * (float(load) - ema[-1])
        ema.append(cur)

    return ema

@app.route("/api/training_load")
def api_training_load():
    days = int(request.args.get("days", 30))
    conn = get_conn()

    rows = conn.execute("""
        SELECT
          date(start_time_local) AS day,
          COALESCE(SUM(tcl), 0) AS tcl
        FROM strava_activities
        WHERE start_time_local IS NOT NULL
        GROUP BY day
        ORDER BY day ASC
    """).fetchall()

    if not rows:
        return jsonify([])

    min_day = _parse_yyyy_mm_dd(rows[0]["day"])
    max_day = _parse_yyyy_mm_dd(rows[-1]["day"])

    end_day = max_day
    display_start_day = end_day - timedelta(days=days - 1)

    # extra historie om ATL/CTL stabiel te maken
    warmup_days = 180
    calc_start_day = max(min_day, display_start_day - timedelta(days=warmup_days))

    tcl_by_day = {r["day"]: float(r["tcl"] or 0) for r in rows}

    xs = []
    loads = []
    d = calc_start_day
    while d <= end_day:
        ds = d.isoformat()
        xs.append(ds)
        loads.append(tcl_by_day.get(ds, 0.0))
        d += timedelta(days=1)

    atl = _ema_series(xs, loads, tau_days=7)
    ctl = _ema_series(xs, loads, tau_days=42)
    tsb = [c - a for c, a in zip(ctl, atl)]

    out = []
    for i in range(len(xs)):
        if xs[i] < display_start_day.isoformat():
            continue

        out.append({
            "day": xs[i],
            "tcl": round(loads[i], 2),
            "atl": round(atl[i], 2),
            "ctl": round(ctl[i], 2),
            "tsb": round(tsb[i], 2),
        })

    return jsonify(out)

@app.route("/hume", methods=["GET", "POST"])
def hume():

    conn = get_conn()

    if request.method == "POST":

        conn.execute("""
            INSERT INTO hume_body (
                day,
                weight_kg,
                body_fat_pct,
                muscle_mass_kg,
                lean_mass_kg,
                visceral_fat_index,
                body_water_pct,
                body_cell_mass_kg
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
                weight_kg=excluded.weight_kg,
                body_fat_pct=excluded.body_fat_pct,
                muscle_mass_kg=excluded.muscle_mass_kg,
                lean_mass_kg=excluded.lean_mass_kg,
                visceral_fat_index=excluded.visceral_fat_index,
                body_water_pct=excluded.body_water_pct,
                body_cell_mass_kg=excluded.body_cell_mass_kg
        """, (

            request.form["day"],
            request.form.get("weight_kg"),
            request.form.get("body_fat_pct"),
            request.form.get("muscle_mass_kg"),
            request.form.get("lean_mass_kg"),
            request.form.get("visceral_fat_index"),
            request.form.get("body_water_pct"),
            request.form.get("body_cell_mass_kg"),

        ))

        conn.commit()
        conn.close()

        # dit is correct
        return redirect(url_for("hume"))


    entries = conn.execute("""
        SELECT *
        FROM hume_body
        ORDER BY day DESC
    """).fetchall()

    conn.close()

    return render_template(
        "hume.html",
        entries=entries,
        active_page="hume"
    )

@app.route("/fitatu", methods=["GET", "POST"])
def fitatu():
    if request.method == "POST":
        f = request.files.get("file")
        if not f or f.filename == "":
            flash("Kies eerst een CSV bestand.", "error")
            return redirect(url_for("fitatu"))

        with get_conn() as conn:
            stats = import_fitatu_meal_csv(conn, f)

            last = conn.execute("SELECT MAX(day) AS max_day FROM fitatu_daily").fetchone()
            last_day = last["max_day"] if last else None

        flash(
            f"Fitatu import OK: {stats['days']} dagen ({stats['min_day']} → {stats['max_day']}). "
            f"DB laatste dag: {last_day}. Bad rows: {stats['bad_rows']}",
            "success"
        )
        return redirect(url_for("fitatu"))

    with get_conn() as conn:
        entries = conn.execute("""
            SELECT day, calories, protein_g, carbs_g, fat_g, fiber_g
            FROM fitatu_daily
            ORDER BY day DESC
            LIMIT 31
        """).fetchall()

        weekly_summary = get_latest_fitatu_week_summary(
            conn,
            weekly_kcal_target=17500,
            protein_target_g_per_day=175,
            fat_target_g_per_day=100,
            carbs_target_g_per_day=225,
            fiber_target_g_per_day=30,
            salt_target_g_per_day=6,
        )

    return render_template(
        "fitatu.html",
        active_page="fitatu",
        days=180,
        weekly_kcal_target=17500,
        entries=entries,
        weekly_summary=weekly_summary,
    )



if __name__ == "__main__":
    app.run(debug=True)