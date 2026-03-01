# models.py
from database import get_conn, row_to_dict

def get_latest_daily_metrics():
    conn = get_conn()
    row = conn.execute("""
        SELECT *
        FROM daily_metrics
        ORDER BY day DESC
        LIMIT 1
    """).fetchone()
    return row_to_dict(row) or {}

def get_daily_metrics_history(days: int = 30):
    conn = get_conn()
    rows = conn.execute("""
        SELECT *
        FROM daily_metrics
        ORDER BY day DESC
        LIMIT ?
    """, (days,)).fetchall()
    return [dict(r) for r in rows]

def get_latest_hume_body():
    conn = get_conn()
    row = conn.execute("""
        SELECT *
        FROM hume_body
        ORDER BY day DESC
        LIMIT 1
    """).fetchone()
    return row_to_dict(row)

def calc_lbmi(lean_mass_kg: float | None, height_m: float):
    if lean_mass_kg is None or not height_m or height_m <= 0:
        return None
    return lean_mass_kg / (height_m ** 2)

def calc_body_deltas(latest_hume: dict | None, *, target_weight: float, target_lean: float, target_bf: float, height_m: float):
    """
    Returns dict with:
      delta_weight, delta_lean, delta_bf, lbmi, delta_lbmi
    """
    if not latest_hume:
        return {
            "delta_weight": None,
            "delta_lean": None,
            "delta_bf": None,
            "lbmi": None,
            "delta_lbmi": None,
        }

    weight = latest_hume.get("weight_kg")
    lean = latest_hume.get("lean_mass_kg")
    bf = latest_hume.get("body_fat_pct")

    delta_weight = (weight - target_weight) if weight is not None else None
    delta_lean = (lean - target_lean) if lean is not None else None
    delta_bf = (bf - target_bf) if bf is not None else None

    lbmi = calc_lbmi(lean, height_m)
    target_lbmi = calc_lbmi(target_lean, height_m)
    delta_lbmi = (lbmi - target_lbmi) if (lbmi is not None and target_lbmi is not None) else None

    return {
        "delta_weight": delta_weight,
        "delta_lean": delta_lean,
        "delta_bf": delta_bf,
        "lbmi": lbmi,
        "delta_lbmi": delta_lbmi,
    }
def debug_db_overview():
    conn = get_conn()

    tables = conn.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type='table'
        ORDER BY name
    """).fetchall()

    out = []
    for t in tables:
        name = t["name"]
        cols = conn.execute(f"PRAGMA table_info({name})").fetchall()
        colnames = [c["name"] for c in cols]
        cnt = conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"]
        out.append({"table": name, "rows": cnt, "columns": colnames})

    return out

def get_activity_types():
    conn = get_conn()
    rows = conn.execute("""
        SELECT DISTINCT activity_type AS t
        FROM activities
        WHERE activity_type IS NOT NULL AND activity_type != ''
        ORDER BY t
    """).fetchall()
    return [r["t"] for r in rows]


def get_activity_trends(days: int = 180, activity_type: str = "all"):
    """
    Voor VO2max + training effect charts: time series.
    Output: [{day, vo2max_value, training_effect}]
    """
    conn = get_conn()

    if activity_type != "all":
        rows = conn.execute("""
            SELECT
              date(start_time_local) AS day,
              AVG(vo2max_value) AS vo2max_value,
              AVG(training_effect) AS training_effect
            FROM activities
            WHERE activity_type = ?
            GROUP BY day
            ORDER BY day DESC
            LIMIT ?
        """, (activity_type, days)).fetchall()
    else:
        rows = conn.execute("""
            SELECT
              date(start_time_local) AS day,
              AVG(vo2max_value) AS vo2max_value,
              AVG(training_effect) AS training_effect
            FROM activities
            GROUP BY day
            ORDER BY day DESC
            LIMIT ?
        """, (days,)).fetchall()

    return [dict(r) for r in rows]

def build_dashboard_context(*, days: int, act_limit: int, activity_type: str, config: dict):
    """
    Eén plek die alle data voor dashboard.html bij elkaar zet.
    """
    latest = get_latest_daily_metrics()
    latest_hume = get_latest_hume_body()
    types = get_activity_types()

    deltas = calc_body_deltas(
        latest_hume,
        target_weight=config["TARGET_WEIGHT"],
        target_lean=config["TARGET_LEAN"],
        target_bf=config["TARGET_BF"],
        height_m=config["USER_HEIGHT"],
    )

    # activities filters bestaan al in de template,
    # maar je hebt nog geen activities tabel → veilig leeg.
    types = []

    return {
        "days": days,
        "act_limit": act_limit,
        "activity_type": activity_type,
        "types": types,

        "latest": latest,
        "latest_hume": latest_hume,

        **deltas,
    }

def get_hume_body_history(days: int = 180):
    conn = get_conn()
    rows = conn.execute("""
        SELECT day, weight_kg, lean_mass_kg, body_fat_pct,
               muscle_mass_kg, visceral_fat_index, body_water_pct, body_cell_mass_kg
        FROM hume_body
        ORDER BY day DESC
        LIMIT ?
    """, (days,)).fetchall()
    return [dict(r) for r in rows]

# models.py (onderaan toevoegen)
from database import get_conn

def _table_exists(conn, name: str) -> bool:
    r = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,)
    ).fetchone()
    return r is not None

def _columns(conn, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}

# ------------------------
# HUME
# ------------------------

def get_hume_history(days: int = 180):
    conn = get_conn()
    if not _table_exists(conn, "hume_body"):
        return []

    rows = conn.execute("""
        SELECT day, weight_kg, lean_mass_kg, body_fat_pct,
               muscle_mass_kg, visceral_fat_index, body_water_pct, body_cell_mass_kg
        FROM hume_body
        ORDER BY day DESC
        LIMIT ?
    """, (days,)).fetchall()
    return [dict(r) for r in rows]

# ------------------------
# FITATU (weekly)
# ------------------------

def get_fitatu_weekly(days: int = 365, limit_weeks: int = 104):
    """
    Output: [{week_start, calories, protein_g, carbs_g, fat_g, fiber_g}]
    """
    days = max(7, int(days))
    limit_weeks = max(4, int(limit_weeks))

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
              date(day, '-' || ((strftime('%w', day) + 6) % 7) || ' days') AS week_start,
              COALESCE(SUM(calories), 0)  AS calories,
              COALESCE(SUM(protein_g), 0) AS protein_g,
              COALESCE(SUM(carbs_g), 0)   AS carbs_g,
              COALESCE(SUM(fat_g), 0)     AS fat_g,
              COALESCE(SUM(fiber_g), 0)   AS fiber_g
            FROM fitatu_daily
            WHERE day IS NOT NULL
              AND date(day) >= date('now', ?)
            GROUP BY week_start
            ORDER BY week_start DESC
            LIMIT ?
        """, (f"-{days} day", limit_weeks)).fetchall()

    return [dict(r) for r in rows]

