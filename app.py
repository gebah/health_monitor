from flask import Flask, render_template, request, jsonify, redirect, current_app, url_for, flash
from datetime import date, datetime, timedelta
from collector import sync_garmin, sync_strava, sync_all

import sqlite3
import json
import csv
import io

app = Flask(__name__)
app.secret_key = "secret"

DB = "/home/gba/Documenten/PycharmProjects/health_monitor/garmin.sqlite"

USER_NAME = "Gé"
USER_HEIGHT = 1.92

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


def clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


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

    hrv = latest.get("hrv_rmssd")
    sleep = latest.get("sleep_score")
    stress = latest.get("avg_stress")
    bb = latest.get("body_battery_high")

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
            FROM activities
            WHERE activity_type = ?
            ORDER BY start_time_local DESC
            LIMIT ?
        """, (activity_type, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT *
            FROM activities
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
    out = []
    prev = float(loads[0])
    out.append(prev)
    for x in loads[1:]:
        x = float(x)
        prev = prev + (x - prev) * k
        out.append(prev)
    return out

def get_training_load_today(conn, days_back: int = 120):
    """
    Geeft laatste ATL/CTL/TSB + daily TCL + laatste 2 dagen load.
    Gebaseerd op strava_activities.tcl en start_time_local.
    """
    rows = conn.execute("""
        SELECT date(start_time_local) AS day, COALESCE(SUM(tcl),0) AS tcl
        FROM strava_activities
        WHERE start_time_local IS NOT NULL
        GROUP BY day
        ORDER BY day ASC
    """).fetchall()

    if not rows:
        return {"atl": None, "ctl": None, "tsb": None, "tcl_today": None, "tcl_yesterday": None}

    min_day = _parse_yyyy_mm_dd(rows[0]["day"])
    max_day = _parse_yyyy_mm_dd(rows[-1]["day"])

    end_day = max_day
    start_day = end_day - timedelta(days=days_back - 1)

    tcl_by_day = {r["day"]: float(r["tcl"] or 0) for r in rows}

    xs = []
    loads = []
    d = start_day
    while d <= end_day:
        ds = d.isoformat()
        xs.append(ds)
        loads.append(tcl_by_day.get(ds, 0.0))
        d += timedelta(days=1)

    atl = _ema(loads, 7)
    ctl = _ema(loads, 42)
    tsb = [c - a for c, a in zip(ctl, atl)]

    # today / yesterday tcl
    tcl_today = loads[-1] if loads else None
    tcl_yesterday = loads[-2] if len(loads) >= 2 else None

    return {
        "atl": atl[-1] if atl else None,
        "ctl": ctl[-1] if ctl else None,
        "tsb": tsb[-1] if tsb else None,
        "tcl_today": tcl_today,
        "tcl_yesterday": tcl_yesterday,
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

def import_fitatu_meal_csv(conn, file_storage) -> dict:
    """
    Fitatu 'maaltijdplan/maaltijd' CSV:
    - meerdere regels per dag (producten)
    - aggregeert naar 1 rij per dag in fitatu_daily
    """
    raw = file_storage.read()
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    # map CSV kolommen -> jouw fitatu_daily kolommen
    COL_DAY = "Datum"
    COL_KCAL = "calorieën (kcal)"
    COL_P = "Eiwitten (g)"
    COL_C = "Koolhydraten (g)"
    COL_F = "Vetten (g)"
    COL_FIBER = "Vezels (g)"

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

        # skip echt lege regels
        if kcal is None and p is None and c is None and f is None and fib is None:
            bad_rows += 1
            continue

        d = per_day.setdefault(day, {"calories": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "fiber_g": 0.0})
        d["calories"] += kcal or 0.0
        d["protein_g"] += p or 0.0
        d["carbs_g"] += c or 0.0
        d["fat_g"] += f or 0.0
        d["fiber_g"] += fib or 0.0

    # UPSERT per dag
    upserts = 0
    for day, t in per_day.items():
        conn.execute("""
            INSERT INTO fitatu_daily(day, calories, protein_g, carbs_g, fat_g, fiber_g)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
              calories=excluded.calories,
              protein_g=excluded.protein_g,
              carbs_g=excluded.carbs_g,
              fat_g=excluded.fat_g,
              fiber_g=excluded.fiber_g
        """, (day, t["calories"], t["protein_g"], t["carbs_g"], t["fat_g"], t["fiber_g"]))
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

@app.route("/")
def home():
    days = int(request.args.get("days", 30))

    with get_conn() as conn:
        latest = conn.execute("SELECT * FROM daily_metrics ORDER BY day DESC LIMIT 1").fetchone()
        latest = row_to_dict(latest)

        tcl_7d = get_tcl_7d(conn)
        tl = get_training_load_today(conn)
        flags = get_recovery_flags(conn)

        readiness = readiness_score(
            latest,
            tcl_7d=tcl_7d,
            tcl_target_7d=300,
            tsb=tl.get("tsb"),
            atl=tl.get("atl"),
            ctl=tl.get("ctl"),
            flags=flags
        )

        # header cache (als je dit al hebt: laten)
        cache_set(conn, "readiness_header", {
            "score": readiness.get("score"),
            "label": readiness.get("label"),
            "icon": readiness.get("icon"),
            "tone": readiness.get("tone"),
            "state": readiness.get("state"),
            "why": readiness.get("why"),
        })

        latest_hume = conn.execute("SELECT * FROM hume_body ORDER BY day DESC LIMIT 1").fetchone()
        latest_hume = dict(latest_hume) if latest_hume else None

    delta_weight = delta_lean = delta_bf = lbmi = delta_lbmi = None
    if latest_hume:
        w = latest_hume.get("weight_kg")
        lean = latest_hume.get("lean_mass_kg")
        bf = latest_hume.get("body_fat_pct")
        if w is not None: delta_weight = w - 90
        if bf is not None: delta_bf = bf - 17.5
        if lean is not None:
            delta_lean = lean - 70
            lbmi = lean / (USER_HEIGHT ** 2)
            delta_lbmi = lbmi - (70 / (USER_HEIGHT ** 2))

    return render_template(
        "home.html",
        active_page="home",
        days=days,
        latest=latest,
        readiness=readiness,
        readiness_score=readiness["score"],
        USER_NAME=USER_NAME,
        USER_HEIGHT=USER_HEIGHT,
        latest_hume=latest_hume,
        delta_weight=delta_weight,
        delta_lean=delta_lean,
        delta_bf=delta_bf,
        lbmi=lbmi,
        delta_lbmi=delta_lbmi,
    )

@app.route("/hume/charts")
def hume_charts():
    days = int(request.args.get("days", 180))
    return render_template("hume_charts.html", active_page="hume", days=days, USER_HEIGHT=USER_HEIGHT)

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
        types = [r["t"] for r in conn.execute("""
            SELECT DISTINCT activity_type AS t
            FROM activities
            WHERE activity_type IS NOT NULL AND activity_type != ''
            ORDER BY t
        """).fetchall()]
        latest_activities = get_latest_activities(conn, limit=act_limit, activity_type=activity_type)

    return render_template(
        "activities.html",
        active_page="activities",
        latest_activities=latest_activities,
        types=types,
        act_limit=act_limit,
        activity_type=activity_type
    )


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


@app.route("/api/activities")
def api_activities():
    limit = int(request.args.get("acts", request.args.get("limit", 25)))
    activity_type = request.args.get("type", "all")
    conn = get_conn()

    if activity_type != "all":
        rows = conn.execute("""
            SELECT *
            FROM activities
            WHERE activity_type = ?
            ORDER BY start_time_local DESC
            LIMIT ?
        """, (activity_type, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT *
            FROM activities
            ORDER BY start_time_local DESC
            LIMIT ?
        """, (limit,)).fetchall()

    return jsonify([dict(r) for r in rows])

