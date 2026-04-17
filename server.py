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
import threading
import urllib.request
from datetime import datetime, timezone
from statistics import mean, stdev
from urllib.parse import quote_plus

from flask import Flask, jsonify, request
from flask_cors import CORS
from playwright.sync_api import sync_playwright

app = Flask(__name__)
CORS(app)

PORT             = 5001
DATA_DIR         = os.environ.get("FF_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_PATH          = os.path.join(DATA_DIR, "fb_marketplace.db")
AUTH_STATE       = os.path.join(DATA_DIR, "auth_state.json")
ADMIN_PASSWORD   = os.environ.get("FF_ADMIN_PASSWORD", "admin")
TELEGRAM_TOKEN   = os.environ.get("FF_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT    = os.environ.get("FF_TELEGRAM_CHAT", "")
OLLAMA_URL       = os.environ.get("FF_OLLAMA_URL", "http://localhost:11434")


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
            CREATE TABLE IF NOT EXISTS notifications (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id TEXT UNIQUE,
                sent_at    TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        # Migrations
        try:
            conn.execute("SELECT key_label FROM searches LIMIT 1")
        except Exception:
            conn.execute("ALTER TABLE searches ADD COLUMN key_label TEXT")
        try:
            conn.execute("SELECT score FROM listings LIMIT 1")
        except Exception:
            conn.execute("ALTER TABLE listings ADD COLUMN score REAL")
        try:
            conn.execute("SELECT sold_at FROM listings LIMIT 1")
        except Exception:
            conn.execute("ALTER TABLE listings ADD COLUMN sold_at TEXT")
        try:
            conn.execute("SELECT photo_condition FROM listings LIMIT 1")
        except Exception:
            conn.execute("ALTER TABLE listings ADD COLUMN photo_condition TEXT")


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


# ── Defect Detection ──────────────────────────────────────────────────────────

BUYER_PREFIXES = (
    "compro ", "compro!", "compro,",
    "quero comprar", "procuro ", "busco ",
    "interesse em comprar", "compra ", "troco por",
    "alguem vende", "alguém vende",
    "vendido", "vendida",
)

DEFECT_KEYWORDS = [
    "defeito", "defeitos", "com defeito",
    "quebrado", "quebrada",
    "trincado", "trincada", "trinca",
    "rachado", "rachada", "racha",
    "não funciona", "nao funciona", "sem funcionar", "nao liga", "não liga",
    "bateria ruim", "bateria viciada", "bateria inchada",
    "tela trincada", "tela quebrada", "tela rachada",
    "com problema", "com problemas", "apresenta problema",
    "estragado", "estragada",
    "danificado", "danificada",
    "avariado", "avariada",
    "para retirada", "para conserto", "para reparo",
    "parado", "parada",
    "travando", "travado",
]

def detect_defects(name: str) -> list[str]:
    """Return list of defect keywords found in listing name."""
    name_lower = name.lower()
    return [kw for kw in DEFECT_KEYWORDS if kw in name_lower]


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
    """Return avg price for a query — prefers confirmed sold prices, falls back to historical."""
    with db_connect() as conn:
        row = conn.execute(
            """SELECT AVG(l.price) FROM listings l
               JOIN searches s ON l.search_id=s.id
               WHERE s.query=? AND l.sold_at IS NOT NULL AND l.price > 0""",
            (query,)
        ).fetchone()
        if row and row[0]:
            return row[0]
        row = conn.execute(
            "SELECT avg_price FROM price_history WHERE query=? ORDER BY recorded_at DESC LIMIT 1",
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


# ── Settings helpers ──────────────────────────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    with db_connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with db_connect() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))


# ── Photo Analysis (Ollama) ────────────────────────────────────────────────────

def analyze_photo(photo_url: str) -> dict:
    """Use local Ollama llama3.2-vision to assess listing photo condition."""
    if not photo_url:
        return {}
    try:
        import base64
        img_data = urllib.request.urlopen(photo_url, timeout=8).read()
        b64      = base64.standard_b64encode(img_data).decode()
        payload  = json.dumps({
            "model":  "llama3.2-vision",
            "prompt": (
                "Look at this product photo from a marketplace listing. "
                "Reply in JSON only, no explanation: "
                "{\"condition\": \"good|fair|poor\", "
                "\"issues\": [\"list visible damage\"], "
                "\"score_adjust\": <integer -20 to 5>}"
            ),
            "images": [b64],
            "stream": False,
        }).encode()
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate", data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        return json.loads(result["response"])
    except Exception as e:
        print(f"[Photo] analysis failed: {e}")
        return {}


# ── Sold Detection ─────────────────────────────────────────────────────────────

def mark_sold_listings(query: str, current_ids: list):
    """Mark previously seen listings that no longer appear as likely sold."""
    if not current_ids:
        return
    now          = datetime.now(timezone.utc).isoformat()
    placeholders = ",".join("?" * len(current_ids))
    with db_connect() as conn:
        rows = conn.execute(
            f"""SELECT l.id FROM listings l
                JOIN searches s ON l.search_id = s.id
                WHERE s.query=? AND l.sold_at IS NULL AND l.id NOT IN ({placeholders})""",
            [query] + current_ids
        ).fetchall()
        if rows:
            sold_ids = [r["id"] for r in rows]
            ph2      = ",".join("?" * len(sold_ids))
            conn.execute(f"UPDATE listings SET sold_at=? WHERE id IN ({ph2})", [now] + sold_ids)
            print(f"[Sold] {len(sold_ids)} listings marked sold for '{query}'")


# ── Telegram Alerts & Bot ─────────────────────────────────────────────────────

def dynamic_gem_threshold() -> float:
    """
    Adaptive threshold: mean + 1 stddev of the last 200 scored listings.
    Falls back to 70.0 if not enough data yet.
    """
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT score FROM listings WHERE score IS NOT NULL ORDER BY found_at DESC LIMIT 200"
        ).fetchall()
    scores = [r["score"] for r in rows if r["score"] is not None]
    if len(scores) < 10:
        return 70.0
    return max(70.0, round(mean(scores) + stdev(scores), 1))


def _tg_notify_session_expired():
    """Alert via Telegram that the Facebook session has expired."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    tg_api("sendMessage", chat_id=TELEGRAM_CHAT, parse_mode="HTML", text=(
        "⚠️ <b>Sessão do Facebook expirou!</b>\n\n"
        "Para reautenticar:\n"
        "1. Rode <code>python setup_fb.py</code> no seu PC\n"
        "2. Envie o arquivo <code>auth_state.json</code> aqui\n\n"
        "A sessão será atualizada imediatamente."
    ))


def notify_gem(listing: dict):
    """Send a Telegram alert for a gem listing. Skips duplicates."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return

    with db_connect() as conn:
        already = conn.execute(
            "SELECT 1 FROM notifications WHERE listing_id=?", (listing["id"],)
        ).fetchone()
    if already:
        return

    profit = listing.get("avg_price", 0) - listing["price"]
    item_url = listing.get("item_url", "")
    msg = (
        f"🔥 <b>Brique Finder — Gem encontrado!</b>\n\n"
        f"<a href=\"{item_url}\"><b>{_esc(listing['name'])}</b></a>\n"
        f"💰 R$ {listing['price']:.2f}  (média R$ {listing.get('avg_price', 0):.2f})\n"
        f"📈 Lucro est.: R$ {profit:.2f}  |  Score: {listing['score']}\n"
        f"📍 {_esc(listing.get('seller_location', ''))}"
    )

    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"}).encode()
    req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        if result.get("ok"):
            now = datetime.now(timezone.utc).isoformat()
            with db_connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO notifications (listing_id, sent_at) VALUES (?,?)",
                    (listing["id"], now)
                )
            print(f"[Telegram] gem sent: {listing['name']!r}  score={listing['score']}")
        else:
            print(f"[Telegram] API error: {result}")
    except Exception as e:
        print(f"[Telegram] failed: {e}")


def check_gems(enriched: list[dict]):
    """Check a list of enriched listings for gems and notify."""
    threshold = dynamic_gem_threshold()
    print(f"[Gem] threshold={threshold}")
    for item in enriched:
        if (item.get("score") or 0) >= threshold:
            notify_gem(item)


# ── Daily Summary ─────────────────────────────────────────────────────────────

def send_daily_summary():
    """Send a 24-hour digest to Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    from datetime import timedelta
    now   = datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat()
    with db_connect() as conn:
        gems = conn.execute(
            """SELECT l.name, l.price, l.score, l.item_url
               FROM notifications n JOIN listings l ON l.id=n.listing_id
               WHERE n.sent_at >= ? ORDER BY l.score DESC LIMIT 5""", (since,)
        ).fetchall()
        scans    = conn.execute("SELECT COUNT(*) FROM searches WHERE searched_at >= ?",  (since,)).fetchone()[0]
        new_list = conn.execute("SELECT COUNT(*) FROM listings WHERE found_at >= ?",     (since,)).fetchone()[0]
        sold     = conn.execute("SELECT COUNT(*) FROM listings WHERE sold_at >= ?",      (since,)).fetchone()[0]

    lines = [
        f"📅 <b>Resumo diário — {now.strftime('%d/%m/%Y')}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔎 Pesquisas <b>{scans}</b>  📦 Novos <b>{new_list}</b>  💸 Vendidos <b>{sold}</b>"
    ]
    if gems:
        lines.append("\n💎 <b>Gems das últimas 24h:</b>")
        for g in gems:
            lines.append(f"• <a href=\"{g['item_url']}\"><b>{_esc(g['name'])}</b></a>  R$ {g['price']:.2f}  Score {g['score']}")
    else:
        lines.append("\nNenhum gem nas últimas 24h.")
    tg_api("sendMessage", chat_id=TELEGRAM_CHAT, parse_mode="HTML", text="\n".join(lines))


def daily_summary_loop():
    """Background thread: sleep until configured hour, send summary, repeat."""
    import time
    from datetime import timedelta
    while True:
        hour   = int(get_setting("summary_hour", "8"))
        now    = datetime.now()
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        secs = (target - now).total_seconds()
        print(f"[Summary] next at {target.strftime('%d/%m %H:%M')} (in {secs/3600:.1f}h)")
        time.sleep(secs)
        send_daily_summary()


# ── Telegram Bot (Interactive HUD) ────────────────────────────────────────────

_tg_offset = 0  # long-poll cursor


def _esc(s: str) -> str:
    """HTML-escape text for Telegram HTML parse mode."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_HUD_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "💎 Gems",       "callback_data": "gems"},
         {"text": "📊 Stats",      "callback_data": "stats"}],
        [{"text": "🕒 Recentes",   "callback_data": "recent"},
         {"text": "📈 Threshold",  "callback_data": "threshold"}],
        [{"text": "🔑 Keywords",   "callback_data": "keywords"},
         {"text": "🔐 FB Auth",    "callback_data": "fbauth"}],
        [{"text": "📅 Resumo",     "callback_data": "summary"},
         {"text": "❓ Help",       "callback_data": "help"}],
        [{"text": "🔄 Refresh",    "callback_data": "refresh"},
         {"text": "✖ Fechar",     "callback_data": "close"}],
    ]
}

