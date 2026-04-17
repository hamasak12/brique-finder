"""
FlipFinder — Facebook Marketplace Scanner
Flask API server. Run with: python server.py

Requires one-time Facebook login:
    python setup_fb.py
"""

import functools
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from statistics import mean
from urllib.parse import quote_plus

from flask import Flask, jsonify, request
from flask_cors import CORS
from playwright.sync_api import sync_playwright

app = Flask(__name__)
CORS(app)

PORT           = 5001
DB_PATH        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fb_marketplace.db")
AUTH_STATE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth_state.json")
ADMIN_PASSWORD = os.environ.get("FF_ADMIN_PASSWORD", "admin")


# ── Database ───────────────────────────────────────────────────────────────────

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    with db_connect() as conn:
        # Wipe stale schema if needed
        try:
            conn.execute("SELECT price FROM listings LIMIT 1")
        except Exception:
            conn.executescript("DROP TABLE IF EXISTS listings; DROP TABLE IF EXISTS searches;")

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS searches (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                query       TEXT,
                searched_at TEXT
            );
            CREATE TABLE IF NOT EXISTS listings (
                id              TEXT PRIMARY KEY,
                name            TEXT,
                price           REAL,
                photo_url       TEXT,
                item_url        TEXT,
                seller_name     TEXT,
                seller_location TEXT,
                search_id       INTEGER REFERENCES searches(id),
                found_at        TEXT
            );
            CREATE TABLE IF NOT EXISTS feedback (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id    TEXT,
                listing_name  TEXT,
                listing_price REAL,
                vote          INTEGER,
                voted_at      TEXT
            );
            CREATE TABLE IF NOT EXISTS feed_keywords (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT UNIQUE,
                active  INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS price_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                query      TEXT,
                avg_price  REAL,
                recorded_at TEXT
            );
            CREATE TABLE IF NOT EXISTS keys (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                key          TEXT UNIQUE NOT NULL,
                label        TEXT,
                active       INTEGER DEFAULT 1,
                created_at   TEXT,
                last_used_at TEXT
            );
        """)
        # Migration: add key_label to searches if missing
        try:
            conn.execute("SELECT key_label FROM searches LIMIT 1")
        except Exception:
            conn.execute("ALTER TABLE searches ADD COLUMN key_label TEXT")
        conn.executescript("""
        """)


# ── Auth ──────────────────────────────────────────────────────────────────────

def require_key(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        key  = auth[7:] if auth.startswith("Bearer ") else request.headers.get("X-API-Key", "")
        if not key:
            return jsonify({"error": "API key required"}), 401
        with db_connect() as conn:
            row = conn.execute("SELECT id FROM keys WHERE key=? AND active=1", (key,)).fetchone()
        if not row:
            return jsonify({"error": "Invalid or revoked key"}), 401
        now = datetime.now(timezone.utc).isoformat()
        with db_connect() as conn:
            conn.execute("UPDATE keys SET last_used_at=? WHERE key=?", (now, key))
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        pwd = request.headers.get("X-Admin-Password", "")
        if pwd != ADMIN_PASSWORD:
            return jsonify({"error": "Admin access required"}), 403
        return f(*args, **kwargs)
    return decorated


# ── Scoring Algorithm ──────────────────────────────────────────────────────────

def compute_score(price: float, query_avg: float, feedback: list[dict]) -> float:
    """
    Score 0-100. Higher = better deal.

    Component 1 — Discount (0-60 pts):
        How much cheaper than the average price for this keyword.

    Component 2 — Preference fit (-20 to +40 pts):
        How similar this price is to liked items vs disliked items.
        Learns what price ranges the user actually buys.
    """
    # Component 1: discount from average
    if query_avg and query_avg > 0 and price < query_avg:
        discount_pct = (query_avg - price) / query_avg * 100
        discount_score = min(60.0, discount_pct * 1.5)
    else:
        discount_score = 0.0

    # Component 2: preference learning from feedback
    pref_score = 0.0
    likes    = [f for f in feedback if f["vote"] == 1]
    dislikes = [f for f in feedback if f["vote"] == -1]

    if likes:
        avg_liked = mean(f["listing_price"] for f in likes)
        if avg_liked > 0:
            similarity = max(0.0, 1.0 - abs(price - avg_liked) / avg_liked)
            pref_score += similarity * 40.0

    if dislikes:
        avg_disliked = mean(f["listing_price"] for f in dislikes)
        if avg_disliked > 0:
            similarity = max(0.0, 1.0 - abs(price - avg_disliked) / avg_disliked)
            pref_score -= similarity * 20.0

    total = discount_score + pref_score
    return max(0.0, min(100.0, round(total, 1)))


def get_feedback() -> list[dict]:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT listing_id, listing_price, vote FROM feedback ORDER BY voted_at DESC LIMIT 100"
        ).fetchall()
    return [dict(r) for r in rows]


def get_query_avg(query: str) -> float | None:
    """Return recent stored average price for a query, or None."""
    with db_connect() as conn:
        row = conn.execute(
            "SELECT avg_price FROM price_history WHERE query = ? ORDER BY recorded_at DESC LIMIT 1",
            (query,)
        ).fetchone()
    return row["avg_price"] if row else None


def save_query_avg(query: str, avg: float):
    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO price_history (query, avg_price, recorded_at) VALUES (?, ?, ?)",
            (query, avg, now)
        )


# ── Facebook Marketplace (Playwright) ─────────────────────────────────────────

def _parse_fb_data(text: str) -> list[dict]:
    results = []
    stripped = text.strip()
    if stripped.startswith("for (;;);"):
        stripped = stripped[9:]
    try:
        data = json.loads(stripped)
    except Exception:
        return results

    def walk(obj):
        if isinstance(obj, dict):
            if "marketplace_listing_title" in obj and "listing_price" in obj:
                try:
                    pi        = obj.get("listing_price") or {}
                    raw_amt   = pi.get("amount") or pi.get("formatted_amount", "0")
                    price     = float(re.sub(r"[^\d.]", "", str(raw_amt)) or "0")
                    photo     = obj.get("primary_listing_photo") or {}
                    img       = photo.get("listing_image") or photo.get("image") or {}
                    seller    = obj.get("marketplace_listing_seller") or {}
                    loc       = obj.get("location_text") or {}
                    item_id   = str(obj.get("id", ""))
                    if item_id and price > 0:
                        results.append({
                            "id":              item_id,
                            "name":            obj.get("marketplace_listing_title", ""),
                            "price":           price,
                            "photo_url":       img.get("uri", ""),
                            "item_url":        f"https://www.facebook.com/marketplace/item/{item_id}/",
                            "seller_name":     seller.get("name", ""),
                            "seller_location": loc.get("text", ""),
                        })
                except Exception:
                    pass
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data)
    return results


def fb_search(query: str) -> list[dict]:
    if not os.path.exists(AUTH_STATE):
        raise RuntimeError("Facebook session not found. Run: python setup_fb.py")

    pending = []

    def on_response(response):
        try:
            if response.status == 200 and ("graphql" in response.url or "api/graphql" in response.url):
                pending.append(response)
        except Exception:
            pass

    url = f"https://www.facebook.com/marketplace/curitiba/search/?query={quote_plus(query)}&exact=false"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            storage_state=AUTH_STATE,
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="pt-BR",
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = context.new_page()
        page.on("response", on_response)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=40_000)
            page.wait_for_timeout(5000)
            page.evaluate("window.scrollBy(0, 1200)")
            page.wait_for_timeout(3000)
        except Exception as e:
            print(f"[FB] page error: {e}")

        print(f"[FB] title: {page.title()!r}  graphql: {len(pending)}")

        captured = []
        for resp in pending:
            try:
                text = resp.body().decode("utf-8", errors="ignore")
                if "marketplace_listing_title" in text or "marketplace_search" in text:
                    captured.append(text)
            except Exception:
                pass

        print(f"[FB] marketplace responses: {len(captured)}")
        browser.close()

    all_listings: list[dict] = []
    for text in captured:
        all_listings.extend(_parse_fb_data(text))

    seen: set[str] = set()
    unique = []
    for item in all_listings:
        if item["id"] not in seen:
            seen.add(item["id"])
            unique.append(item)

    print(f"[FB] listings found: {len(unique)}")
    return unique


def _enrich(listings: list[dict], query: str) -> tuple[list[dict], float | None]:
    """Add score to each listing. Returns (enriched_listings, avg_price)."""
    prices = [l["price"] for l in listings if l["price"] > 0]
    if not prices:
        return listings, None

    query_avg = round(mean(prices), 2)

    # Prefer stored historical average if we have more data
    stored_avg = get_query_avg(query)
    effective_avg = stored_avg if stored_avg else query_avg
    save_query_avg(query, query_avg)

    feedback = get_feedback()

    enriched = []
    for listing in listings:
        score = compute_score(listing["price"], effective_avg, feedback)
        enriched.append({**listing, "score": score, "avg_price": effective_avg})

    enriched.sort(key=lambda x: x["score"], reverse=True)
    return enriched, effective_avg


# ── API Routes ─────────────────────────────────────────────────────────────────

@app.route("/api/search", methods=["POST"])
@require_key
def api_search():
    body  = request.get_json(force=True) or {}
    query = (body.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400

    try:
        listings = fb_search(query)
    except RuntimeError as e:
        return jsonify({"error": str(e), "setup_required": True}), 503
    except Exception as e:
        return jsonify({"error": f"Search error: {e}"}), 500

    if not listings:
        return jsonify({"error": "No listings found. Try a different keyword."}), 404

    enriched, avg = _enrich(listings, query)

    # Identify who made the search
    auth = request.headers.get("Authorization", "")
    api_key = auth[7:] if auth.startswith("Bearer ") else ""
    key_label = None
    if api_key:
        with db_connect() as conn:
            row = conn.execute("SELECT label FROM keys WHERE key=?", (api_key,)).fetchone()
            key_label = row["label"] if row else None

    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        cur = conn.execute("INSERT INTO searches (query, searched_at, key_label) VALUES (?, ?, ?)", (query, now, key_label))
        search_id = cur.lastrowid
        for item in enriched:
            conn.execute(
                "INSERT OR REPLACE INTO listings (id, name, price, photo_url, item_url, seller_name, seller_location, search_id, found_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (item["id"], item["name"], item["price"], item.get("photo_url",""), item.get("item_url",""), item.get("seller_name",""), item.get("seller_location",""), search_id, now)
            )

    return jsonify({"search_id": search_id, "query": query, "avg_price": avg, "count": len(enriched), "listings": enriched})


@app.route("/api/feedback", methods=["POST"])
@require_key
def api_feedback():
    body  = request.get_json(force=True) or {}
    lid   = (body.get("listing_id") or "").strip()
    name  = (body.get("listing_name") or "").strip()
    price = float(body.get("listing_price") or 0)
    vote  = int(body.get("vote") or 0)  # 1 = like, -1 = dislike

    if not lid or vote not in (1, -1):
        return jsonify({"error": "listing_id and vote (1 or -1) required"}), 400

    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        # Remove previous vote for same listing
        conn.execute("DELETE FROM feedback WHERE listing_id = ?", (lid,))
        conn.execute(
            "INSERT INTO feedback (listing_id, listing_name, listing_price, vote, voted_at) VALUES (?,?,?,?,?)",
            (lid, name, price, vote, now)
        )

    return jsonify({"ok": True})


@app.route("/api/feed", methods=["POST"])
@require_key
def api_feed():
    """Scan all active feed keywords and return scored results."""
    with db_connect() as conn:
        rows = conn.execute("SELECT keyword FROM feed_keywords WHERE active = 1").fetchall()
    keywords = [r["keyword"] for r in rows]

    if not keywords:
        return jsonify({"error": "No feed keywords set. Add some keywords first."}), 400

    all_results = []
    errors = []

    for kw in keywords:
        try:
            listings = fb_search(kw)
            enriched, _ = _enrich(listings, kw)
            for item in enriched:
                item["query"] = kw
            all_results.extend(enriched)
        except Exception as e:
            errors.append(f"{kw}: {e}")

    all_results.sort(key=lambda x: x["score"], reverse=True)

    return jsonify({"listings": all_results, "keywords_scanned": keywords, "errors": errors})


@app.route("/api/feed/keywords", methods=["GET"])
@require_key
def api_feed_keywords_get():
    with db_connect() as conn:
        rows = conn.execute("SELECT id, keyword, active FROM feed_keywords ORDER BY id").fetchall()
    return jsonify({"keywords": [dict(r) for r in rows]})


@app.route("/api/feed/keywords", methods=["POST"])
@require_key
def api_feed_keywords_add():
    body    = request.get_json(force=True) or {}
    keyword = (body.get("keyword") or "").strip().lower()
    if not keyword:
        return jsonify({"error": "keyword required"}), 400
    with db_connect() as conn:
        conn.execute("INSERT OR IGNORE INTO feed_keywords (keyword) VALUES (?)", (keyword,))
        conn.execute("UPDATE feed_keywords SET active=1 WHERE keyword=?", (keyword,))
    return jsonify({"ok": True})


@app.route("/api/feed/keywords/<int:kid>", methods=["DELETE"])
@require_key
def api_feed_keywords_delete(kid: int):
    with db_connect() as conn:
        conn.execute("DELETE FROM feed_keywords WHERE id = ?", (kid,))
    return jsonify({"ok": True})


@app.route("/api/history", methods=["GET"])
@require_key
def api_history():
    with db_connect() as conn:
        rows = conn.execute("""
            SELECT s.id, s.query, s.searched_at, s.key_label, COUNT(l.id) as listing_count
            FROM searches s LEFT JOIN listings l ON l.search_id = s.id
            GROUP BY s.id ORDER BY s.searched_at DESC LIMIT 50
        """).fetchall()
    return jsonify({"searches": [dict(r) for r in rows]})


@app.route("/api/search/<int:search_id>", methods=["GET"])
@require_key
def api_search_by_id(search_id: int):
    with db_connect() as conn:
        rows = conn.execute("SELECT * FROM listings WHERE search_id = ? ORDER BY price ASC", (search_id,)).fetchall()
    results = [dict(r) for r in rows]
    feedback = get_feedback()
    for r in results:
        avg = get_query_avg("") or r["price"]
        r["score"] = compute_score(r["price"], avg, feedback)
    return jsonify({"search_id": search_id, "listings": results})


@app.route("/api/top-picks", methods=["GET"])
@require_key
def api_top_picks():
    with db_connect() as conn:
        rows = conn.execute("SELECT l.*, s.query FROM listings l JOIN searches s ON s.id = l.search_id ORDER BY l.price ASC LIMIT 30").fetchall()
    results = [dict(r) for r in rows]
    feedback = get_feedback()
    for r in results:
        avg = get_query_avg(r.get("query","")) or r["price"]
        r["score"] = compute_score(r["price"], avg, feedback)
    results.sort(key=lambda x: x["score"], reverse=True)
    return jsonify({"listings": results})


@app.route("/api/auth/validate", methods=["POST"])
def api_auth_validate():
    body = request.get_json(force=True) or {}
    key  = (body.get("key") or "").strip()
    if not key:
        return jsonify({"valid": False}), 400
    with db_connect() as conn:
        row = conn.execute("SELECT label FROM keys WHERE key=? AND active=1", (key,)).fetchone()
    if not row:
        return jsonify({"valid": False}), 401
    return jsonify({"valid": True, "label": row["label"]})


@app.route("/api/admin/keys", methods=["GET"])
@require_admin
def admin_keys_list():
    with db_connect() as conn:
        rows = conn.execute("SELECT id, key, label, active, created_at, last_used_at FROM keys ORDER BY id DESC").fetchall()
    return jsonify({"keys": [dict(r) for r in rows]})


@app.route("/api/admin/keys", methods=["POST"])
@require_admin
def admin_keys_create():
    body  = request.get_json(force=True) or {}
    label = (body.get("label") or "").strip()
    key   = "bf_" + secrets.token_urlsafe(20)
    now   = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        conn.execute("INSERT INTO keys (key, label, active, created_at) VALUES (?,?,1,?)", (key, label, now))
    return jsonify({"key": key, "label": label})


@app.route("/api/admin/keys/<int:kid>", methods=["DELETE"])
@require_admin
def admin_keys_revoke(kid: int):
    with db_connect() as conn:
        conn.execute("UPDATE keys SET active=0 WHERE id=?", (kid,))
    return jsonify({"ok": True})


@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify({"fb_auth": os.path.exists(AUTH_STATE)})


@app.route("/api/ping", methods=["GET"])
def api_ping():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    db_init()
    if not os.path.exists(AUTH_STATE):
        print("\n  [!] Facebook session not found. Run: python setup_fb.py\n")
    else:
        print("\n  [ok] Facebook session ready.")

    # Auto-generate first key if none exist
    with db_connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM keys WHERE active=1").fetchone()[0]
        if count == 0:
            first_key = "bf_" + secrets.token_urlsafe(20)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute("INSERT INTO keys (key, label, active, created_at) VALUES (?,?,1,?)", (first_key, "owner", now))
            print(f"\n  [key] Your first access key (save this!):")
            print(f"        {first_key}\n")

    print(f"  Admin password: {ADMIN_PASSWORD}")
    print(f"  FlipFinder running at http://localhost:{PORT}\n")
    app.run(port=PORT, debug=False)
