from flask import Flask, render_template, request, jsonify, redirect, current_app, url_for, flash
from datetime import date, datetime, timedelta, UTC
from collector import sync_garmin, ensure_manual_recovery_table
from models import get_daily_metrics_history, calc_body_deltas
from recovery import get_latest_manual_recovery, get_manual_recovery_series, get_best_recovery_for_day, get_latest_recovery_input, get_recovery_baselines

import sqlite3
import json
import csv
import io

app = Flask(__name__)
app.secret_key = "secret"

#DB = "/home/gba/Documenten/PycharmProjects/health_monitor/health.sqlite"
DB = "/opt/health_monitor/health.sqlite"

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

def match_activity_type(activity_type, keywords):
    t = (activity_type or "").lower()
    return any(k in t for k in keywords)

def format_duration_short(seconds):
    if seconds is None:
        return "0u 00m"
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}u {minutes:02d}m"

def format_km(value):
    return round(float(value or 0), 1)

def clamp(value, low=0, high=100):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = low
    return max(low, min(high, value))


def avg_ignore_none(values):
    vals = [v for v in values if v not in (None, "")]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 2)


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
    """, (key, json.dumps(obj), datetime.now(UTC).isoformat()))
    conn.commit()

def cache_get(conn, key, default=None):
    ensure_cache_table(conn)
    row = conn.execute("SELECT value FROM app_cache WHERE key=?", (key,)).fetchone()
    if not row:
        return default

    raw = row["value"]
    try:
        return json.loads(raw)
    except Exception:
        return raw

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

def time_ago_from_iso(ts):
    if not ts:
        return None

    try:
        dt = datetime.fromisoformat(ts)
    except Exception:
        return ts

    # Oude naive timestamps als UTC behandelen
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)

    delta = datetime.now(UTC) - dt
    minutes = int(delta.total_seconds() // 60)

    if minutes < 1:
        return "zojuist"
    if minutes < 60:
        return f"{minutes} min geleden"

    hours = minutes // 60
    if hours < 24:
        return f"{hours} uur geleden"

    days = hours // 24
    return f"{days} d geleden"

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

def get_training_load_today(conn, days_back: int = 365):
    """
    Geeft ATL/CTL/Form op basis van dagelijkse trainingsload uit strava_activities.tcl.

    Gebruikt start-of-day én end-of-day waarden:
    - end-of-day: na de load van vandaag
    - start-of-day: vóór de load van vandaag
    """

    rows = conn.execute("""
        SELECT
            date(start_time_local) AS day,
            SUM(COALESCE(tcl, 0)) AS load
        FROM strava_activities
        WHERE start_time_local IS NOT NULL
        GROUP BY date(start_time_local)
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
            "atl_start_prev": None,
            "ctl_start_prev": None,
            "tsb_start_prev": None,
        }

    daily = {row["day"]: float(row["load"] or 0.0) for row in rows}

    days_sorted = sorted(daily.keys())
    min_day = _parse_yyyy_mm_dd(days_sorted[0])
    max_day = _parse_yyyy_mm_dd(days_sorted[-1])

    end_day = max_day
    start_day = end_day - timedelta(days=days_back - 1)

    if start_day < min_day:
        start_day = min_day

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
            "atl_start_prev": None,
            "ctl_start_prev": None,
            "tsb_start_prev": None,
        }

    tcl_today = loads[-1] if loads else None
    tcl_yesterday = loads[-2] if len(loads) >= 2 else None

    atl_end = atl_series[-1]
    ctl_end = ctl_series[-1]
    tsb_end = tsb_series[-1]

    if len(atl_series) >= 2 and len(ctl_series) >= 2:
        atl_start = atl_series[-2]
        ctl_start = ctl_series[-2]
        tsb_start = ctl_start - atl_start
    else:
        atl_start = atl_end
        ctl_start = ctl_end
        tsb_start = tsb_end

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
    rows = conn.execute("""
        SELECT
            date(start_time_local) AS day,
            SUM(COALESCE(tcl, 0)) AS load
        FROM strava_activities
        WHERE start_time_local IS NOT NULL
        GROUP BY date(start_time_local)
        ORDER BY day ASC
    """).fetchall()

    if not rows:
        return []

    daily = {row["day"]: float(row["load"] or 0.0) for row in rows}

    days_sorted = sorted(daily.keys())
    min_day = _parse_yyyy_mm_dd(days_sorted[0])
    max_day = _parse_yyyy_mm_dd(days_sorted[-1])

    end_day = max_day
    start_day = end_day - timedelta(days=days_back - 1)

    if start_day < min_day:
        start_day = min_day

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

