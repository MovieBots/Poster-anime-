import os
import re
import html
import sqlite3
import asyncio
import json
import io
from urllib.parse import quote
from urllib.request import Request, urlopen
from bson import ObjectId
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, ChatJoinRequestHandler,
    ContextTypes, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
SOURCE_CHANNEL_ID = os.getenv("SOURCE_CHANNEL_ID", "").strip()
DB_PATH = os.getenv("DB_PATH", "animebot.db")
PORT = int(os.getenv("PORT", "8000"))
# Comma-separated channel usernames/IDs; examples: @mychannel,-1001234567890
FSUB_CHANNELS = [x.strip() for x in os.getenv("FSUB_CHANNELS", "").split(",") if x.strip()]
# Set FSUB_ENABLED=true/false in Koyeb, or toggle at runtime with /fsub on|off (admin only).
FSUB_ENABLED = os.getenv("FSUB_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
# Comma-separated invite links in the same order as FSUB_CHANNELS
FSUB_LINKS = [x.strip() for x in os.getenv("FSUB_LINKS", "").split(",") if x.strip()]

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "anime_vault").strip()
_mongo_client = None

def mongo_collection():
    global _mongo_client
    if not MONGO_URI:
        return None
    from pymongo import MongoClient
    if _mongo_client is None:
        _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
        _mongo_client.admin.command("ping")
    return _mongo_client[MONGO_DB_NAME]["files"]

