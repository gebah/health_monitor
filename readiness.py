#!/usr/bin/env python3
from __future__ import annotations

from datetime import date, timedelta
from math import exp
from typing import Iterable


# ------------------------
# Algemene helpers
# ------------------------

def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


# ------------------------
# Legacy wellness / Garmin-style helpers
# Deze laat ik staan voor compatibiliteit met bestaande UI/code.
# ------------------------

def score_hrv(latest_hrv, baseline_hrv=None):
    if latest_hrv is None:
        return None

    if baseline_hrv and baseline_hrv > 0:
        ratio = latest_hrv / baseline_hrv
        score = 100 * (ratio - 0.5) / 0.5
        return round(clamp(score))

    if latest_hrv >= 80:
        return 100
    if latest_hrv >= 70:
        return 85
    if latest_hrv >= 60:
        return 70
    if latest_hrv >= 50:
        return 55
    if latest_hrv >= 40:
        return 40
    return 25


def score_sleep(sleep_score):
    if sleep_score is None:
        return None
    return round(clamp(float(sleep_score)))


def score_stress(avg_stress):
    if avg_stress is None:
        return None

    if avg_stress <= 15:
        return 100
    if avg_stress <= 20:
        return 85
    if avg_stress <= 25:
        return 70
    if avg_stress <= 30:
        return 55
    if avg_stress <= 35:
        return 40
    return 25


def score_body_battery(body_battery_high):
    if body_battery_high is None:
        return None

    if body_battery_high >= 90:
        return 100
    if body_battery_high >= 80:
        return 90
    if body_battery_high >= 70:
        return 75
    if body_battery_high >= 60:
        return 60
    if body_battery_high >= 50:
        return 45
    return 30


def score_resting_hr(resting_hr, baseline_rhr=None):
    if resting_hr is None:
        return None

    if baseline_rhr and baseline_rhr > 0:
        delta = resting_hr - baseline_rhr
        if delta <= 0:
            return 100
        if delta <= 2:
            return 85
        if delta <= 4:
            return 70
        if delta <= 6:
            return 50
        if delta <= 8:
            return 35
        return 20

    if resting_hr <= 50:
        return 95
    if resting_hr <= 55:
        return 85
    if resting_hr <= 60:
        return 70
    if resting_hr <= 65:
        return 55
    return 35


def score_tcl_status(tcl_status):
    if not tcl_status:
        return None

    s = str(tcl_status).strip().lower()
    mapping = {
        "fresh": 95,
        "balanced": 85,
        "productive": 85,
        "loaded": 65,
        "fatigued": 40,
        "very_fatigued": 20,
        "vermoeid": 40,
        "zeer_vermoeid": 20,
    }
    return mapping.get(s, 60)


def weighted_average(parts):
    valid = [(score, weight) for score, weight in parts if score is not None]
    if not valid:
        return None

    total_weight = sum(weight for _, weight in valid)
    if total_weight == 0:
        return None

    value = sum(score * weight for score, weight in valid) / total_weight
    return round(value)


def classify_recovery(score, latest_hrv=None, baseline_hrv=None, tcl_status=None):
    if score is None:
        return "unknown", ["Onvoldoende data"]

    reasons = []

    if latest_hrv and baseline_hrv and baseline_hrv > 0:
        ratio = latest_hrv / baseline_hrv
        if ratio < 0.80:
            reasons.append("HRV duidelijk onder baseline")
            return "rest", reasons
        if ratio < 0.90:
            reasons.append("HRV onder baseline")
            if score > 55:
                score = 55

    if tcl_status and str(tcl_status).strip().lower() in {"very_fatigued", "zeer_vermoeid"}:
        reasons.append("TCL zeer vermoeid")
        return "rest", reasons

    if tcl_status and str(tcl_status).strip().lower() in {"fatigued", "vermoeid"}:
        reasons.append("TCL vermoeid")
        if score > 65:
            score = 65

    if score >= 75:
        return "train", reasons
    if score >= 60:
        return "easy", reasons
    if score >= 40:
        return "walk", reasons
    return "rest", reasons


