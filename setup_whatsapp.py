"""
Brique Finder — WhatsApp Setup
Run this ONCE to log into WhatsApp Web and save your session.

    python setup_whatsapp.py

After scanning the QR code and pressing Enter, the session is saved to
whatsapp_auth.json and gem alerts will work automatically.

You also need to set the group name:
    set FF_WHATSAPP_GROUP=Nome do Grupo
    python server.py
"""

import os
from playwright.sync_api import sync_playwright

WA_AUTH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whatsapp_auth.json")


def main():
    print("\n  Brique Finder — WhatsApp Setup")
    print("  ─────────────────────────────────────────────")
    print("  A browser window will open.")
    print("  Scan the QR code with your phone, wait until WhatsApp loads,")
    print("  then come back here and press Enter.\n")

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
        page.goto("https://web.whatsapp.com")

        input("  [After WhatsApp loads fully, press Enter here] > ")

        context.storage_state(path=WA_AUTH)
        browser.close()

    print(f"\n  [ok] Session saved → whatsapp_auth.json")
    print("\n  Now find your group name (exactly as it appears in WhatsApp),")
    print("  then set it before running the server:\n")
    print("  Windows CMD:")
    print('      set FF_WHATSAPP_GROUP=Nome do Grupo')
    print("      python server.py\n")
    print("  Windows PowerShell:")
    print('      $env:FF_WHATSAPP_GROUP="Nome do Grupo"')
    print("      python server.py\n")


if __name__ == "__main__":
    main()