def build_recovery_summary(conn):
    rows = conn.execute("""
        SELECT day, hrv_rmssd, stress, sleep_score, notes
        FROM manual_recovery_entries
        ORDER BY day DESC
        LIMIT 7
    """).fetchall()

    rows = [dict(r) for r in rows]
    if not rows:
        return {
            "n_days": 0,
            "hrv_baseline": None,
            "stress_baseline": None,
            "sleep_baseline": None,
            "hrv_today": None,
            "stress_today": None,
            "sleep_today": None,
            "hrv_status": None,
            "stress_status": None,
            "sleep_status": None,
            "trend": "unknown",
            "confidence": "low",
            "latest_notes": None,
        }

    def avg(values):
        vals = [v for v in values if v is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    latest = rows[0]

    hrv_today = latest.get("hrv_rmssd")
    stress_today = latest.get("stress")
    sleep_today = latest.get("sleep_score")

    hrv_baseline = avg([r.get("hrv_rmssd") for r in rows[:7]])
    stress_baseline = avg([r.get("stress") for r in rows[:7]])
    sleep_baseline = avg([r.get("sleep_score") for r in rows[:7]])

    hrv_status = None
    stress_status = None
    sleep_status = None

    if hrv_today is not None and hrv_baseline is not None:
        if hrv_today <= hrv_baseline * 0.90:
            hrv_status = "low"
        elif hrv_today >= hrv_baseline * 1.05:
            hrv_status = "good"
        else:
            hrv_status = "normal"

    if stress_today is not None and stress_baseline is not None:
        if stress_today >= stress_baseline + 5:
            stress_status = "high"
        elif stress_today <= stress_baseline - 3:
            stress_status = "good"
        else:
            stress_status = "normal"

    if sleep_today is not None and sleep_baseline is not None:
        if sleep_today < 60:
            sleep_status = "low"
        elif sleep_today < sleep_baseline - 7:
            sleep_status = "below_baseline"
        elif sleep_today >= sleep_baseline:
            sleep_status = "good"
        else:
            sleep_status = "normal"

    # trend op basis van laatste 3 vs oudere 4
    recent = rows[:3]
    older = rows[3:7]

    recent_hrv = avg([r.get("hrv_rmssd") for r in recent])
    older_hrv = avg([r.get("hrv_rmssd") for r in older])

    recent_stress = avg([r.get("stress") for r in recent])
    older_stress = avg([r.get("stress") for r in older])

    recent_sleep = avg([r.get("sleep_score") for r in recent])
    older_sleep = avg([r.get("sleep_score") for r in older])

    worsening_signals = 0
    improving_signals = 0

    if recent_hrv is not None and older_hrv is not None:
        if recent_hrv < older_hrv * 0.95:
            worsening_signals += 1
        elif recent_hrv > older_hrv * 1.03:
            improving_signals += 1

    if recent_stress is not None and older_stress is not None:
        if recent_stress > older_stress + 3:
            worsening_signals += 1
        elif recent_stress < older_stress - 2:
            improving_signals += 1

    if recent_sleep is not None and older_sleep is not None:
        if recent_sleep < older_sleep - 5:
            worsening_signals += 1
        elif recent_sleep > older_sleep + 3:
            improving_signals += 1

    if worsening_signals >= 2:
        trend = "worsening"
    elif improving_signals >= 2:
        trend = "improving"
    else:
        trend = "stable"

    n_days = len(rows)
    if n_days >= 6:
        confidence = "high"
    elif n_days >= 3:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "n_days": n_days,
        "hrv_baseline": hrv_baseline,
        "stress_baseline": stress_baseline,
        "sleep_baseline": sleep_baseline,
        "hrv_today": hrv_today,
        "stress_today": stress_today,
        "sleep_today": sleep_today,
        "hrv_status": hrv_status,
        "stress_status": stress_status,
        "sleep_status": sleep_status,
        "trend": trend,
        "confidence": confidence,
        "latest_notes": latest.get("notes"),
    }

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

def build_coach_signals(conn):
    recovery_summary = build_recovery_summary(conn)

    tcl_7d = get_tcl_7d(conn)
    strava_status = get_live_strava_status(conn) or {}

    tsb = strava_status.get("form")
    ctl = strava_status.get("fitness")
    atl = strava_status.get("fatigue")

    signals = {
        "recovery_summary": recovery_summary,
        "tcl_7d": tcl_7d,
        "ctl": ctl,
        "atl": atl,
        "tsb": tsb,
        "load_flag": None,
        "status": "ready",
        "priority": None,
    }

    if tsb is not None:
        if tsb <= -20:
            signals["load_flag"] = "very_fatigued"
        elif tsb <= -10:
            signals["load_flag"] = "fatigued"
        elif tsb >= 10:
            signals["load_flag"] = "fresh"
        else:
            signals["load_flag"] = "normal"

    return signals