# ------------------------
# ACTIVITIES (Strava)
# ------------------------

def get_activities(limit: int = 25, activity_type: str = "all"):
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

    return [dict(r) for r in rows]



# ------------------------
# PRO ANALYSIS (weekly)
# ------------------------

def get_pro_analysis(days: int = 365):
    # als je dashboard.js pro_analysis gebruikt als weekly chart data:
    return get_pro_weekly_analysis(days=days)

def get_pro_weekly_analysis(days: int = 365):
    conn = get_conn()

    # Daily metrics weekly
    dm = conn.execute("""
        SELECT
          date(day, '-' || ((strftime('%w',day) + 6) % 7) || ' days') AS week_start,
          AVG(sleep_score) AS sleep_score_avg,
          AVG(hrv_rmssd)   AS hrv_avg,
          AVG(avg_stress)  AS stress_avg,
          AVG(resting_hr)  AS rhr_avg,
          MAX(body_battery_high) AS bb_high_max,
          MIN(body_battery_low)  AS bb_low_min,
          COUNT(*) AS dm_days
        FROM daily_metrics
        GROUP BY week_start
    """).fetchall()
    dm_map = {r["week_start"]: dict(r) for r in dm}

    # Fitatu weekly
    ft = conn.execute("""
        SELECT
          date(day, '-' || ((strftime('%w',day) + 6) % 7) || ' days') AS week_start,
          SUM(calories)  AS calories,
          SUM(protein_g) AS protein_g,
          SUM(carbs_g)   AS carbs_g,
          SUM(fat_g)     AS fat_g
        FROM fitatu_daily
        GROUP BY week_start
    """).fetchall()
    ft_map = {r["week_start"]: dict(r) for r in ft}

    # Activities weekly (training effect avg + distance + duration)
    act = conn.execute("""
        SELECT
          date(date(start_time_local), '-' || ((strftime('%w',date(start_time_local)) + 6) % 7) || ' days') AS week_start,
          COUNT(*) AS acts,
          SUM(distance_m)/1000.0 AS distance_km,
          SUM(duration_s)/3600.0 AS duration_h,
          AVG(training_effect) AS training_effect_avg,
          AVG(vo2max_value) AS vo2max_avg
        FROM activities
        GROUP BY week_start
    """).fetchall()
    act_map = {r["week_start"]: dict(r) for r in act}

    # Strava weekly TCL (load)
    tcl = conn.execute("""
        SELECT
          date(date(start_time_local), '-' || ((strftime('%w',date(start_time_local)) + 6) % 7) || ' days') AS week_start,
          SUM(tcl) AS tcl_sum,
          AVG(tcl) AS tcl_avg
        FROM strava_activities
        GROUP BY week_start
    """).fetchall()
    tcl_map = {r["week_start"]: dict(r) for r in tcl}

    # Hume weekly body
    hb = conn.execute("""
        SELECT
          date(day, '-' || ((strftime('%w',day) + 6) % 7) || ' days') AS week_start,
          AVG(weight_kg) AS weight_avg,
          AVG(lean_mass_kg) AS lean_avg,
          AVG(body_fat_pct) AS bf_avg
        FROM hume_body
        GROUP BY week_start
    """).fetchall()
    hb_map = {r["week_start"]: dict(r) for r in hb}

    # union of keys
    weeks = sorted(set(dm_map) | set(ft_map) | set(act_map) | set(tcl_map) | set(hb_map), reverse=True)

    out = []
    for w in weeks[:104]:
        row = {"week_start": w}
        row.update(dm_map.get(w, {}))
        row.update(ft_map.get(w, {}))
        row.update(act_map.get(w, {}))
        row.update(tcl_map.get(w, {}))
        row.update(hb_map.get(w, {}))
        out.append(row)

    return out

def get_pro_analysis(days: int = 365):
    # als je dashboard.js pro_analysis gebruikt als weekly chart data:
    return get_pro_weekly_analysis(days=days)