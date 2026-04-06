#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import platform
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

try:
    import importlib.metadata as importlib_metadata
except Exception:  # pragma: no cover
    import importlib_metadata  # type: ignore


def print_header(title: str) -> None:
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)


def mask(value: str | None, keep: int = 3) -> str:
    if not value:
        return "(empty)"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep * 2) + value[-keep:]


def file_info(path: Path) -> dict:
    if not path.exists():
        return {"exists": False}
    stat = path.stat()
    return {
        "exists": True,
        "size": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
    }


def dump_environment() -> None:
    print_header("Environment")
    print(f"Python         : {sys.version}")
    print(f"Executable     : {sys.executable}")
    print(f"Platform       : {platform.platform()}")
    print(f"CWD            : {os.getcwd()}")
    print(f"GARMIN_EMAIL   : {mask(os.getenv('GARMIN_EMAIL'))}")
    print(f"GARMIN_PASSWORD: {'set' if os.getenv('GARMIN_PASSWORD') else 'missing'}")
    print(f"GARMIN_TOKENDIR: {os.getenv('GARMIN_TOKENDIR', '~/.garminconnect')}")


def dump_packages() -> None:
    print_header("Packages")
    for pkg in ["garminconnect", "curl_cffi", "ua-generator", "requests", "garth"]:
        try:
            ver = importlib_metadata.version(pkg)
        except Exception:
            ver = "(not installed)"
        print(f"{pkg:14}: {ver}")


def resolve_token_dir() -> Path:
    raw = os.getenv("GARMIN_TOKENDIR", "~/.garminconnect")
    return Path(raw).expanduser().resolve()


def read_token_file(token_dir: Path) -> None:
    print_header("Token file")
    token_file = token_dir / "garmin_tokens.json"
    print(f"Token dir  : {token_dir}")
    print(f"Token file : {token_file}")
    print(f"Info       : {json.dumps(file_info(token_file), indent=2)}")

    if not token_file.exists():
        return

    try:
        data = json.loads(token_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            print("Top-level keys:", sorted(data.keys()))
        else:
            print(f"Unexpected token file type: {type(data).__name__}")
    except Exception as exc:
        print(f"Could not parse token file: {exc}")


def prompt_mfa() -> str:
    return input("Garmin MFA code: ").strip()


def try_login_and_probe(email: str, password: str, token_dir: Path) -> int:
    print_header("Garmin native auth test")

    try:
        from garminconnect import Garmin
    except Exception as exc:
        print(f"Import failed: {exc}")
        traceback.print_exc()
        return 2

    try:
        print("Creating Garmin client...")
        client = Garmin(
            email=email,
            password=password,
            prompt_mfa=prompt_mfa,
        )

        print(f"Logging in with token dir: {token_dir}")
        client.login(str(token_dir))
        print("Login OK")

        today = date.today().isoformat()

        print_header("Basic profile checks")

        try:
            full_name = client.get_full_name()
            print(f"Full name: {full_name}")
        except Exception as exc:
            print(f"get_full_name failed: {exc.__class__.__name__}: {exc}")

        for fn_name in [
            "get_user_summary",
            "get_stats",
            "get_body_composition",
            "get_training_readiness",
            "get_hrv_data",
        ]:
            if not hasattr(client, fn_name):
                continue

            try:
                fn = getattr(client, fn_name)
                result = fn(today)
                print(f"{fn_name}({today}) OK -> {type(result).__name__}")

                if isinstance(result, dict):
                    print("keys:", sorted(result.keys())[:20])
                elif isinstance(result, list):
                    print(f"list length: {len(result)}")
                break
            except Exception as exc:
                print(f"{fn_name}({today}) failed: {exc.__class__.__name__}: {exc}")

        print_header("Token file after login")
        read_token_file(token_dir)

        return 0

    except Exception as exc:
        print(f"Login failed: {exc.__class__.__name__}: {exc}")
        traceback.print_exc()

        token_file = token_dir / "garmin_tokens.json"
        if token_file.exists():
            print("\nA token file exists, but login still failed.")
            print("That can mean the refresh token is expired or invalid.")
        else:
            print("\nNo token file was created.")
            print("That usually means auth failed before token exchange completed.")

        return 1


def main() -> int:
    dump_environment()
    dump_packages()

    email = (os.getenv("GARMIN_EMAIL") or "").strip()
    password = (os.getenv("GARMIN_PASSWORD") or "").strip()
    token_dir = resolve_token_dir()

    read_token_file(token_dir)

    if not email or not password:
        print_header("Missing credentials")
        print("Set GARMIN_EMAIL and GARMIN_PASSWORD in the shell first.")
        return 2

    token_dir.mkdir(parents=True, exist_ok=True)
    return try_login_and_probe(email, password, token_dir)


if __name__ == "__main__":
    raise SystemExit(main())