def build_priority_coach_message(conn):
    s = build_coach_signals(conn)
    r = s["recovery_summary"]

    bullets = []
    why = []
        
    if r["hrv_status"] == "low":
        why.append("HRV ligt onder je baseline")
        bullets.append("HRV ligt onder je baseline")
    elif r["hrv_status"] == "good":
        why.append("HRV ligt op of boven je baseline")
        bullets.append("HRV ligt op of boven je baseline")        

    if r["stress_status"] == "high":
        why.append("Stress ligt hoger dan normaal")
        bullets.append("Stress ligt hoger dan normaal")        
    elif r["stress_status"] == "good":
        why.append("Stress is lager dan normaal")
        bullets.append("Stress is lager dan normaal")

    if r["sleep_status"] == "low":
        why.append("Slaapscore is laag")
        bullets.append("Slaapscore is laag")        
    elif r["sleep_status"] == "below_baseline":
        why.append("Slaapscore ligt onder je normale niveau")
        bullets.append("Slaapscore ligt onder je normale niveau")        
    elif r["sleep_status"] == "good":
        why.append("Slaapscore is prima")
        bullets.append("Slaapscore is prima")       
        
    if r["trend"] == "worsening":
        why.append("Hersteltrend verslechtert")
        bullets.append("Hersteltrend verslechtert")
    elif r["trend"] == "improving":
        why.append("Hersteltrend verbetert")
        bullets.append("Hersteltrend verbetert")         

    if s["load_flag"] == "very_fatigued":
        why.append("Trainingsbelasting is zwaar")
        bullets.append("Strava status wijst op stevige vermoeidheid")
    elif s["load_flag"] == "fatigued":
        why.append("Trainingsbelasting loopt op")
        bullets.append("Trainingsvermoeidheid loopt op")
    elif s["load_flag"] == "fresh":
        why.append("Je bent relatief fris qua trainingsbelasting")
        bullets.append("Je bent relatief fris qua trainingsbelasting")
    else:
        why.append("Trainingsbelasting is normaal")
        bullets.append("Trainingsbelasting is normaal")

    if r["latest_notes"]:
        bullets.append(f"Notitie: {r['latest_notes']}")

    red_recovery = sum([
        1 if r["hrv_status"] == "low" else 0,
        1 if r["stress_status"] == "high" else 0,
        1 if r["sleep_status"] in ("low", "below_baseline") else 0,
        1 if r["trend"] == "worsening" else 0,
    ])

    heavy_load = s["load_flag"] in ("fatigued", "very_fatigued")

    if red_recovery >= 3 and heavy_load:
        status = "recovery_needed"
        priority = "recovery"
        top_insight = "Zowel herstel als trainingsbelasting staan nu duidelijk onder druk."
        advice = "Vandaag liefst herstel, wandelen of heel rustig duurwerk. Geen zware sessie."
    elif red_recovery >= 2:
        status = "light_fatigue"
        priority = "recovery"
        top_insight = "Je herstel is momenteel de beperkende factor."
        advice = "Hou training licht tot matig en geef slaap en herstel prioriteit."
    elif heavy_load:
        if s["load_flag"] == "very_fatigued":
            status = "recovery_needed"
            priority = "fatigue"
            top_insight = "Je trainingsbelasting is nu duidelijk hoger dan je herstel aankan."
            advice = "Neem gas terug: rustdag of hersteltraining is hier het verstandigst."
        else:
            status = "light_fatigue"
            priority = "fatigue"
            top_insight = "Je herstel is redelijk, maar trainingsvermoeidheid stapelt zich op."
            advice = "Rustige duurtraining of upper body kan prima, zware benenprikkel liever uitstellen."
    else:
        if r["trend"] == "improving" and r["sleep_status"] == "good":
            status = "fresh"
            priority = "readiness"
            top_insight = "Je hersteltrend verbetert en de signalen staan er goed voor."
            advice = "Goede dag voor een normale tot stevige training, mits je benen ook goed voelen."
        else:
            status = "ready"
            priority = "readiness"
            top_insight = "Er zijn geen sterke rode vlaggen; je bent normaal trainbaar."
            advice = "Normale trainingsdag is prima, met wat ruimte om op gevoel te sturen."


    return {
        "status": status,
        "priority": priority,
        "top_insight": top_insight,
        "bullets": bullets[:5],
        "why": why[:3],
        "advice": advice,
        "recovery": {
            "hrv_rmssd": r["hrv_today"],
            "stress": r["stress_today"],
            "sleep_score": r["sleep_today"],
            "notes": r["latest_notes"],
        },
        "baselines": {
            "hrv_baseline": r["hrv_baseline"],
            "stress_baseline": r["stress_baseline"],
            "sleep_baseline": r["sleep_baseline"],
        },
        "trend": r["trend"],
        "confidence": r["confidence"],
        "tsb": s["tsb"],
        "ctl": s["ctl"],
        "atl": s["atl"],
        "tcl_7d": s["tcl_7d"],
    }

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
    
  
def get_fitness_label(fitness):
    if fitness > 60:
        return "High"
    elif fitness > 40:
        return "Normal"
    else:
        return "Low"

def get_fatigue_label(fatigue):
    if fatigue > 70:
        return "Very high"
    elif fatigue > 55:
        return "High"
    elif fatigue > 40:
        return "Normal"
    else:
        return "Low"
    