@app.route("/api/activity_types")
def api_activity_types():
    conn = get_conn()
    rows = conn.execute("""
        SELECT DISTINCT activity_type AS t
        FROM activities
        WHERE activity_type IS NOT NULL AND activity_type != ''
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

def _ema_series(days, loads, tau_days: int):
    """
    TrainingPeaks-achtige EMA:
      ema[t] = ema[t-1] + (load[t] - ema[t-1]) * (1/tau)
    tau=7 (ATL), tau=42 (CTL)
    """
    if not days:
        return []
    k = 1.0 / float(tau_days)
    ema = []
    prev = float(loads[0])
    ema.append(prev)
    for i in range(1, len(loads)):
        x = float(loads[i])
        prev = prev + (x - prev) * k
        ema.append(prev)
    return ema

@app.route("/api/training_load")
def api_training_load():
    days = int(request.args.get("days", 30))
    conn = get_conn()

    # Daily TCL from Strava activities (tcl) grouped by date
    rows = conn.execute("""
        SELECT
          date(start_time_local) AS day,
          COALESCE(SUM(tcl), 0)  AS tcl
        FROM strava_activities
        WHERE start_time_local IS NOT NULL
        GROUP BY day
        ORDER BY day ASC
    """).fetchall()

    if not rows:
        return jsonify([])

    # Build continuous day range (fill missing days with 0)
    min_day = _parse_yyyy_mm_dd(rows[0]["day"])
    max_day = _parse_yyyy_mm_dd(rows[-1]["day"])

    # Limit to last N days
    end_day = max_day
    start_day = end_day - timedelta(days=days - 1)

    tcl_by_day = {r["day"]: float(r["tcl"] or 0) for r in rows}

    xs = []
    loads = []
    d = start_day
    while d <= end_day:
        ds = d.isoformat()
        xs.append(ds)
        loads.append(tcl_by_day.get(ds, 0.0))
        d += timedelta(days=1)

    atl = _ema_series(xs, loads, tau_days=7)    # fatigue
    ctl = _ema_series(xs, loads, tau_days=42)   # fitness
    tsb = [c - a for c, a in zip(ctl, atl)]     # form

    out = []
    for i in range(len(xs)):
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

            # extra check: wat is nu de laatste dag in de DB?
            last = conn.execute("SELECT MAX(day) AS max_day FROM fitatu_daily").fetchone()
            last_day = last["max_day"] if last else None

        flash(
            f"Fitatu import OK: {stats['days']} dagen ({stats['min_day']} → {stats['max_day']}). "
            f"DB laatste dag: {last_day}. Bad rows: {stats['bad_rows']}",
            "success"
        )
        return redirect(url_for("fitatu"))

    # Optioneel: toon laatste entries in de pagina (handig om te zien dat import gelukt is)
    with get_conn() as conn:
        entries = conn.execute("""
            SELECT day, calories, protein_g, carbs_g, fat_g, fiber_g
            FROM fitatu_daily
            ORDER BY day DESC
            LIMIT 31
        """).fetchall()

    return render_template("fitatu.html", active_page="fitatu", days=180, weekly_kcal_target=17500)


if __name__ == "__main__":
    app.run(debug=True)