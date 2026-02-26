from flask import Flask, render_template, request, jsonify, redirect, flash
import sqlite3

from collector import sync_garmin, sync_strava, sync_all

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


def readiness_score(latest, *, tcl_7d=0.0, tcl_target_7d=300.0):
    """
    Readiness = HRV + Sleep + Stress + Body Battery + Training Load (TCL 7d)

    tcl_target_7d: richtpunt per 7 dagen (pas aan naar jouw niveau)
    """
    latest = latest or {}

    hrv = latest.get("hrv_rmssd")
    sleep = latest.get("sleep_score")
    stress = latest.get("avg_stress")
    bb = latest.get("body_battery_high")

    hrv_score = clamp((hrv - 20) * 1.5) if hrv is not None else 50
    sleep_score = sleep if sleep is not None else 50
    stress_score = clamp(100 - stress * 2) if stress is not None else 50
    bb_score = bb if bb is not None else 50

    # --- Training load score (TCL 7d) ---
    # ratio rond 1.0 = top; te hoog = penalize, te laag = klein beetje minder
    target = max(1.0, float(tcl_target_7d))
    ratio = (float(tcl_7d) / target) if target else 0.0

    if ratio <= 0.60:
        load_score = 80.0
    elif ratio <= 1.00:
        # 0.60 -> 80, 1.00 -> 100
        load_score = 80.0 + (ratio - 0.60) / 0.40 * 20.0
    elif ratio <= 1.40:
        # 1.00 -> 100, 1.40 -> 60
        load_score = 100.0 - (ratio - 1.00) / 0.40 * 40.0
    else:
        load_score = 40.0

    load_score = clamp(load_score)

    # --- Combine ---
    score = round(
        0.30 * hrv_score +
        0.30 * sleep_score +
        0.15 * stress_score +
        0.10 * bb_score +
        0.15 * load_score
    )

    label = "Good" if score >= 75 else "OK" if score >= 50 else "Low"

    return {
        "score": score,
        "label": label,
        "components": {
            "hrv": round(hrv_score),
            "sleep": round(sleep_score),
            "stress": round(stress_score),
            "bb": round(bb_score),
            "load": round(load_score),
        },
        "tcl_7d": round(float(tcl_7d), 1),
        "tcl_target_7d": round(float(tcl_target_7d), 1),

        # laat deze staan als je template ze nog ergens verwacht
        "fuel": None,
        "kcal": None,
        "kcal_target": None,
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


if __name__ == "__main__":
    app.run(debug=True)