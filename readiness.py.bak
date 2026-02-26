# readiness.py

# ------------------------
# HELPER
# ------------------------

def clamp(v, lo=0, hi=100):

    return max(lo, min(hi, v))


# ------------------------
# READINESS SCORE
# ------------------------

def readiness_score(latest: dict | None):

    latest = latest or {}

    hrv = latest.get("hrv_rmssd")
    sleep = latest.get("sleep_score")
    stress = latest.get("avg_stress")
    bb = latest.get("body_battery_high")

    hrv_score = clamp((hrv - 20) * 1.5) if hrv is not None else 50

    sleep_score = sleep if sleep is not None else 50

    stress_score = clamp(100 - stress * 2) if stress is not None else 50

    bb_score = bb if bb is not None else 50


    score = round(

        0.35 * hrv_score +
        0.35 * sleep_score +
        0.15 * stress_score +
        0.15 * bb_score

    )


    label = (

        "Good" if score >= 75 else
        "OK" if score >= 50 else
        "Low"

    )


    return {

        "score": score,

        "label": label,

        "components": {

            "hrv": round(hrv_score),
            "sleep": round(sleep_score),
            "stress": round(stress_score),
            "bb": round(bb_score),

            # nodig voor template
            "fuel": None

        },

        # nodig voor template
        "kcal": None,
        "kcal_target": None

    }