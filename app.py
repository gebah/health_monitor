from flask import Flask, render_template, request, jsonify, redirect, flash
from datetime import date, datetime, timedelta
from collector import sync_garmin, sync_strava, sync_all

import sqlite3

app = Flask(__name__)
app.secret_key = "secret"

DB = "/home/gba/Documenten/PycharmProjects/health_monitor/garmin.sqlite"

USER_NAME = "Gé"
USER_HEIGHT = 1.80


def get_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


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

@app.route("/")
def dashboard():
    days = int(request.args.get("days", 30))
    act_limit = int(request.args.get("acts", 25))
    activity_type = request.args.get("type", "all")

    conn = get_conn()  # <-- FIX: conn bestaat vanaf hier

    # types voor dropdown (uit activities)
    types = [r["t"] for r in conn.execute("""
        SELECT DISTINCT activity_type AS t
        FROM activities
        WHERE activity_type IS NOT NULL AND activity_type != ''
        ORDER BY t
    """).fetchall()]

    latest_activities = get_latest_activities(conn, limit=act_limit, activity_type=activity_type)

    latest = conn.execute("""
        SELECT *
        FROM daily_metrics
        ORDER BY day DESC
        LIMIT 1
    """).fetchone()
    latest = row_to_dict(latest)

    tcl_7d = get_tcl_7d(conn)
    tl = get_training_load_today(conn)          # atl/ctl/tsb
    flags = get_recovery_flags(conn)  
    readiness = readiness_score(latest, tcl_7d=tcl_7d, tcl_target_7d=300)

    latest_hume = conn.execute("""
        SELECT *
        FROM hume_body
        ORDER BY day DESC
        LIMIT 1
    """).fetchone()
    latest_hume = dict(latest_hume) if latest_hume else None

    # deltas/lbmi (veilig)
    delta_weight = delta_lean = delta_bf = lbmi = delta_lbmi = None
    if latest_hume:
        w = latest_hume.get("weight_kg")
        lean = latest_hume.get("lean_mass_kg")
        bf = latest_hume.get("body_fat_pct")

        if w is not None:
            delta_weight = w - 90
        if bf is not None:
            delta_bf = bf - 17.5
        if lean is not None:
            delta_lean = lean - 70
            lbmi = lean / (USER_HEIGHT ** 2)
            delta_lbmi = lbmi - (70 / (USER_HEIGHT ** 2))

    return render_template(
        "dashboard.html",
        latest=latest,
        tcl_7d=tcl_7d,
        tcl_target_7d=300,
        tsb=tl.get("tsb"),
        atl=tl.get("atl"),
        ctl=tl.get("ctl"),
        flags=flags,
        readiness=readiness,
        readiness_score=readiness["score"],
        USER_NAME=USER_NAME,
        USER_HEIGHT=USER_HEIGHT,
        days=days,
        act_limit=act_limit,
        activity_type=activity_type,
        types=types,
        latest_hume=latest_hume,
        delta_weight=delta_weight,
        delta_lean=delta_lean,
        delta_bf=delta_bf,
        lbmi=lbmi,
        delta_lbmi=delta_lbmi,
        status_emoji=None,
        status_text=None,
        advice=None,
        latest_activities=latest_activities,
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
    days = int(request.args.get("days", 180))
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

if __name__ == "__main__":
    app.run(debug=True)