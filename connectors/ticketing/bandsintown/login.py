"""
One-time (or occasional) manual login for the Bandsintown connector.

Run this by hand whenever connector.py reports the saved session as
expired or missing. Opens a REAL, visible browser window using the same
persistent profile directory the connector reads from (bit_profile/, next
to this file) — log in once by hand, and the connector will reuse that
session on every future automated run.

This never asks for or stores your password directly; what gets saved is
Bandsintown's session cookies, inside the browser profile itself.
bit_profile/ is gitignored — treat it like a password.

Run:
    python connectors/ticketing/bandsintown/login.py
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE_DIR = Path(__file__).parent / "bit_profile"
LOGIN_URL = "https://www.bandsintown.com/?came_from=257"


def dismiss_cookies(page):
    for sel in (
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept All')",
        "button:has-text('Accept')",
    ):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2500):
                btn.click()
                page.wait_for_timeout(600)
                return
        except Exception:
            continue


def is_logged_in(page):
    try:
        for sel in ("text=/^Log In$/", "text=/^Sign Up$/"):
            if page.locator(sel).first.is_visible(timeout=1500):
                return False
    except Exception:
        pass
    return True


def main():
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        try:
            # channel="chrome" (your real Chrome install) is what makes
            # Google's login accept an automated browser at all — its
            # bundled Chromium is flagged as "not secure" for OAuth.
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
                channel="chrome",
                locale="en-US",
                args=["--start-maximized"],
                no_viewport=True,
            )
        except Exception as error:
            print(
                f"Real Chrome unavailable ({error.__class__.__name__}); "
                "falling back to bundled Chromium (Google sign-in may refuse it)."
            )
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
                locale="en-US",
                args=["--start-maximized"],
                no_viewport=True,
            )

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
        dismiss_cookies(page)

        print("=" * 70)
        print("  LOG IN IN THE BROWSER WINDOW (however you normally sign in).")
        print("  Take as long as you need — nothing here is timed.")
        print("=" * 70)
        input("\nPress Enter once you are logged in... ")

        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2000)
        dismiss_cookies(page)

        if is_logged_in(page):
            print(f"\nLogged in — session saved to {PROFILE_DIR}")
        else:
            print("\nWARNING: still seeing 'Log In' — it may not have taken. Try again.")

        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