_BACK_KEYBOARD = {
    "inline_keyboard": [[{"text": "◀ Menu", "callback_data": "menu"}]]
}


def tg_api(method: str, **params) -> dict:
    """Call any Telegram Bot API method."""
    if not TELEGRAM_TOKEN:
        return {"ok": False}
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    data = json.dumps(params).encode()
    req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[Telegram] {method} error: {e}")
        return {"ok": False}


def _hud_text() -> str:
    with db_connect() as conn:
        total_scans    = conn.execute("SELECT COUNT(*) FROM searches").fetchone()[0]
        total_listings = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        gems_sent      = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        active_keys    = conn.execute("SELECT COUNT(*) FROM keys WHERE active=1").fetchone()[0]
    threshold = dynamic_gem_threshold()
    now = datetime.now(timezone.utc).strftime("%d/%m %H:%M UTC")
    return (
        f"🔍 <b>Brique Finder</b>  <i>{now}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔎 Pesquisas    <b>{total_scans}</b>\n"
        f"📦 Anúncios     <b>{total_listings}</b>\n"
        f"💎 Gems         <b>{gems_sent}</b>\n"
        f"🔑 Keys ativas  <b>{active_keys}</b>\n"
        f"📈 Threshold    <b>{threshold}</b>"
    )


def tg_send_hud(chat_id):
    tg_api("sendMessage", chat_id=chat_id, text=_hud_text(),
           parse_mode="HTML", reply_markup=_HUD_KEYBOARD)


