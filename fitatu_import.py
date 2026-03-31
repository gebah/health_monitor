#!/usr/bin/env python3
from __future__ import annotations
from pathlib import Path

import json
import os
import sqlite3
from datetime import datetime

import pandas as pd


# Pas aan als je DB elders staat
DB_PATH = os.path.expanduser(
    "/home/gba/Documenten/PycharmProjects/health_monitor/health.sqlite"
)


# --- Fitatu NL kolomnamen uit jouw header ---
COL_DATE = "Datum"
COL_KCAL = "calorieën (kcal)"
COL_PROT = "Eiwitten (g)"
COL_CARB = "Koolhydraten (g)"
COL_FAT  = "Vetten (g)"
COL_FIB  = "Vezels (g)"
COL_SALT = "Zout (g)"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS fitatu_daily (
        day TEXT PRIMARY KEY,
        calories REAL,
        protein_g REAL,
        carbs_g REAL,
        fat_g REAL,
        fiber_g REAL,
        salt_g REAL,
        synced_at TEXT,
        raw_json TEXT
    )
    """)
    conn.commit()


def to_num_series(s: pd.Series) -> pd.Series:
    # Fitatu kan komma-decimaal gebruiken
    return pd.to_numeric(s.astype(str).str.replace(",", "."), errors="coerce")


def load_csv(path: str) -> pd.DataFrame:
    # Jouw export is comma-separated en quoted (zie header)
    # utf-8-sig vangt BOM af als die er is
    return pd.read_csv(path, sep=",", encoding="utf-8-sig", quotechar='"')


def main(path: str) -> None:
    stats = import_fitatu(path)
    print(f"Imported {stats['days']} days into fitatu_daily from {stats['file']}")
    print("DB:", stats["db"])


def import_fitatu(path: str) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"File not found: {path}")

    df = load_csv(path)

    missing = [c for c in (COL_DATE, COL_KCAL, COL_PROT, COL_CARB, COL_FAT, COL_FIB, COL_SALT) if c not in df.columns]
    if missing:
        raise ValueError(f"CSV columns do not match expected Fitatu export. Missing: {missing}. Found: {list(df.columns)}")

    s = df[COL_DATE].astype(str).str.strip().str.slice(0, 10)

    dt = pd.to_datetime(s, format="%d-%m-%Y", errors="coerce")
    dt = dt.fillna(pd.to_datetime(s, format="%d/%m/%Y", errors="coerce"))
    dt = dt.fillna(pd.to_datetime(s, format="%Y-%m-%d", errors="coerce"))

    df["day"] = dt.dt.strftime("%Y-%m-%d")
    # fallback: als het nóg niet lukt, neem de ruwe string
    df.loc[df["day"].isna(), "day"] = s

    df[COL_KCAL] = to_num_series(df[COL_KCAL])
    df[COL_PROT] = to_num_series(df[COL_PROT])
    df[COL_CARB] = to_num_series(df[COL_CARB])
    df[COL_FAT]  = to_num_series(df[COL_FAT])
    df[COL_FIB]  = to_num_series(df[COL_FIB])
    df[COL_SALT] = to_num_series(df[COL_SALT])

    daily = (
        df.groupby("day", as_index=False)[[COL_KCAL, COL_PROT, COL_CARB, COL_FAT, COL_FIB, COL_SALT]]
          .sum()
          .sort_values("day")
    )

    synced_at = datetime.now().isoformat(timespec="seconds")

    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")

        ensure_schema(conn)

        n = 0
        for _, r in daily.iterrows():
            day = str(r["day"])
            payload = {
                "day": day,
                "calories": None if pd.isna(r[COL_KCAL]) else float(r[COL_KCAL]),
                "protein_g": None if pd.isna(r[COL_PROT]) else float(r[COL_PROT]),
                "carbs_g": None if pd.isna(r[COL_CARB]) else float(r[COL_CARB]),
                "fat_g": None if pd.isna(r[COL_FAT]) else float(r[COL_FAT]),
                "fiber_g": None if pd.isna(r[COL_FIB]) else float(r[COL_FIB]),
                "salt_g": None if pd.isna(r[COL_SALT]) else float(r[COL_SALT]),
            }

            conn.execute("""
                INSERT OR REPLACE INTO fitatu_daily
                (day, calories, protein_g, carbs_g, fat_g, fiber_g, salt_g, synced_at, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                day,
                payload["calories"],
                payload["protein_g"],
                payload["carbs_g"],
                payload["fat_g"],
                payload["fiber_g"],
                payload["salt_g"],
                synced_at,
                json.dumps(payload, ensure_ascii=False),
            ))
            n += 1

        conn.commit()

    return {
        "days": n,
        "file": os.path.basename(path),
        "db": DB_PATH,
        "synced_at": synced_at,
        "min_day": str(daily["day"].min()) if len(daily) else None,
        "max_day": str(daily["day"].max()) if len(daily) else None,
    }


if __name__ == "__main__":
    main()