def build_coach_insight(hume_summary=None, readiness_header=None, strava_status=None):
    insight = {
        "headline": "Nog onvoldoende data voor een duidelijke analyse.",
        "trend": "Er is al wel data aanwezig, maar nog niet genoeg samenhang voor een sterke interpretatie.",
        "advice": "Blijf consistent meten, eten en trainen.",
        "tone": "unknown",
    }

    if not hume_summary and not readiness_header and not strava_status:
        return insight

    weight_delta = hume_summary.get("weight_delta") if isinstance(hume_summary, dict) else None
    bf_delta = hume_summary.get("bf_delta") if isinstance(hume_summary, dict) else None
    lean_delta = hume_summary.get("lean_delta") if isinstance(hume_summary, dict) else None
    muscle_delta = hume_summary.get("muscle_delta") if isinstance(hume_summary, dict) else None
    visceral_delta = hume_summary.get("visceral_delta") if isinstance(hume_summary, dict) else None

    readiness_score_value = None
    if isinstance(readiness_header, dict):
        readiness_score_value = readiness_header.get("score")

    fitness_delta = strava_status.get("fitness_delta") if isinstance(strava_status, dict) else None
    fatigue_delta = strava_status.get("fatigue_delta") if isinstance(strava_status, dict) else None
    form_value = strava_status.get("form") if isinstance(strava_status, dict) else None

    if bf_delta is not None and bf_delta < 0 and lean_delta is not None and lean_delta >= -0.2:
        insight["headline"] = "Je lichaamssamenstelling beweegt de goede kant op: vet daalt terwijl lean mass redelijk stabiel blijft."
        insight["tone"] = "good"

    elif visceral_delta is not None and visceral_delta < 0:
        insight["headline"] = "Je Hume-data laat een gunstige ontwikkeling zien, met onder meer een daling van het visceraal vet."
        insight["tone"] = "good"

    elif muscle_delta is not None and muscle_delta > 0 and bf_delta is not None and bf_delta <= 0:
        insight["headline"] = "Spiermassa houdt goed stand terwijl vet niet oploopt. Dat wijst op een nette fase van behoud of recompositie."
        insight["tone"] = "good"

    elif weight_delta is not None and weight_delta < 0 and lean_delta is not None and lean_delta < -0.3:
        insight["headline"] = "Je gewicht daalt, maar ook lean mass loopt mee terug. Dat kan wijzen op een wat agressieve fase."
        insight["tone"] = "ok"

    elif weight_delta is not None and weight_delta > 0 and bf_delta is not None and bf_delta <= 0:
        insight["headline"] = "Je gewicht ligt iets hoger, maar vet stijgt niet mee. Dat past eerder bij vocht- of glycogeenschommeling dan bij echte terugval."
        insight["tone"] = "ok"

    elif weight_delta is not None and abs(weight_delta) < 0.2 and bf_delta is not None and bf_delta < 0:
        insight["headline"] = "Je gewicht is vrij stabiel, maar je vetpercentage daalt wel. Dat past goed bij recompositie."
        insight["tone"] = "good"

    if readiness_score_value is not None and fatigue_delta is not None and fitness_delta is not None:
        if readiness_score_value < 55 and fatigue_delta > 0 and fitness_delta <= fatigue_delta:
            insight["trend"] = "Je trainingsbelasting loopt op terwijl je readiness relatief laag blijft. Herstel vraagt nu extra aandacht."
            if insight["tone"] == "unknown":
                insight["tone"] = "ok"
        elif readiness_score_value >= 70 and fitness_delta >= 0:
            insight["trend"] = "Je readiness is degelijk en je fitness houdt goed stand. Dat is een prima basis om gecontroleerd door te bouwen."
            if insight["tone"] == "unknown":
                insight["tone"] = "good"
        elif form_value is not None and form_value < -10:
            insight["trend"] = "Je vorm staat nog behoorlijk onder druk. Dat hoeft niet verkeerd te zijn, maar het vraagt wel om gecontroleerde belasting."
            if insight["tone"] == "unknown":
                insight["tone"] = "ok"
        else:
            insight["trend"] = "De combinatie van Hume-, readiness- en trainingsdata oogt redelijk stabiel, zonder grote alarmsignalen."
            if insight["tone"] == "unknown":
                insight["tone"] = "good"
    elif isinstance(hume_summary, dict):
        insight["trend"] = "Je Hume-data laat duidelijke beweging zien, maar met extra trainings- en readiness-context wordt de analyse nog sterker."

    if insight["tone"] == "good":
        insight["advice"] = "Blijf vooral consistent in voeding, training en herstel. Dit is een fase om rustig rendement te blijven pakken."
    elif readiness_score_value is not None and readiness_score_value < 55:
        insight["advice"] = "Houd je training gecontroleerd en voorkom dat vermoeidheid sneller oploopt dan je herstel aankan."
    elif lean_delta is not None and lean_delta < -0.3:
        insight["advice"] = "Let extra op herstel, eiwitinname en trainingskwaliteit, zodat gewichtsverlies niet te veel ten koste gaat van lean mass."
    else:
        insight["advice"] = "Blijf de trend nog even volgen en stuur vooral op herstelkwaliteit, eiwitinname en trainingsdosering."

    return insight

def calc_metrics(a):
    if not a:
        return None

    if not isinstance(a, dict):
        a = dict(a)

    distance_km = (a.get("distance_m") or 0) / 1000.0
    duration_h = (a.get("moving_time_s") or 0) / 3600.0
    speed_kmh = distance_km / duration_h if duration_h > 0 else 0.0

    return {
        "distance_km": round(distance_km, 1),
        "speed_kmh": round(speed_kmh, 1),
        "suffer": round(a.get("suffer_score") or 0, 1),
    }