def tg_edit(chat_id, message_id: int, text: str, back: bool = False):
    tg_api("editMessageText", chat_id=chat_id, message_id=message_id,
           text=text, parse_mode="HTML",
           reply_markup=_BACK_KEYBOARD if back else _HUD_KEYBOARD)


def tg_handle_callback(query: dict):
    data       = query.get("data", "")
    chat_id    = query["message"]["chat"]["id"]
    message_id = query["message"]["message_id"]
    tg_api("answerCallbackQuery", callback_query_id=query["id"])

    if data in ("menu", "refresh"):
        tg_edit(chat_id, message_id, _hud_text())

    elif data == "close":
        tg_api("deleteMessage", chat_id=chat_id, message_id=message_id)

    elif data == "gems":
        with db_connect() as conn:
            rows = conn.execute(
                """SELECT l.name, l.price, l.score, l.item_url, n.sent_at
                   FROM notifications n
                   JOIN listings l ON l.id = n.listing_id
                   ORDER BY n.id DESC LIMIT 5"""
            ).fetchall()
        lines = ["💎 <b>Últimos gems</b>\n━━━━━━━━━━━━━━━━━━━━"]
        if not rows:
            lines.append("Nenhum gem encontrado ainda.")
        for r in rows:
            t = (r["sent_at"] or "")[:16].replace("T", " ")
            lines.append(
                f"\n• <a href=\"{r['item_url']}\"><b>{_esc(r['name'])}</b></a>\n"
                f"  💰 R$ {r['price']:.2f}  |  Score {r['score']}  <i>{t}</i>"
            )
        tg_edit(chat_id, message_id, "\n".join(lines), back=True)

    elif data == "stats":
        with db_connect() as conn:
            recent = conn.execute(
                "SELECT query, searched_at, key_label FROM searches ORDER BY id DESC LIMIT 8"
            ).fetchall()
        lines = ["📊 <b>Últimas pesquisas</b>\n━━━━━━━━━━━━━━━━━━━━"]
        for r in recent:
            t = (r["searched_at"] or "")[:16].replace("T", " ")
            who = f" <i>({_esc(r['key_label'])})</i>" if r["key_label"] else ""
            lines.append(f"• {_esc(r['query'])}{who}  <i>{t}</i>")
        tg_edit(chat_id, message_id, "\n".join(lines), back=True)

    elif data == "recent":
        with db_connect() as conn:
            rows = conn.execute(
                """SELECT name, price, score, item_url
                   FROM listings WHERE score IS NOT NULL
                   ORDER BY score DESC, found_at DESC LIMIT 6"""
            ).fetchall()
        lines = ["🕒 <b>Top anúncios (score)</b>\n━━━━━━━━━━━━━━━━━━━━"]
        for r in rows:
            lines.append(
                f"\n• <a href=\"{r['item_url']}\"><b>{_esc(r['name'])}</b></a>\n"
                f"  💰 R$ {r['price']:.2f}  |  Score {r['score']}"
            )
        tg_edit(chat_id, message_id, "\n".join(lines), back=True)

    elif data == "threshold":
        threshold = dynamic_gem_threshold()
        with db_connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM listings WHERE score IS NOT NULL"
            ).fetchone()[0]
        text = (
            f"📈 <b>Gem Threshold</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Threshold atual: <b>{threshold}</b>\n\n"
            f"Anúncios com score: <b>{count}</b>\n\n"
            f"Fórmula: <code>max(70, média + desvio)</code>\n"
            f"Calculado sobre os últimos 200 anúncios."
        )
        tg_edit(chat_id, message_id, text, back=True)

    elif data == "keywords":
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT keyword, active FROM feed_keywords ORDER BY keyword"
            ).fetchall()
        lines = ["🔑 <b>Keywords do feed</b>\n━━━━━━━━━━━━━━━━━━━━"]
        if not rows:
            lines.append("Nenhuma keyword configurada.")
        for r in rows:
            lines.append(f"{'✅' if r['active'] else '⏸'} {_esc(r['keyword'])}")
        tg_edit(chat_id, message_id, "\n".join(lines), back=True)

    elif data == "fbauth":
        status = "✅ Ativa" if os.path.exists(AUTH_STATE) else "❌ Não encontrada"
        text = (
            f"🔐 <b>Facebook Auth</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Sessão: <b>{status}</b>\n\n"
            f"Para reautenticar:\n"
            f"1. Rode <code>python setup_fb.py</code> no seu PC\n"
            f"2. Envie o arquivo <code>auth_state.json</code> aqui\n\n"
            f"O arquivo será aplicado imediatamente."
        )
        tg_edit(chat_id, message_id, text, back=True)

    elif data == "help":
        text = (
            f"❓ <b>Como funciona</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Escaneia o Facebook Marketplace de Curitiba buscando "
            f"ofertas abaixo do preço médio.\n\n"
            f"<b>Score (0–100):</b>\n"
            f"• Desconto → até 60 pts\n"
            f"• Feedback aprendido → ±20 pts\n"
            f"• Defeito detectado → −20 pts cada\n\n"
            f"<b>Gem:</b> score ≥ threshold adaptativo\n"
            f"(média + desvio dos últimos 200 anúncios)\n\n"
            f"<b>Comandos:</b>\n"
            f"/start — abrir este menu"
        )
        tg_edit(chat_id, message_id, text, back=True)

    elif data == "summary":
        hour = get_setting("summary_hour", "8")
        text = (
            f"📅 <b>Resumo Diário</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Horário atual: <b>{hour}:00</b>\n\n"
            f"Escolha o novo horário:"
        )
        keyboard = {"inline_keyboard": [
            [{"text": "6:00",  "callback_data": "sum_6"},
             {"text": "7:00",  "callback_data": "sum_7"},
             {"text": "8:00",  "callback_data": "sum_8"},
             {"text": "9:00",  "callback_data": "sum_9"}],
            [{"text": "10:00", "callback_data": "sum_10"},
             {"text": "12:00", "callback_data": "sum_12"},
             {"text": "18:00", "callback_data": "sum_18"},
             {"text": "20:00", "callback_data": "sum_20"}],
            [{"text": "📤 Enviar agora", "callback_data": "sum_now"},
             {"text": "◀ Menu",          "callback_data": "menu"}],
        ]}
        tg_api("editMessageText", chat_id=chat_id, message_id=message_id,
               text=text, parse_mode="HTML", reply_markup=keyboard)

    elif data.startswith("sum_"):
        val = data[4:]
        if val == "now":
            threading.Thread(target=send_daily_summary, daemon=True).start()
            tg_api("answerCallbackQuery", callback_query_id=query["id"], text="Enviando resumo...")
        else:
            set_setting("summary_hour", val)
            tg_api("answerCallbackQuery", callback_query_id=query["id"], text=f"Horário salvo: {val}:00")
            tg_edit(chat_id, message_id, _hud_text())


