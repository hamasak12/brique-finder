"""
Brique Finder — WhatsApp Setup
Run this ONCE to log into WhatsApp and find your group ID.

Steps:
    1. python setup_whatsapp.py
    2. Scan the QR code that appears with your phone
    3. Copy the group ID shown in the list
    4. Set the env var before running server.py:
           set FF_WHATSAPP_GROUP=<group_id>
           python server.py

Requires wacli installed:
    Download from https://github.com/steipete/wacli/releases
    Extract wacli.exe and place it somewhere on your PATH (e.g. C:\\Windows)
"""

import subprocess
import sys


def run(cmd: list[str]) -> int:
    try:
        result = subprocess.run(cmd, timeout=120)
        return result.returncode
    except FileNotFoundError:
        print("\n  [!] wacli not found.")
        print("      Download it from: https://github.com/steipete/wacli/releases")
        print("      Extract wacli.exe and place it in C:\\Windows or another folder on your PATH.\n")
        sys.exit(1)
    except KeyboardInterrupt:
        return 1


def main():
    print("\n  Brique Finder — WhatsApp Setup")
    print("  ─────────────────────────────────────────────")
    print("  Step 1: Log in to WhatsApp\n")
    print("  A QR code will appear below.")
    print("  Open WhatsApp on your phone → Linked Devices → Link a Device → scan.\n")

    code = run(["wacli", "login"])
    if code != 0:
        print("\n  [!] Login failed or was cancelled.\n")
        sys.exit(1)

    print("\n  [ok] Logged in!\n")
    print("  Step 2: Your WhatsApp groups\n")
    print("  Copy the ID of the group you want gem alerts sent to.\n")

    run(["wacli", "groups"])

    print("\n  ─────────────────────────────────────────────")
    print("  Step 3: Set the group ID before running the server\n")
    print("  Windows CMD:")
    print("      set FF_WHATSAPP_GROUP=<paste group ID here>")
    print("      python server.py\n")
    print("  Windows PowerShell:")
    print("      $env:FF_WHATSAPP_GROUP=\"<paste group ID here>\"")
    print("      python server.py\n")
    print("  Or set it permanently in System → Environment Variables.\n")


if __name__ == "__main__":
    main()