def ride_kind_label(kind: str | None) -> str:
    return {
        "road": "Road",
        "gravel": "Gravel",
        "mountain": "MTB",
    }.get(kind, "Ride")


def build_ride_insight(last_metrics, prev_metrics, comparison, ride_kind):
    if not last_metrics or not prev_metrics or not comparison:
        return None

    kind_label = ride_kind_label(ride_kind).lower()

    speed_diff = comparison["speed_diff"]
    suffer_diff = comparison["suffer_diff"]
    distance_diff = comparison["distance_diff"]

    if speed_diff > 0 and suffer_diff <= 0:
        return f"Sterker dan je vorige {kind_label}  rit: hogere snelheid bij gelijke of lagere belasting."
    if speed_diff > 0 and suffer_diff > 0:
        return f"Sneller dan je vorige {kind_label} rit, maar het kostte ook duidelijk meer belasting."
    if speed_diff < 0 and suffer_diff > 0:
        return f"Zwaarder dan je vorige {kind_label} rit, met lagere snelheid en hogere belasting."
    if abs(speed_diff) <= 0.2 and abs(suffer_diff) <= 5:
        return f"Vrijwel vergelijkbaar met je vorige {kind_label} rit."
    if distance_diff > 5:
        return f"Langere {kind_label} rit dan de vorige, dus vergelijk de snelheid met wat nuance."
    if speed_diff < 0:
        return f"Iets langzamer dan je vorige {kind_label} rit."
    return f"Interessante vergelijking met je vorige {kind_label} rit."

def get_week_activity_summary(conn):
    today = datetime.now().date()
    start_day = today - timedelta(days=today.weekday())
    end_day = start_day + timedelta(days=6)

    rows = conn.execute("""
        SELECT
            type,
            COALESCE(distance_m, 0) AS distance_m,
            COALESCE(moving_time_s, 0) AS duration_s
        FROM strava_activities
        WHERE date(start_time_local) BETWEEN ? AND ? 
    """, (start_day.isoformat(), end_day.isoformat())).fetchall()

    summary = {
        "period_label": f"Week {start_day.strftime('%d %b')} t/m {end_day.strftime('%d %b')}",
        "walk_count": 0,
        "walk_distance_km": 0.0,
        "walk_duration_s": 0,
        "ride_count": 0,
        "ride_distance_km": 0.0,
        "ride_duration_s": 0,
        "strength_count": 0,
        "strength_duration_s": 0,
    }

    for row in rows:
        activity_type = row["type"]
        distance_m = float(row["distance_m"] or 0)
        duration_s = int(row["duration_s"] or 0)

        # 🚶 wandelen
        if match_activity_type(activity_type, ["walk", "hike"]):
            summary["walk_count"] += 1
            summary["walk_distance_km"] += distance_m / 1000
            summary["walk_duration_s"] += duration_s

        # 🚴 fietsen
        elif match_activity_type(activity_type, ["ride", "cycl", "bike"]):
            summary["ride_count"] += 1
            summary["ride_distance_km"] += distance_m / 1000
            summary["ride_duration_s"] += duration_s

        # 🏋️ kracht
        elif match_activity_type(activity_type, ["weight", "strength", "workout"]):
            summary["strength_count"] += 1
            summary["strength_duration_s"] += duration_s

    return summary

def trend_meta(delta, current, previous):
    if previous == 0:
        pct = 100.0 if current > 0 else 0.0
    else:
        pct = ((current - previous) / previous) * 100.0

    if delta > 0:
        return {"arrow": "↑", "class": "trend-up", "pct": pct}
    elif delta < 0:
        return {"arrow": "↓", "class": "trend-down", "pct": pct}
    else:
        return {"arrow": "→", "class": "trend-flat", "pct": 0}


def get_activity_summary_period(conn, start_day, end_day):
    rows = conn.execute("""
        SELECT
            type,
            COALESCE(distance_m, 0) AS distance_m,
            COALESCE(moving_time_s, 0) AS duration_s
        FROM strava_activities
        WHERE date(start_time_local) BETWEEN ? AND ?
    """, (start_day.isoformat(), end_day.isoformat())).fetchall()

    summary = {
        "walk_count": 0,
        "walk_distance_km": 0.0,
        "walk_duration_s": 0,
        "ride_count": 0,
        "ride_distance_km": 0.0,
        "ride_duration_s": 0,
        "strength_count": 0,
        "strength_duration_s": 0,
    }

    for row in rows:
        t = row["type"]
        d = float(row["distance_m"] or 0)
        s = int(row["duration_s"] or 0)

        if match_activity_type(t, ["walk", "hike"]):
            summary["walk_count"] += 1
            summary["walk_distance_km"] += d / 1000
            summary["walk_duration_s"] += s

        elif match_activity_type(t, ["ride", "cycl", "bike"]):
            summary["ride_count"] += 1
            summary["ride_distance_km"] += d / 1000
            summary["ride_duration_s"] += s

        elif match_activity_type(t, ["strength", "weight", "workout"]):
            summary["strength_count"] += 1
            summary["strength_duration_s"] += s

    return summary

def trend_arrow(delta):
    if delta > 0:
        return "↑"
    elif delta < 0:
        return "↓"
    return "→"