def tg_handle_message(msg: dict):
    text    = msg.get("text", "").strip()
    chat_id = msg["chat"]["id"]

    if text.startswith("/start") or text.startswith("/menu"):
        tg_send_hud(chat_id)
        return

    if text.startswith("/summary"):
        threading.Thread(target=send_daily_summary, daemon=True).start()
        tg_api("sendMessage", chat_id=chat_id, text="📅 Enviando resumo...")
        return

    doc = msg.get("document")
    if doc and doc.get("file_name") == "auth_state.json":
        try:
            file_info = tg_api("getFile", file_id=doc["file_id"])
            fp        = file_info["result"]["file_path"]
            dl_url    = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{fp}"
            urllib.request.urlretrieve(dl_url, AUTH_STATE)
            tg_api("sendMessage", chat_id=chat_id, parse_mode="HTML",
                   text="✅ <b>Sessão atualizada!</b> O servidor já pode buscar no Facebook.")
        except Exception as e:
            tg_api("sendMessage", chat_id=chat_id, text=f"❌ Erro ao salvar sessão: {e}")


def tg_polling_loop():
    global _tg_offset
    import time
    print("[Telegram] Bot polling started — send /start to open the HUD.")
    while True:
        try:
            result = tg_api(
                "getUpdates", offset=_tg_offset, timeout=30,
                allowed_updates=["message", "callback_query"]
            )
            if not result.get("ok"):
                time.sleep(5)
                continue
            for update in result.get("result", []):
                _tg_offset = update["update_id"] + 1
                if "callback_query" in update:
                    try:
                        tg_handle_callback(update["callback_query"])
                    except Exception as e:
                        print(f"[Telegram] callback error: {e}")
                elif "message" in update:
                    try:
                        tg_handle_message(update["message"])
                    except Exception as e:
                        print(f"[Telegram] message error: {e}")
        except Exception as e:
            print(f"[Telegram] polling error: {e}")
            time.sleep(5)


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

        title = page.title()
        print(f"[FB] title: {title!r}  graphql: {len(pending)}")
        if "log in" in title.lower() or "log into" in title.lower():
            browser.close()
            threading.Thread(target=_tg_notify_session_expired, daemon=True).start()
            raise RuntimeError("Facebook session expired. Send auth_state.json to the Telegram bot.")

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
    # Filter out buyer-intent listings (people looking to buy, not sell)
    listings = [
        l for l in listings
        if not any(l.get("name", "").lower().startswith(p) for p in BUYER_PREFIXES)
    ]

    prices = [l["price"] for l in listings if l["price"] > 0]
    if not prices:
        return listings, None

    query_avg = round(mean(prices), 2)

    # Use stored historical average for scoring (more stable), but fresh avg for profit display
    stored_avg    = get_query_avg(query)
    scoring_avg   = stored_avg if stored_avg else query_avg
    save_query_avg(query, query_avg)

    feedback = get_feedback()

    enriched = []
    for listing in listings:
        score   = compute_score(listing["price"], scoring_avg, feedback)
        defects = detect_defects(listing.get("name", ""))
        if defects:
            penalty = min(50.0, len(defects) * 20.0)
            score   = max(0.0, round(score - penalty, 1))
        item = {**listing, "score": score, "avg_price": query_avg, "defects": defects,
                "photo_condition": "", "photo_issues": []}
        if score >= 75 and item.get("photo_url"):
            analysis = analyze_photo(item["photo_url"])
            if analysis:
                item["score"]          = max(0.0, min(100.0, round(score + analysis.get("score_adjust", 0), 1)))
                item["photo_condition"] = analysis.get("condition", "")
                item["photo_issues"]    = analysis.get("issues", [])
        enriched.append(item)

    enriched.sort(key=lambda x: x["score"], reverse=True)
    return enriched, query_avg


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

    mark_sold_listings(query, [l["id"] for l in listings])
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
                "INSERT OR REPLACE INTO listings (id, name, price, photo_url, item_url, seller_name, seller_location, search_id, found_at, score, photo_condition) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (item["id"], item["name"], item["price"], item.get("photo_url",""), item.get("item_url",""), item.get("seller_name",""), item.get("seller_location",""), search_id, now, item.get("score"), item.get("photo_condition",""))
            )

    threading.Thread(target=check_gems, args=(enriched,), daemon=True).start()
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
            mark_sold_listings(kw, [l["id"] for l in listings])
            enriched, _ = _enrich(listings, kw)
            for item in enriched:
                item["query"] = kw
            all_results.extend(enriched)
        except Exception as e:
            errors.append(f"{kw}: {e}")

    all_results.sort(key=lambda x: x["score"], reverse=True)
    threading.Thread(target=check_gems, args=(all_results,), daemon=True).start()

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


