"""
Internal script — called by server.py to send a WhatsApp message.
Do not run this directly.

Usage: python _wa_sender.py "<group_name>" "<message>"
Exits 0 on success, 1 on failure (prints error to stderr).
"""

import os
import sys

AUTH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whatsapp_auth.json")


def main():
    if len(sys.argv) < 3:
        print("usage: _wa_sender.py <group> <message>", file=sys.stderr)
        sys.exit(1)

    group = sys.argv[1]
    msg   = sys.argv[2]

    if not os.path.exists(AUTH):
        print("WhatsApp session not found. Run: python setup_whatsapp.py", file=sys.stderr)
        sys.exit(1)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=AUTH)
        page    = context.new_page()

        page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")

        # Wait for chats to load — if this fails the session is expired
        try:
            page.wait_for_selector('[aria-label="Search input textbox"]', timeout=25_000)
        except Exception:
            browser.close()
            print("WhatsApp session expired. Run: python setup_whatsapp.py", file=sys.stderr)
            sys.exit(1)

        # Search for the group
        page.click('[aria-label="Search input textbox"]')
        page.wait_for_timeout(500)
        page.keyboard.type(group)
        page.wait_for_timeout(2500)

        # Click matching result
        result = page.locator(f'span[title="{group}"]').first
        try:
            result.wait_for(timeout=6000)
        except Exception:
            browser.close()
            print(f'Group "{group}" not found. Check FF_WHATSAPP_GROUP matches exactly.', file=sys.stderr)
            sys.exit(1)

        result.click()
        page.wait_for_timeout(1000)

        # Find message box
        msg_box = None
        for sel in ['[aria-label="Type a message"]', '[data-testid="compose-input"]',
                    'div[contenteditable="true"][data-tab="10"]']:
            try:
                page.wait_for_selector(sel, timeout=4000)
                msg_box = page.locator(sel).first
                break
            except Exception:
                continue

        if msg_box is None:
            browser.close()
            print("Could not find message input box.", file=sys.stderr)
            sys.exit(1)

        msg_box.click()
        page.keyboard.type(msg)
        page.keyboard.press("Enter")
        page.wait_for_timeout(2000)
        browser.close()

    print("ok")
    sys.exit(0)


if __name__ == "__main__":
    main()