def build_comparison(this_week, prev_week, start_day, end_day):
    def cmp(key):
        current = this_week.get(key, 0)
        previous = prev_week.get(key, 0)
        delta = current - previous
        trend = trend_meta(delta, current, previous)    

        return {
            "current": current,
            "previous": previous,
            "delta": delta,
            "trend": trend["arrow"],
            "trend_class": trend["class"],
            "trend_pct": trend["pct"],
        }

    return {
        "period_label": f"{start_day} t/m {end_day}",

        "walk": {
            "count": cmp("walk_count"),
            "distance": cmp("walk_distance_km"),
            "duration": cmp("walk_duration_s"),
        },

        "ride": {
            "count": cmp("ride_count"),
            "distance": cmp("ride_distance_km"),
            "duration": cmp("ride_duration_s"),
        },

        "strength": {
            "count": cmp("strength_count"),
            "duration": cmp("strength_duration_s"),
        },
    }

def build_week_coach_message(comp, readiness_score=None, form=None):
    ride_delta = comp["ride"]["distance"]["delta"]
    strength_delta = comp["strength"]["count"]["delta"]

    messages = []

    if ride_delta > 30:
        messages.append("meer fietsvolume dan vorige week")
    elif ride_delta < -30:
        messages.append("minder fietsvolume dan vorige week")

    if strength_delta > 0:
        messages.append("meer krachttraining")
    elif strength_delta < 0:
        messages.append("minder krachttraining")

    low_readiness = readiness_score is not None and readiness_score < 45
    good_readiness = readiness_score is not None and readiness_score >= 65
    heavy_fatigue = form is not None and form < -10

    if low_readiness or heavy_fatigue:
        messages.append("herstel verdient nu prioriteit")
    elif good_readiness:
        messages.append("herstel oogt goed")

    if not messages:
        return "Vrij stabiele trainingsweek."

    sentence = " · ".join(messages)
    return sentence[:1].upper() + sentence[1:] + "."


def get_week_comparison(conn, readiness_score=None, form=None):
    today = datetime.now().date()

    start_this = today - timedelta(days=today.weekday())
    end_this = start_this + timedelta(days=6)

    start_prev = start_this - timedelta(days=7)
    end_prev = start_this - timedelta(days=1)

    this_week = get_activity_summary_period(conn, start_this, end_this)
    prev_week = get_activity_summary_period(conn, start_prev, end_prev)

    comp = build_comparison(this_week, prev_week, start_this, end_this)
    comp["coach"] = build_week_coach_message(
        comp,
        readiness_score=readiness_score,
        form=form,
    )
    return comp


def build_week_readiness_message(week_comp, readiness_score=None, form=None):
    """
    Combineert weektrend met readiness/form.
    readiness_score: 0-100
    form: TSB / form score (mag None zijn)
    """
    ride_km_delta = week_comp["ride"]["distance"]["delta"]
    strength_delta = week_comp["strength"]["count"]["delta"]

    heavy_week = ride_km_delta > 25
    light_week = ride_km_delta < -25
    less_strength = strength_delta < 0
    more_strength = strength_delta > 0

    low_readiness = readiness_score is not None and readiness_score < 45
    okay_readiness = readiness_score is not None and 45 <= readiness_score < 65
    good_readiness = readiness_score is not None and readiness_score >= 65

    heavy_fatigue = form is not None and form < -10
    fresh_form = form is not None and form > 5

    if heavy_week and (low_readiness or heavy_fatigue):
        return "Zware trainingsweek + lage herstelstatus → houd het vandaag rustig of kies herstel."
    if heavy_week and good_readiness:
        return "Meer trainingsvolume dan vorige week en je herstel oogt nog goed → prima opbouw."
    if light_week and low_readiness:
        return "Minder trainingsvolume en lage herstelstatus → waarschijnlijk terecht een rustigere fase."
    if light_week and good_readiness:
        return "Je deed minder dan vorige week terwijl je herstel goed is → ruimte voor een stevige prikkel."
    if less_strength and okay_readiness:
        return "Minder krachttraining deze week → let op behoud van kracht en spierprikkel."
    if less_strength and low_readiness:
        return "Minder krachttraining past bij je lagere herstelstatus → focus op opladen."
    if more_strength and good_readiness:
        return "Meer krachttraining en goede herstelstatus → sterke combinatie voor progressie."
    if fresh_form and good_readiness:
        return "Je herstel en vorm zien er gunstig uit → goed moment voor kwaliteitstraining."
    if heavy_fatigue:
        return "Je weekbelasting loopt op en je vorm is wat vermoeid → bewaak herstel."
    return "Vrij stabiele trainingsweek met een neutrale herstelindruk."

