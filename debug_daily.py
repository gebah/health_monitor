#!/usr/bin/env python3
"""
Eenmalig uitvoeren om Garmin tokens op te slaan via een echte browser.
Werkt met garminconnect 0.3.2+
"""

import re
import time
from pathlib import Path
from playwright.sync_api import sync_playwright
from garminconnect import Garmin

TOKEN_DIR = str(Path.home() / ".garminconnect")

# Service URL moet overeenkomen met wat in de SSO-embed gebruikt wordt
SERVICE_URL = "https://sso.garmin.com/sso/embed"

SSO_URL = (
    "https://sso.garmin.com/sso/embed"
    "?id=gauth-widget&embedWidget=true"
    "&gauthHost=https://sso.garmin.com/sso"
    "&clientId=GarminConnect&locale=en_US"
    "&redirectAfterAccountLoginUrl=https://sso.garmin.com/sso/embed"
    "&redirectAfterAccountCreationUrl=https://sso.garmin.com/sso/embed"
)


def browser_login() -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_context().new_page()
        page.goto(SSO_URL)

        print("Log in met je Garmin-account in het browservenster...")

        page.wait_for_function(
            "document.body.innerText.includes('serviceTicket')",
            timeout=120000
        )

        content = page.inner_text("body")
        browser.close()

    match = re.search(r"""['"]?serviceTicket['"]?\s*:\s*['"]([^'"]+)['"]""", content)
    if not match:
        raise ValueError(f"Kon serviceTicket niet vinden in: {content}")

    ticket = match.group(1)
    print(f"Ticket ontvangen: {ticket[:20]}...")
    return ticket


def main():
    print("Browser openen voor login...")
    ticket = browser_login()

    print("Token uitwisselen via diauth.garmin.com...")
    api = Garmin()  # geen credentials nodig
    api.client._establish_session(ticket, service_url=SERVICE_URL)

    print("Tokens opslaan...")
    Path(TOKEN_DIR).mkdir(parents=True, exist_ok=True)
    api.client.dump(TOKEN_DIR)

    print(f"\nKlaar! Tokens opgeslagen in {TOKEN_DIR}")
    print("Je kunt nu je normale Garmin-script draaien.")


if __name__ == "__main__":
    main()