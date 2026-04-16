"""
FlipFinder — Facebook Login Setup
Run this ONCE to log into Facebook and save your session.

    python setup_fb.py

After logging in and pressing Enter, the session is saved to auth_state.json
and all future searches will work headlessly without logging in again.
"""

import os
from playwright.sync_api import sync_playwright

AUTH_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth_state.json")


def main():
    print("\n  FlipFinder — Facebook Setup")
    print("  ─────────────────────────────────────────────")
    print("  A browser window will open.")
    print("  Log into Facebook, then come back here and press Enter.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=100)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.goto("https://www.facebook.com/login")

        input("  [After logging in press Enter here] > ")

        context.storage_state(path=AUTH_STATE)
        browser.close()

    print(f"\n  Session saved → auth_state.json")
    print("  You can now run: python server.py\n")


if __name__ == "__main__":
    main()
