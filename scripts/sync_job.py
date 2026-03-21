#!/usr/bin/env python3
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import time
import json
import sqlite3
import traceback
import logging
from datetime import datetime, timezone
from contextlib import contextmanager

# Linux-only lock (past bij jouw server)
import fcntl

# jouw collectors
from collector import sync_all, sync_garmin, sync_strava


# ------------------------
# Config
# ------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


DB_PATH = os.environ.get("HEALTH_DB") or os.environ.get("DB") or "garmin.sqlite"
LOCK_PATH = os.environ.get("HEALTH_SYNC_LOCK", "/tmp/health_monitor_sync.lock")


# ------------------------
# Logging
# ------------------------

def setup_logger() -> logging.Logger:
    logger = logging.getLogger("health_sync")
    logger.setLevel(logging.INFO)

    # systemd/journal haalt stdout op
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)
    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(fmt)

    # voorkom dubbele handlers bij herstart in dezelfde proc
    if not logger.handlers:
        logger.addHandler(handler)

    return logger


log = setup_logger()


# ------------------------
# Database (collector_runs)
# ------------------------

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_collector_runs(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collector_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collector TEXT,
            status TEXT,
            started_at TEXT,
            finished_at TEXT,
            message TEXT
        )
    """)
    conn.commit()


def insert_run(conn: sqlite3.Connection, collector: str, status: str, started_at: str, message: str = "") -> int:
    cur = conn.execute(
        """
        INSERT INTO collector_runs (collector, status, started_at, finished_at, message)
        VALUES (?, ?, ?, NULL, ?)
        """,
        (collector, status, started_at, message),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, finished_at: str, message: str = "") -> None:
    conn.execute(
        """
        UPDATE collector_runs
        SET status = ?, finished_at = ?, message = ?
        WHERE id = ?
        """,
        (status, finished_at, message, run_id),
    )
    conn.commit()


# ------------------------
# Locking
# ------------------------

@contextmanager
def single_instance_lock(lock_path: str):
    os.makedirs(os.path.dirname(lock_path), exist_ok=True) if "/" in lock_path else None
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError("LOCKED")

    try:
        # schrijf PID + timestamp (handig bij debug)
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()} started={utc_now_iso()}\n".encode())
        os.fsync(fd)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ------------------------
# Run target
# ------------------------

def run_target(target: str) -> None:
    if target == "all":
        sync_all()
    elif target == "garmin":
        sync_garmin()
    elif target == "strava":
        sync_strava()
    else:
        raise ValueError(f"Unknown target: {target}")


def main():
    target = (sys.argv[1] if len(sys.argv) > 1 else "all").strip().lower()
    started = utc_now_iso()

    # DB logging best effort: als DB niet bereikbaar is, willen we nog steeds loggen via stdout
    conn = None
    run_id = None

    try:
        with single_instance_lock(LOCK_PATH):
            log.info(f"Sync start target={target} db={DB_PATH}")

            try:
                conn = get_conn()
                ensure_collector_runs(conn)
                run_id = insert_run(conn, collector=f"sync_{target}", status="running", started_at=started)
            except Exception as e:
                log.warning(f"DB run logging disabled (cannot write collector_runs): {e}")

            t0 = time.time()

            try:
                run_target(target)
                dur = time.time() - t0
                finished = utc_now_iso()
                msg = f"OK in {dur:.1f}s"
                log.info(f"Sync OK target={target} duration={dur:.1f}s")

                if conn and run_id is not None:
                    finish_run(conn, run_id, status="success", finished_at=finished, message=msg)

                return 0

            except Exception as e:
                dur = time.time() - t0
                finished = utc_now_iso()
                tb = traceback.format_exc()
                short = f"FAILED after {dur:.1f}s: {tb.splitlines()[-1]}"

                if target == "garmin":
                    msg = f"Skipped Garmin sync due to auth/API issue: {e}"
                    log.warning(f"{msg}\n{tb}")

                    if conn and run_id is not None:
                        finish_run(conn, run_id, status="skipped", finished_at=finished, message=msg)

                    return 0

                log.error(f"Sync FAILED target={target} duration={dur:.1f}s\n{tb}")

                if conn and run_id is not None:
                    finish_run(conn, run_id, status="error", finished_at=finished, message=short)

                return 2

    except RuntimeError as e:
        # lock held → geen dubbele run
        if str(e) == "LOCKED":
            log.warning(f"Sync skipped (lock held) target={target}")
            try:
                conn = get_conn()
                ensure_collector_runs(conn)
                run_id = insert_run(conn, collector=f"sync_{target}", status="skipped", started_at=started,
                                    message="Skipped: another sync is running")
                finish_run(conn, run_id, status="skipped", finished_at=utc_now_iso(),
                           message="Skipped: another sync is running")
            except Exception:
                pass
            return 0
        raise
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())