@app.route("/")
def home():
    days = int(request.args.get("days", 30))

    with get_conn() as conn:
        ensure_manual_recovery_table(conn)

        latest_row = conn.execute(
            "SELECT * FROM daily_metrics ORDER BY day DESC LIMIT 1"
        ).fetchone()
        latest = row_to_dict(latest_row)

        coach_v2 = build_priority_coach_message(conn)

        tcl_7d = get_tcl_7d(conn)
        tl = get_training_load_today(conn)
        fitness = tl.get("ctl")
        fatigue = tl.get("atl")
        form = tl.get("tsb")

        fitness_label = get_fitness_label(fitness)
        fatigue_label = get_fatigue_label(fatigue)
        flags = get_recovery_flags(conn)
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

        last_ride_row = conn.execute("""
            SELECT *
            FROM strava_activities
            WHERE ride_kind IN ('road', 'gravel', 'mountain')
            ORDER BY start_time_local DESC
            LIMIT 1
        """).fetchone()

        last_ride = dict(last_ride_row) if last_ride_row else None

        last_ride_kind = last_ride.get("ride_kind") if last_ride else None
        last_ride_kind_label = ride_kind_label(last_ride_kind)

        prev_ride = None
        if last_ride:
            prev_ride_row = conn.execute("""
                SELECT *
                FROM strava_activities
                WHERE ride_kind = ?
                  AND start_time_local < ?
                ORDER BY start_time_local DESC
                LIMIT 1
            """, (
                last_ride["ride_kind"],
                last_ride["start_time_local"],
            )).fetchone()

            prev_ride = dict(prev_ride_row) if prev_ride_row else None

        last_metrics = calc_metrics(last_ride)
        prev_metrics = calc_metrics(prev_ride)

        comparison = None
        if last_metrics and prev_metrics:
            comparison = {
                "distance_diff": round(last_metrics["distance_km"] - prev_metrics["distance_km"], 1),
                "speed_diff": round(last_metrics["speed_kmh"] - prev_metrics["speed_kmh"], 1),
                "suffer_diff": round(last_metrics["suffer"] - prev_metrics["suffer"], 1),
            }

        ride_insight = build_ride_insight(
            last_metrics=last_metrics,
            prev_metrics=prev_metrics,
            comparison=comparison,
            ride_kind=last_ride_kind,
        )

        readiness = readiness_score(
            latest,
            tcl_7d=tcl_7d,
            tcl_target_7d=300,
            tsb=tl.get("tsb"),
            atl=tl.get("atl"),
            ctl=tl.get("ctl"),
            flags=flags,
        ) if latest else {}

        hume_summary = get_hume_weekly_summary(conn)

        cache_set(conn, "readiness_header", {
            "score": training_advice.get("score"),
            "label": training_advice.get("label"),
            "icon": None,
            "tone": training_advice.get("tone"),
            "state": training_advice.get("code"),
            "why": training_advice.get("summary"),
        })

        coach_insight = build_coach_insight(
            hume_summary=hume_summary,
            readiness_header=readiness,
            strava_status=strava_status,
        )

        safe_hume = latest_hume or {}

        deltas = calc_body_deltas(
            safe_hume,
            target_weight=90,
            target_lean=70,
            target_bf=17.5,
            height_m=USER_HEIGHT,
        )

        delta_weight = round(deltas.get("delta_weight"), 1) if deltas.get("delta_weight") is not None else None
        delta_lean = round(deltas.get("delta_lean"), 1) if deltas.get("delta_lean") is not None else None
        delta_bf = round(deltas.get("delta_bf"), 1) if deltas.get("delta_bf") is not None else None
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

        last_strava_sync_obj = cache_get(conn, "last_strava_sync", default=None)

        if isinstance(last_strava_sync_obj, dict):
            last_strava_sync_raw = last_strava_sync_obj.get("ts")
        else:
            last_strava_sync_raw = last_strava_sync_obj

        last_strava_sync = time_ago_from_iso(last_strava_sync_raw)
        
        week_activity = get_week_activity_summary(conn)        
      
        week_comp = get_week_comparison(
            conn, 
            readiness_score=readiness.get("score"), 
            form=form,
        )
    

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
        tl=tl,
        readiness=readiness,
        hume_summary=hume_summary,
        coach_insight=coach_insight,
        coach_v2=coach_v2,
        USER_NAME=USER_NAME,
        USER_HEIGHT=USER_HEIGHT,
        last_strava_sync=last_strava_sync,
        fitness_label=fitness_label,
        fatigue_label=fatigue_label,
        last_ride=last_ride,
        prev_ride=prev_ride,
        last_ride_kind=last_ride_kind,
        last_ride_kind_label=last_ride_kind_label,
        last_metrics=last_metrics,
        prev_metrics=prev_metrics,
        comparison=comparison,
        ride_insight=ride_insight,
        week_activity=week_activity,
        format_duration_short=format_duration_short,
        format_km=format_km,
        week_comp=week_comp,
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

from datetime import date, timedelta