def db():
    """Return a SQLite or PostgreSQL connection with dict-like rows."""
    if DATABASE_URL:
        import psycopg
        from psycopg.rows import dict_row
        class PGConnection:
            def __init__(self):
                self.conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, tb):
                if exc_type:
                    self.conn.rollback()
                else:
                    self.conn.commit()
                self.conn.close()
            def execute(self, sql, params=()):
                sql = sql.replace("COLLATE NOCASE", "")
                sql = sql.replace("?", "%s")
                return self.conn.execute(sql, params)
        return PGConnection()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    if MONGO_URI:
        col = mongo_collection()
        col.create_index("file_unique_id", unique=True)
        col.create_index("title")
        return
    if DATABASE_URL:
        with db() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS files (
                id BIGSERIAL PRIMARY KEY, file_id TEXT NOT NULL,
                file_unique_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
                episode TEXT DEFAULT '', quality TEXT DEFAULT '', caption TEXT DEFAULT '',
                source_chat TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_files_title ON files(title)")
            c.execute("CREATE TABLE IF NOT EXISTS posters (title TEXT PRIMARY KEY, file_id TEXT NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    else:
        with db() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT, file_id TEXT NOT NULL,
                file_unique_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
                episode TEXT DEFAULT '', quality TEXT DEFAULT '', caption TEXT DEFAULT '',
                source_chat TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_files_title ON files(title COLLATE NOCASE)")
            c.execute("CREATE TABLE IF NOT EXISTS posters (title TEXT PRIMARY KEY COLLATE NOCASE, file_id TEXT NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")

def extract_meta(text, fallback=""):
    text = (text or "").strip()
    lines = [re.sub(r'^[\W_]+', '', x).strip() for x in text.splitlines() if x.strip()]
    title = ""
    episode = ""
    quality = ""
    for line in lines:
        low = line.lower()
        m = re.search(r'(?:anime|title|series)\s*[:\-]\s*(.+)', line, re.I)
        if m:
            title = m.group(1).strip()
        m = re.search(r'\b(?:ep|episode)\s*[:#\-]?\s*(\d+(?:\.\d+)?)', line, re.I)
        if m:
            episode = m.group(1)
        m = re.search(r'\b(360p|480p|720p|1080p|1440p|2160p|4k)\b', line, re.I)
        if m:
            quality = m.group(1).upper()
        if not title:
            candidate = re.sub(r'#\w+', '', line).strip(" -|•🎬📺🌐")
            candidate = re.sub(r"\.(mkv|mp4|avi|mov|webm|m4v)$", "", candidate, flags=re.I)
            candidate = re.sub(r"\b(?:ep(?:isode)?\s*[-_.#]?\d+|\d{1,4}p|4k|x26[45]|hevc|aac|10bit|hindi|dubbed|subbed|dual audio|official|quality|resolution)\b", " ", candidate, flags=re.I)
            candidate = re.sub(r"[._]+", " ", candidate)
            candidate = re.sub(r"\s{2,}", " ", candidate).strip(" -_|•")
            if len(candidate) >= 2 and candidate.lower() not in {"anime", "video", "file", "unknown"}:
                title = candidate
    # Remove common release noise from a title inferred from filename/caption.
    if title:
        title = re.sub(r"\.(mkv|mp4|avi|mov|webm|m4v)$", "", title, flags=re.I)
        title = re.sub(r"\b(?:ep(?:isode)?\s*[-_.#]?\d+|\d{1,4}p|4k|x26[45]|hevc|aac|10bit|hindi|dubbed|subbed|dual audio)\b", " ", title, flags=re.I)
        title = re.sub(r"[._]+", " ", title)
        title = re.sub(r"\s{2,}", " ", title).strip(" -_|•")
    if title.lower() in {"untitled anime", "anime", "video", "file", "unknown", ""}:
        title = fallback or ""
    return title[:160], episode[:20], quality[:20]

def is_admin(user_id):
    return user_id in ADMIN_IDS

async def subscribed(user_id, context):
    if not FSUB_ENABLED or not FSUB_CHANNELS:
        return True, []
    missing = []
    for channel in FSUB_CHANNELS:
        try:
            member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status in ("left", "kicked"):
                missing.append(channel)
        except Exception:
            # Fail closed so a misconfigured channel does not silently bypass access.
            missing.append(channel)
    return not missing, missing

async def fsub_markup(missing):
    buttons = []
    for i, channel in enumerate(missing):
        link = None
        try:
            idx = FSUB_CHANNELS.index(channel)
            if idx < len(FSUB_LINKS) and FSUB_LINKS[idx]:
                link = FSUB_LINKS[idx]
        except ValueError:
            pass
        if not link and channel.startswith("@"):
            link = "https://t.me/" + channel[1:]
        if link:
            buttons.append([InlineKeyboardButton(f"Join {channel}", url=link)])
    buttons.append([InlineKeyboardButton("✅ I Joined — Check Again", callback_data="check_fsub")])
    return InlineKeyboardMarkup(buttons)

def mention_prefix(message, user=None):
    """Clickable mention for group replies; empty in private chats."""
    chat = getattr(message, "chat", None)
    user = user or getattr(message, "from_user", None)
    if not chat or chat.type not in ("group", "supergroup") or not user:
        return ""
    label = html.escape(user.first_name or "User")
    return f'<a href="tg://user?id={user.id}">{label}</a> '

async def reply_user(message, text, user=None, **kwargs):
    """Reply and mention the requesting user in groups."""
    prefix = mention_prefix(message, user=user)
    if prefix:
        kwargs["parse_mode"] = "HTML"
    await message.reply_text(prefix + text, **kwargs)

async def chat_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Explain that request-to-join links require channel admin approval."""
    request = update.chat_join_request
    if not request or not request.from_user:
        return
    try:
        await context.bot.send_message(
            chat_id=request.from_user.id,
            text="⏳ Your channel join request is pending. Please wait for a channel admin to approve it, then return here and try again."
        )
    except Exception as exc:
        # Users must start the bot before it can DM them. Do not spam logs for this expected case.
        if "bot can't initiate conversation" not in str(exc).lower():
            print(f"Could not DM join requester: {exc!r}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    ok, missing = await subscribed(user.id, context)
    if not ok:
        await update.effective_message.reply_text(
            "🔐 **Join our channel(s) first** to use the anime catalog.",
            parse_mode="Markdown", reply_markup=await fsub_markup(missing)
        )
        return
    welcome_text = (
        "🌌 **ANIME VAULT**\n\nChoose one option below, or type an anime name to search.\n"
        "Example: `Naruto` or `Demon Slayer`"
    )
    welcome_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Anime Search", callback_data="search_help"), InlineKeyboardButton("🎬 Anime Movie", callback_data="browse:movie")],
        [InlineKeyboardButton("📺 Latest Anime Series", callback_data="browse:series"), InlineKeyboardButton("▶️ Latest Episode", callback_data="browse:episode")]
    ])
    # The admin can customize the image shown with /start using /setstartposter.
    welcome_poster = get_poster("__START_WELCOME_POSTER__")
    if welcome_poster:
        await update.effective_message.reply_photo(
            photo=welcome_poster,
            caption=welcome_text,
            parse_mode="Markdown",
            reply_markup=welcome_markup,
        )
    else:
        await update.effective_message.reply_text(
            welcome_text,
            parse_mode="Markdown",
            reply_markup=welcome_markup,
        )

async def fsub_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only runtime toggle: /fsub on or /fsub off."""
    global FSUB_ENABLED
    msg = update.effective_message
    user = update.effective_user
    if not msg:
        return
    if not user or not is_admin(user.id):
        await msg.reply_text("⛔ Admin only.")
        return
    if not context.args or context.args[0].lower() not in ("on", "off", "status"):
        await msg.reply_text("Usage: /fsub on  |  /fsub off  |  /fsub status")
        return
    action = context.args[0].lower()
    if action == "on":
        FSUB_ENABLED = True
        await msg.reply_text("✅ Force Subscribe ON. Users must join configured channels.")
    elif action == "off":
        FSUB_ENABLED = False
        await msg.reply_text("🔓 Force Subscribe OFF. Users can use the catalog without joining.")
    else:
        await msg.reply_text(f"Force Subscribe: {'ON' if FSUB_ENABLED else 'OFF'}\nConfigured channels: {len(FSUB_CHANNELS)}")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Type an anime title to search. Admins can upload a video/document to this bot in private chat to add it to the catalog.")

def save_poster(title, file_id):
    title = title.strip()[:160]
    if not title:
        return False
    if MONGO_URI:
        mongo_collection().database["posters"].update_one(
            {"title_key": title.casefold()},
            {"$set": {"title": title, "title_key": title.casefold(), "file_id": file_id}},
            upsert=True,
        )
    else:
        with db() as c:
            if DATABASE_URL:
                c.execute("INSERT INTO posters(title,file_id) VALUES(?,?) ON CONFLICT(title) DO UPDATE SET file_id=EXCLUDED.file_id, updated_at=CURRENT_TIMESTAMP", (title, file_id))
            else:
                c.execute("INSERT INTO posters(title,file_id) VALUES(?,?) ON CONFLICT(title) DO UPDATE SET file_id=excluded.file_id, updated_at=CURRENT_TIMESTAMP", (title, file_id))
    return True

def _lookup_anime_poster(title):
    """Find an anime cover through Jikan and download it; Telegram can reject remote image URLs."""
    try:
        url = "https://api.jikan.moe/v4/anime?q=" + quote(title) + "&limit=10&sfw=true"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 AnimeSearchBot/1.0"})
        with urlopen(req, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        items = payload.get("data") or []
        if not items:
            return None
        def norm(value):
            return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())
        target = norm(title)
        def score(item):
            names = [item.get("title"), item.get("title_english"), item.get("title_japanese")]
            norms = [norm(n) for n in names if n]
            if target in norms:
                return 3
            if any(target in n or n in target for n in norms if n):
                return 2
            return 1
        chosen = sorted(items, key=score, reverse=True)[0]
        images = chosen.get("images") or {}
        jpg = images.get("jpg") or {}
        webp = images.get("webp") or {}
        image_url = jpg.get("large_image_url") or webp.get("large_image_url") or jpg.get("image_url") or webp.get("image_url")
        if not image_url:
            return None
        req = Request(image_url, headers={"User-Agent": "Mozilla/5.0 AnimeSearchBot/1.0", "Referer": "https://myanimelist.net/"})
        with urlopen(req, timeout=12) as response:
            data = response.read(5 * 1024 * 1024 + 1)
        if not data or len(data) > 5 * 1024 * 1024:
            return None
        return data
    except Exception as exc:
        print(f"Anime poster lookup failed for {title!r}: {exc}")
        return None

async def lookup_anime_poster(title):
    return await asyncio.to_thread(_lookup_anime_poster, title)

def get_poster(title):
    if MONGO_URI:
        doc = mongo_collection().database["posters"].find_one({"title_key": title.casefold()})
        return doc.get("file_id") if doc else None
    with db() as c:
        row = c.execute("SELECT file_id FROM posters WHERE title=? COLLATE NOCASE", (title,)).fetchone()
        return row["file_id"] if row else None

async def setposter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not is_admin(user.id):
        if msg: await msg.reply_text("⛔ Admin only.")
        return
    photo_msg = msg.reply_to_message
    if not photo_msg or not photo_msg.photo:
        await msg.reply_text("Poster set karne ke liye pehle photo bhejein, phir us photo par reply karke likhein:\n/setposter Naruto Shippuden\n\nYa photo ke caption mein likhein: /setposter Naruto Shippuden")
        return
    title = " ".join(context.args).strip()
    if not title:
        await msg.reply_text("Usage: photo par reply karke /setposter Anime Name likhein.")
        return
    save_poster(title, photo_msg.photo[-1].file_id)
    await msg.reply_text(f"✅ Poster updated for: {title}")

async def setstartposter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not is_admin(user.id):
        if msg:
            await msg.reply_text("⛔ Admin only.")
        return
    photo_msg = msg.reply_to_message
    if not photo_msg or not photo_msg.photo:
        await msg.reply_text(
            "Start poster badalne ke liye photo pehle bhejein, phir us photo par reply karke /setstartposter likhein.\n"
            "Ya photo ke caption mein /setstartposter likhkar bhejein."
        )
        return
    save_poster("__START_WELCOME_POSTER__", photo_msg.photo[-1].file_id)
    await msg.reply_text("✅ /start welcome poster updated. Ab /start karke check karein.")


async def poster_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not is_admin(user.id) or not msg.photo:
        return
    caption = (msg.caption or "").strip()
    if re.match(r"^/setstartposter(?:@\w+)?$", caption, re.I):
        save_poster("__START_WELCOME_POSTER__", msg.photo[-1].file_id)
        await msg.reply_text("✅ /start welcome poster updated. Ab /start karke check karein.")
        return
    match = re.match(r"^/setposter(?:@\w+)?\s+(.+)$", caption, re.I | re.S)
    if not match:
        return
    title = match.group(1).strip()
    save_poster(title, msg.photo[-1].file_id)
    await msg.reply_text(f"✅ Poster updated for: {title}")

async def browse_callback(q, context, mode):
    if MONGO_URI:
        docs = list(mongo_collection().find({}, {"title": 1, "episode": 1, "quality": 1, "created_at": 1}).sort("_id", -1).limit(30))
        rows = docs
    else:
        with db() as c:
            rows = c.execute("SELECT id, title, episode, quality FROM files ORDER BY id DESC LIMIT 30").fetchall()
    if mode == "movie":
        # The starter stores files, not a verified movie/series type; show latest catalog titles here.
        title_map = {}
        for r in rows:
            title_map.setdefault(r["title"], r)
        rows = list(title_map.values())
    elif mode == "series":
        title_map = {}
        for r in rows:
            title_map.setdefault(r["title"], r)
        rows = list(title_map.values())
    else:
        rows = rows[:15]
    if not rows:
        await q.message.reply_text("Abhi catalog mein koi anime nahi hai.")
        return
    buttons = []
    for r in rows[:15]:
        label = str(r.get("title") if hasattr(r, "get") else r["title"])
        ep = (r.get("episode", "") if hasattr(r, "get") else r["episode"]) or "?"
        buttons.append([InlineKeyboardButton(("🎬 " if mode == "movie" else "📺 ") + label[:45] + ("" if mode != "episode" else f" · EP {ep}"), callback_data="title:" + label[:55])])
    heading = {"movie": "🎬 Anime Movie", "series": "📺 Latest Anime Series", "episode": "▶️ Latest Episode"}.get(mode, "Anime")
    await q.message.reply_text(heading + "\nSelect an item:", reply_markup=InlineKeyboardMarkup(buttons))

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Admin only.")
        return
    if MONGO_URI:
        col = mongo_collection()
        count = col.count_documents({})
        titles = len(col.distinct("title"))
    else:
        with db() as c:
            count = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            titles = c.execute("SELECT COUNT(DISTINCT title) FROM files").fetchone()[0]
    await update.effective_message.reply_text(
        f"🛠 Admin Panel\n\nFiles: {count}\nAnime titles: {titles}\n\n"
        f"Force Subscribe: {'ON' if FSUB_ENABLED else 'OFF'}\n"
        "Toggle with /fsub on or /fsub off (admin only).\n\n"
        "Send a video/document in this private chat to add it manually.\n"
        "For best title matching, use caption format:\nAnime: Naruto Shippuden\nEpisode: 12\nQuality: 1080p\n"
        "Filename matching is a fallback; image/thumbnail AI recognition is not enabled in this build."
    )

def media_filename(message):
    if message.document and message.document.file_name:
        return message.document.file_name
    if message.video and getattr(message.video, "file_name", None):
        return message.video.file_name
    if message.audio and getattr(message.audio, "file_name", None):
        return message.audio.file_name
    if message.animation and getattr(message.animation, "file_name", None):
        return message.animation.file_name
    return ""

def media_details(message):
    if message.video:
        return message.video.file_id, message.video.file_unique_id
    if message.document:
        return message.document.file_id, message.document.file_unique_id
    if message.audio:
        return message.audio.file_id, message.audio.file_unique_id
    if message.animation:
        return message.animation.file_id, message.animation.file_unique_id
    return None, None

def save_media(message, source_chat=""):
    file_id, unique_id = media_details(message)
    if not file_id:
        return "unsupported"
    caption = message.caption or ""
    filename = media_filename(message)
    # Explicit caption metadata wins; filename helps when caption is empty/generic.
    title, episode, quality = extract_meta(caption, fallback="")
    file_title, file_episode, file_quality = extract_meta(filename, fallback="")
    if not title:
        title = file_title
    if not episode:
        episode = file_episode
    if not quality:
        quality = file_quality
    # Avoid polluting the catalog with a fake generic title.
    if not title:
        title = "Needs Review"
    if MONGO_URI:
        from pymongo.errors import DuplicateKeyError
        try:
            mongo_collection().insert_one({"file_id": file_id, "file_unique_id": unique_id, "title": title,
                "episode": episode, "quality": quality, "caption": caption, "source_chat": str(source_chat)})
            return "saved"
        except DuplicateKeyError:
            return "duplicate"
    with db() as c:
        try:
            c.execute(
                "INSERT INTO files(file_id,file_unique_id,title,episode,quality,caption,source_chat) VALUES(?,?,?,?,?,?,?)",
                (file_id, unique_id, title, episode, quality, caption, str(source_chat))
            )
            return "saved"
        except Exception as exc:
            if isinstance(exc, sqlite3.IntegrityError) or (DATABASE_URL and exc.__class__.__name__ == 'UniqueViolation'):
                return "duplicate"
            raise

async def source_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    result = save_media(msg, update.effective_chat.id if update.effective_chat else "")
    if result == "saved":
        # Silent by default in source channel; avoids noisy replies.
        return

async def manual_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not is_admin(user.id) or msg.chat.type != "private":
        return
    result = save_media(msg, "manual")
    if result == "saved":
        await msg.reply_text("✅ File added to Anime Vault.")
    elif result == "duplicate":
        await msg.reply_text("ℹ️ This exact Telegram file is already in the catalog.")
    else:
        await msg.reply_text("Please send a video, document, audio, or animation file.")

async def search_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not msg.text or msg.text.startswith("/"):
        return
    ok, missing = await subscribed(user.id, context)
    if not ok:
        await reply_user(msg, "🔐 Join the required channel(s) first.", reply_markup=await fsub_markup(missing))
        return
    query = msg.text.strip()
    if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
        bot_username = (context.bot.username or "").lower()
        if bot_username:
            query = re.sub(rf"@{re.escape(bot_username)}\b", "", query, flags=re.I).strip()
        if not query and msg.reply_to_message and msg.reply_to_message.text:
            query = msg.reply_to_message.text.strip()
    if len(query) < 2:
        await reply_user(msg, "Type at least 2 characters to search.")
        return
    if MONGO_URI:
        pipeline = [
            {"$match": {"title": {"$regex": re.escape(query), "$options": "i"}}},
            {"$group": {"_id": "$title", "n": {"$sum": 1}}},
            {"$sort": {"_id": 1}}, {"$limit": 15}
        ]
        rows = [{"title": r["_id"], "n": r["n"]} for r in mongo_collection().aggregate(pipeline)]
    else:
        with db() as c:
            search_op = "ILIKE" if DATABASE_URL else "LIKE"
            rows = c.execute(
                f"SELECT title, COUNT(*) AS n FROM files WHERE title {search_op} ? COLLATE NOCASE GROUP BY title ORDER BY title LIMIT 15" if not DATABASE_URL else
                f"SELECT title, COUNT(*) AS n FROM files WHERE title {search_op} ? GROUP BY title ORDER BY title LIMIT 15",
                (f"%{query}%",)
            ).fetchall()
    if not rows:
        await reply_user(msg, "😕 No matching anime found yet. Ask an admin to add its files.")
        return
    buttons = [[InlineKeyboardButton(f"🎬 {r['title']} · {r['n']} file(s)", callback_data="title:" + str(r["title"])[:55])] for r in rows]
    await reply_user(msg, "✨ <b>Matching anime</b>\nChoose the exact title:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    if q.data == "search_help":
        await reply_user(q.message, "🔎 type an anime title in the chat to search.", user=q.from_user)
        return
    if q.data.startswith("browse:"):
        await browse_callback(q, context, q.data.split(":", 1)[1])
        return
    if q.data == "check_fsub":
        ok, missing = await subscribed(q.from_user.id, context)
        if not ok:
            await reply_user(q.message, "Please join all required channels, then tap check again.", user=q.from_user, reply_markup=await fsub_markup(missing))
        else:
            await reply_user(q.message, "✅ Access confirmed. Now type an anime title to search.", user=q.from_user)
        return
    if q.data.startswith("title:"):
        title = q.data[6:]
        if MONGO_URI:
            rows = list(mongo_collection().find({"title": {"$regex": "^" + re.escape(title) + "$", "$options": "i"}}).sort([("episode", 1)]).limit(80))
        else:
            with db() as c:
                rows = c.execute("SELECT id, episode, quality FROM files WHERE title=? COLLATE NOCASE ORDER BY CAST(episode AS REAL), id LIMIT 80", (title,)).fetchall()
        if not rows:
            await reply_user(q.message, "No files found for this title.", user=q.from_user)
            return
        buttons = []
        for r in rows:
            label = f"EP {r['episode'] or '?'} · {r['quality'] or 'Quality N/A'}"
            row_id = str(r["_id"]) if MONGO_URI else str(r["id"])
            buttons.append([InlineKeyboardButton("📥 " + label, callback_data=f"file:{row_id}")])
        title_text = f"📺 <b>{html.escape(str(title))}</b>\nSelect an episode / quality:"
        poster_id = get_poster(str(title))
        if poster_id:
            await context.bot.send_photo(chat_id=q.message.chat_id, photo=poster_id, caption=title_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
        else:
            poster_data = await lookup_anime_poster(str(title))
            if poster_data:
                try:
                    photo = io.BytesIO(poster_data)
                    photo.name = "anime-poster.jpg"
                    await context.bot.send_photo(chat_id=q.message.chat_id, photo=photo, caption=title_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
                except Exception as exc:
                    print(f"Could not send anime poster for {title!r}: {exc}")
                    await reply_user(q.message, title_text, user=q.from_user, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
            else:
                await reply_user(q.message, title_text, user=q.from_user, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
        return
    if q.data.startswith("file:"):
        try:
            file_row_id = q.data.split(":", 1)[1] if MONGO_URI else int(q.data.split(":", 1)[1])
        except ValueError:
            return
        ok, missing = await subscribed(q.from_user.id, context)
        if not ok:
            await reply_user(q.message, "🔐 Join the required channel(s) first.", user=q.from_user, reply_markup=await fsub_markup(missing))
            return
        if MONGO_URI:
            row = mongo_collection().find_one({"_id": ObjectId(file_row_id)})
        else:
            with db() as c:
                row = c.execute("SELECT * FROM files WHERE id=?", (file_row_id,)).fetchone()
        if not row:
            await reply_user(q.message, "This file is no longer available.", user=q.from_user)
            return
        caption = f"🎬 {row['title']}\n📺 Episode: {row['episode'] or 'N/A'}\n🎞 Quality: {row['quality'] or 'N/A'}"
        try:
            await context.bot.send_document(chat_id=q.from_user.id, document=row["file_id"], caption=caption)
        except Exception:
            await context.bot.send_message(chat_id=q.from_user.id, text="I couldn't deliver that file. Please contact the admin.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print(f"Update error: {context.error!r}")

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if urlparse(self.path).path not in ("/", "/health", "/healthz"):
            self.send_response(404); self.end_headers(); return
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, fmt, *args):
        return

def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Health server listening on 0.0.0.0:{PORT}")

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required.")
    init_db()
    start_health_server()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(ChatJoinRequestHandler(chat_join_request))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("fsub", fsub_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("setposter", setposter_cmd))
    app.add_handler(CommandHandler("setstartposter", setstartposter_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, poster_photo))
    if SOURCE_CHANNEL_ID:
        try:
            source_id = int(SOURCE_CHANNEL_ID)
        except ValueError:
            source_id = SOURCE_CHANNEL_ID
        app.add_handler(MessageHandler(filters.Chat(source_id) & (filters.VIDEO | filters.Document.ALL | filters.AUDIO | filters.ANIMATION), source_post))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler((filters.VIDEO | filters.Document.ALL | filters.AUDIO | filters.ANIMATION) & ~filters.COMMAND, manual_upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_text))
    app.add_error_handler(error_handler)
    print("Polling started")
    app.run_polling(drop_pending_updates=False)

if __name__ == "__main__":
    main()