def calculate_recovery_gauge(
    latest_hrv=None,
    baseline_hrv=None,
    sleep_score=None,
    avg_stress=None,
    body_battery_high=None,
    resting_hr=None,
    baseline_rhr=None,
    tcl_status=None,
):
    hrv_s = score_hrv(latest_hrv, baseline_hrv)
    sleep_s = score_sleep(sleep_score)
    stress_s = score_stress(avg_stress)
    bb_s = score_body_battery(body_battery_high)
    rhr_s = score_resting_hr(resting_hr, baseline_rhr)
    tcl_s = score_tcl_status(tcl_status)

    score = weighted_average([
        (hrv_s, 0.30),
        (sleep_s, 0.25),
        (stress_s, 0.20),
        (bb_s, 0.10),
        (rhr_s, 0.05),
        (tcl_s, 0.10),
    ])

    state, reasons = classify_recovery(
        score=score,
        latest_hrv=latest_hrv,
        baseline_hrv=baseline_hrv,
        tcl_status=tcl_status,
    )

    advice_map = {
        "train": "Klaar voor training",
        "easy": "Alleen lichte training",
        "walk": "Wandelen / herstel",
        "rest": "Rust houden",
        "unknown": "Onvoldoende data",
    }

    return {
        "score": score,
        "state": state,
        "label": advice_map[state],
        "reasons": reasons,
        "components": {
            "hrv": hrv_s,
            "sleep": sleep_s,
            "stress": stress_s,
            "body_battery": bb_s,
            "resting_hr": rhr_s,
            "tcl": tcl_s,
        }
    }


def calculate_wellness_readiness(latest: dict | None):
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
            "fuel": None,
        },
        "kcal": None,
        "kcal_target": None,
    }


# ------------------------
# Nieuwe Strava/TCL-based readiness logica
# ------------------------

def recovery_gauge_from_form(form: float) -> int:
    score = 50 + form * 2.2
    return max(1, min(100, round(score)))


def readiness_from_load(form: float, fatigue: float, fitness: float, last2d_load: float) -> int:
    score = 65

    score += max(-25, min(25, form * 1.5))

    if fitness > 0:
        ratio = last2d_load / fitness
        if ratio > 1.8:
            score -= 12
        elif ratio > 1.3:
            score -= 7
        elif ratio < 0.4:
            score += 4

    if fitness > 0:
        if fatigue > fitness * 1.20:
            score -= 8
        elif fatigue < fitness * 0.85:
            score += 4

    return max(1, min(100, round(score)))


def ewma_value(loads: list[float], tau: float) -> float:
    total = 0.0
    for i, load in enumerate(loads):
        age = len(loads) - 1 - i
        total += float(load) * exp(-age / tau)
    return total


def normalize_daily_loads(
    daily_loads: Iterable[tuple[date, float]]
) -> list[tuple[date, float]]:
    data = sorted((d, float(load or 0.0)) for d, load in daily_loads)
    if not data:
        return []

    start = data[0][0]
    end = data[-1][0]
    by_day = {d: load for d, load in data}

    out = []
    cur = start
    while cur <= end:
        out.append((cur, by_day.get(cur, 0.0)))
        cur += timedelta(days=1)

    return out


def compute_strava_readiness_series(
    daily_loads: Iterable[tuple[date, float]]
) -> list[dict]:
    series = normalize_daily_loads(daily_loads)
    if not series:
        return []

    out: list[dict] = []

    for idx in range(len(series)):
        window = series[: idx + 1]
        loads_only = [load for _, load in window]

        fatigue = ewma_value(loads_only, tau=7)
        fitness = ewma_value(loads_only, tau=42)
        form = fitness - fatigue
        last2d_load = sum(load for _, load in window[-2:])

        out.append({
            "day": window[-1][0].isoformat(),
            "daily_load": round(window[-1][1], 1),
            "fatigue": round(fatigue, 1),
            "fitness": round(fitness, 1),
            "form": round(form, 1),
            "readiness_score": readiness_from_load(form, fatigue, fitness, last2d_load),
            "recovery_gauge": recovery_gauge_from_form(form),
            "source": "strava",
        })

    return out


def compute_strava_readiness_today(
    daily_loads: Iterable[tuple[date, float]]
) -> dict | None:
    series = compute_strava_readiness_series(daily_loads)
    return series[-1] if series else None