@app.route("/activities")
def activities_page():
    act_limit = int(request.args.get("acts", 50))
    if act_limit <= 0:
        act_limit = 50

    page = int(request.args.get("page", 1))
    if page < 1:
        page = 1

    activity_type = request.args.get("type", "all")
    date_from = request.args.get("from", "").strip()
    date_to = request.args.get("to", "").strip()
    preset = request.args.get("preset", "").strip()

    today = date.today()

    if preset == "year":
        date_from = f"{today.year}-01-01"
        date_to = today.isoformat()
    elif preset == "month":
        date_from = today.replace(day=1).isoformat()
        date_to = today.isoformat()
    elif preset == "30d":
        date_from = (today - timedelta(days=30)).isoformat()
        date_to = today.isoformat()
    elif preset == "all":
        date_from = ""
        date_to = ""

    where_sql = "WHERE 1=1"
    where_params = []

    if activity_type != "all":
        where_sql += " AND type = ?"
        where_params.append(activity_type)

    if date_from:
        where_sql += " AND date(start_time_local) >= date(?)"
        where_params.append(date_from)

    if date_to:
        where_sql += " AND date(start_time_local) <= date(?)"
        where_params.append(date_to)

    offset = (page - 1) * act_limit

    summary_query = f"""
        SELECT
            COUNT(*) AS count,
            COALESCE(SUM(distance_m), 0) AS total_distance_m,
            COALESCE(SUM(moving_time_s), 0) AS total_duration_s
        FROM strava_activities
        {where_sql}
    """

    list_query = f"""
        SELECT
            strava_activity_id AS activity_id,
            start_time_local,
            type AS activity_type,
            name AS activity_name,
            distance_m,
            moving_time_s AS duration_s,
            tcl
        FROM strava_activities
        {where_sql}
        ORDER BY start_time_local DESC
        LIMIT ? OFFSET ?
    """

    with get_conn() as conn:
        summary_row = conn.execute(summary_query, where_params).fetchone()
        latest_activities = conn.execute(
            list_query,
            [*where_params, act_limit, offset]
        ).fetchall()

        types = [
            r["t"] for r in conn.execute("""
                SELECT DISTINCT type AS t
                FROM strava_activities
                WHERE type IS NOT NULL AND type != ''
                ORDER BY t
            """).fetchall()
        ]
        
        weekly_rows = conn.execute(f"""
            SELECT
                strftime('%Y-%W', start_time_local) AS year_week,
                MIN(date(start_time_local)) AS week_start,
                COUNT(*) AS activity_count,
                COALESCE(SUM(distance_m), 0) AS total_distance_m,
                COALESCE(SUM(moving_time_s), 0) AS total_duration_s
            FROM strava_activities
            {where_sql}
            GROUP BY strftime('%Y-%W', start_time_local)
            ORDER BY year_week DESC
            LIMIT 8
        """, where_params).fetchall()

    total_count = summary_row["count"] or 0
    total_pages = max(1, (total_count + act_limit - 1) // act_limit)

    activities = [dict(r) for r in latest_activities]
    
    weekly_totals = []
    for r in weekly_rows:
        year_week = r["year_week"] or ""
        week_label = year_week.replace("-", "-W") if year_week else ""

        weekly_totals.append({
            "year_week": year_week,
            "week_label": week_label,
            "week_start": r["week_start"],
            "activity_count": r["activity_count"] or 0,
            "distance_km": round((r["total_distance_m"] or 0) / 1000, 1),
            "duration_h": round((r["total_duration_s"] or 0) / 3600, 1),
        })

    weekly_totals = list(reversed(weekly_totals))

    summary = {
        "count": total_count,
        "distance_km": round((summary_row["total_distance_m"] or 0) / 1000, 1),
        "duration_h": round((summary_row["total_duration_s"] or 0) / 3600, 1)
    }

    return render_template(
        "activities.html",
        active_page="activities",
        latest_activities=activities,
        types=types,
        act_limit=act_limit,
        activity_type=activity_type,
        date_from=date_from,
        date_to=date_to,
        preset=preset,
        summary=summary,
        page=page,
        total_pages=total_pages,
        total_count=total_count,
        weekly_totals=weekly_totals,
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

@app.route("/recovery", methods=["GET", "POST"])
def recovery():
    with get_conn() as conn:
        ensure_manual_recovery_table(conn)

        if request.method == "POST":
            day = request.form.get("day", "").strip()
            hrv = request.form.get("hrv_rmssd", "").strip()
            stress = request.form.get("stress", "").strip()
            sleep_score = request.form.get("sleep_score", "").strip()
            notes = request.form.get("notes", "").strip()

            def to_float(v):
                if v == "":
                    return None
                return float(v.replace(",", "."))

            try:
                conn.execute("""
                    INSERT INTO manual_recovery_entries (
                        day, hrv_rmssd, stress, sleep_score, notes
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(day) DO UPDATE SET
                        hrv_rmssd = excluded.hrv_rmssd,
                        stress = excluded.stress,
                        sleep_score = excluded.sleep_score,
                        notes = excluded.notes,
                        created_at = CURRENT_TIMESTAMP
                """, (
                    day,
                    to_float(hrv),
                    to_float(stress),
                    to_float(sleep_score),
                    notes or None,
                ))
                conn.commit()
                flash("Recovery data opgeslagen.", "success")
                return redirect(url_for("recovery"))

            except Exception as e:
                flash(f"Opslaan mislukt: {e}", "error")

        latest_manual = get_latest_manual_recovery(conn)
        coach = build_priority_coach_message(conn)

    return render_template(
        "recovery.html",
        active_page="recovery",
        latest_manual=latest_manual,
        coach=coach,
    )

if __name__ == "__main__":
    app.run(debug=True)
