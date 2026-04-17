"""
Generate an access key for Brique Finder.

Usage:
    python gen_key.py
    python gen_key.py "Pedro"
"""

import os
import secrets
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fb_marketplace.db")


def main():
    label = sys.argv[1].strip() if len(sys.argv) > 1 else ""

    if not os.path.exists(DB_PATH):
        print("  [!] Database not found. Run python server.py first.")
        sys.exit(1)

    key = "bf_" + secrets.token_urlsafe(20)
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO keys (key, label, active, created_at) VALUES (?,?,1,?)",
        (key, label, now)
    )
    conn.commit()
    conn.close()

    print()
    print(f"  Key generated" + (f' for "{label}"' if label else "") + ":")
    print(f"  {key}")
    print()


if __name__ == "__main__":
    main()