@app.route("/api/admin/test-telegram", methods=["POST"])
@require_admin
def admin_test_telegram():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return jsonify({"ok": False, "error": "Set FF_TELEGRAM_TOKEN and FF_TELEGRAM_CHAT before running server"}), 400

    msg = (
        "🔥 <b>Brique Finder — Teste de alerta!</b>\n\n"
        "<a href=\"https://www.facebook.com/marketplace/\"><b>Teste — Brique Finder</b></a>\n"
        "💰 R$ 100.00  (média R$ 200.00)\n"
        "📈 Lucro est.: R$ 100.00  |  Score: 99.0\n"
        "📍 Curitiba, PR"
    )
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"}).encode()
    req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        if result.get("ok"):
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": result.get("description", "Unknown API error")}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


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
    if TELEGRAM_TOKEN and TELEGRAM_CHAT:
        print(f"  [Telegram] chat: {TELEGRAM_CHAT}")
        threading.Thread(target=tg_polling_loop,      daemon=True).start()
        threading.Thread(target=daily_summary_loop,   daemon=True).start()
    else:
        print(f"  [Telegram] not configured — set FF_TELEGRAM_TOKEN and FF_TELEGRAM_CHAT")
    print(f"  Brique Finder running at http://localhost:{PORT}\n")
    app.run(port=PORT, debug=False)
