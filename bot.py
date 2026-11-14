import os
import re
import html
import json
import logging
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from urllib.parse import quote

import aiohttp
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# ضع Telegram User ID الخاص بك في GitHub Secrets باسم ADMIN_ID
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()

YTS_API = "https://yts.mx/api/v2"

DB_PATH = Path(os.getenv("DB_PATH", "yts_movies.db"))

CHECK_INTERVAL = 600  # فحص الأفلام الجديدة كل 10 دقائق
MOVIES_PER_PAGE = 5

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("YTS-Movies-Pro")


# =========================================================
# DATABASE
# =========================================================

class Database:

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(
            str(self.path),
            timeout=30,
        )

        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")

        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT DEFAULT '',
                    first_seen TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS favorites (
                    user_id TEXT NOT NULL,
                    movie_id INTEGER NOT NULL,
                    title TEXT DEFAULT '',
                    PRIMARY KEY (user_id, movie_id)
                );

                CREATE TABLE IF NOT EXISTS watchlist (
                    user_id TEXT NOT NULL,
                    movie_id INTEGER NOT NULL,
                    title TEXT DEFAULT '',
                    PRIMARY KEY (user_id, movie_id)
                );

                CREATE TABLE IF NOT EXISTS settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT
                );
            """)

    # تسجيل المستخدم بدون تكرار
    def register_user(self, user_id, username=""):
        with self.connect() as db:
            db.execute("""
                INSERT INTO users (user_id, username)
                VALUES (?, ?)
                ON CONFLICT(user_id)
                DO UPDATE SET username = excluded.username
            """, (str(user_id), username or ""))

    # عدد المستخدمين
    def get_user_count(self):
        with self.connect() as db:
            result = db.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()

            return result[0]

    # جميع المستخدمين
    def get_all_users(self):
        with self.connect() as db:
            rows = db.execute(
                "SELECT user_id FROM users"
            ).fetchall()

            return [row["user_id"] for row in rows]

    # إضافة أو حذف من المفضلة
    def toggle_favorite(self, user_id, movie_id, title):
        with self.connect() as db:

            existing = db.execute("""
                SELECT 1 FROM favorites
                WHERE user_id = ? AND movie_id = ?
            """, (str(user_id), movie_id)).fetchone()

            if existing:
                db.execute("""
                    DELETE FROM favorites
                    WHERE user_id = ? AND movie_id = ?
                """, (str(user_id), movie_id))

                return False

            db.execute("""
                INSERT OR IGNORE INTO favorites
                (user_id, movie_id, title)
                VALUES (?, ?, ?)
            """, (str(user_id), movie_id, title))

            return True

    def get_favorites(self, user_id):
        with self.connect() as db:
            return db.execute("""
                SELECT movie_id, title
                FROM favorites
                WHERE user_id = ?
                ORDER BY title
            """, (str(user_id),)).fetchall()

    # إضافة أو حذف من قائمة المتابعة
    def toggle_watchlist(self, user_id, movie_id, title):
        with self.connect() as db:

            existing = db.execute("""
                SELECT 1 FROM watchlist
                WHERE user_id = ? AND movie_id = ?
            """, (str(user_id), movie_id)).fetchone()

            if existing:
                db.execute("""
                    DELETE FROM watchlist
                    WHERE user_id = ? AND movie_id = ?
                """, (str(user_id), movie_id))

                return False

            db.execute("""
                INSERT OR IGNORE INTO watchlist
                (user_id, movie_id, title)
                VALUES (?, ?, ?)
            """, (str(user_id), movie_id, title))

            return True

    def get_watchlist(self, user_id):
        with self.connect() as db:
            return db.execute("""
                SELECT movie_id, title
                FROM watchlist
                WHERE user_id = ?
                ORDER BY title
            """, (str(user_id),)).fetchall()

    # حفظ آخر أفلام تم فحصها
    def get_setting(self, key):
        with self.connect() as db:
            row = db.execute("""
                SELECT setting_value
                FROM settings
                WHERE setting_key = ?
            """, (key,)).fetchone()

            return row["setting_value"] if row else None

    def set_setting(self, key, value):
        with self.connect() as db:
            db.execute("""
                INSERT INTO settings (setting_key, setting_value)
                VALUES (?, ?)
                ON CONFLICT(setting_key)
                DO UPDATE SET setting_value = excluded.setting_value
            """, (key, value))


db = Database(DB_PATH)


# =========================================================
# YTS API
# =========================================================

async def yts_request(endpoint, params=None):

    url = f"{YTS_API}/{endpoint}.json"

    timeout = aiohttp.ClientTimeout(total=25)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url,
                params=params or {},
            ) as response:

                if response.status != 200:
                    logger.error("YTS API returned %s", response.status)
                    return None

                data = await response.json(content_type=None)

                if data.get("status") != "ok":
                    return None

                return data.get("data", {})

    except Exception:
        logger.exception("YTS API request failed")
        return None


async def get_movies(
    page=1,
    sort_by="date_added",
    genre=None,
    query=None,
):

    params = {
        "limit": MOVIES_PER_PAGE,
        "page": page,
        "sort_by": sort_by,
        "order_by": "desc",
    }

    if genre:
        params["genre"] = genre

    if query:
        params["query_term"] = query

    data = await yts_request("list_movies", params)

    if not data:
        return []

    return data.get("movies") or []


async def get_movie_details(movie_id):

    data = await yts_request(
        "movie_details",
        {
            "movie_id": movie_id,
            "with_images": "true",
            "with_cast": "true",
        },
    )

    if not data:
        return None

    return data.get("movie")


async def get_similar_movies(movie_id):

    data = await yts_request(
        "movie_suggestions",
        {"movie_id": movie_id},
    )

    if not data:
        return []

    return data.get("movies") or []


# =========================================================
# HELPERS
# =========================================================

def register_update_user(update):

    user = update.effective_user

    if user:
        db.register_user(
            str(user.id),
            user.username or "",
        )


def safe_text(value):
    return html.escape(str(value or ""))


def get_movie_title(movie):
    return movie.get("title_long") or movie.get("title") or "Unknown"


def get_magnet(movie, torrent):

    info_hash = torrent.get("hash", "").strip()

    if not info_hash:
        return None

    title = get_movie_title(movie)

    trackers = [
        "udp://open.demonii.com:1337/announce",
        "udp://tracker.openbittorrent.com:80",
        "udp://tracker.opentrackr.org:1337/announce",
        "udp://tracker.torrent.eu.org:451/announce",
    ]

    magnet = (
        f"magnet:?xt=urn:btih:{info_hash}"
        f"&dn={quote(title)}"
    )

    for tracker in trackers:
        magnet += f"&tr={quote(tracker, safe='')}"

    return magnet


def movie_keyboard(movie):

    movie_id = movie.get("id")
    title = get_movie_title(movie)

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🎬 التفاصيل",
                callback_data=f"movie:{movie_id}",
            ),
            InlineKeyboardButton(
                "⭐ المفضلة",
                callback_data=f"fav:{movie_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔔 قائمة المتابعة",
                callback_data=f"watch:{movie_id}",
            ),
        ],
    ])


def movie_caption(movie):

    title = safe_text(get_movie_title(movie))
    year = movie.get("year", "N/A")
    rating = movie.get("rating", "N/A")
    runtime = movie.get("runtime", "N/A")

    genres = movie.get("genres") or []
    genres_text = ", ".join(genres[:4]) if genres else "غير محدد"

    summary = movie.get("summary") or movie.get("description_full") or ""
    summary = safe_text(summary[:700])

    return (
        f"🎬 <b>{title}</b>\n\n"
        f"📅 السنة: {year}\n"
        f"⭐ التقييم: {rating}/10\n"
        f"⏱ المدة: {runtime} دقيقة\n"
        f"🎭 التصنيف: {safe_text(genres_text)}\n\n"
        f"📝 <b>القصة:</b>\n"
        f"{summary or 'لا توجد قصة متاحة.'}"
    )


async def send_movie_list(
    update,
    movies,
    title="🎬 الأفلام",
    page=1,
    list_type="latest",
):

    if not movies:
        await update.effective_message.reply_text(
            "❌ مفيش أفلام متاحة حاليًا."
        )
        return

    await update.effective_message.reply_text(
        f"<b>{safe_text(title)}</b>\n"
        f"الصفحة: {page}",
        parse_mode=ParseMode.HTML,
    )

    for movie in movies:

        poster = movie.get("medium_cover_image") or movie.get("large_cover_image")

        caption = movie_caption(movie)

        keyboard = movie_keyboard(movie)

        if poster:
            try:
                await update.effective_message.reply_photo(
                    photo=poster,
                    caption=caption[:1024],
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
                continue
            except Exception:
                logger.warning("Could not send movie poster")

        await update.effective_message.reply_text(
            caption[:4000],
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    navigation = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⬅️ السابق",
                callback_data=f"page:{list_type}:{max(1, page - 1)}",
            ),
            InlineKeyboardButton(
                "التالي ➡️",
                callback_data=f"page:{list_type}:{page + 1}",
            ),
        ]
    ])

    await update.effective_message.reply_text(
        "تصفح صفحات الأفلام:",
        reply_markup=navigation,
    )


# =========================================================
# COMMANDS
# =========================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    register_update_user(update)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🆕 أحدث الأفلام", callback_data="page:latest:1"),
            InlineKeyboardButton("🔥 الأعلى تقييمًا", callback_data="page:top:1"),
        ],
        [
            InlineKeyboardButton("⭐ المفضلة", callback_data="myfavorites"),
            InlineKeyboardButton("🔔 قائمة المتابعة", callback_data="mywatchlist"),
        ],
        [
            InlineKeyboardButton("🎭 التصنيفات", callback_data="categories"),
        ],
    ])

    await update.message.reply_text(
        "🎬 <b>أهلاً بك في YTS Movies Pro Ultra</b>\n\n"
        "تقدر تبحث عن الأفلام وتشوف أحدث الإصدارات "
        "وتضيف أفلامك للمفضلة وقائمة المتابعة.\n\n"
        "🔎 ابعت اسم الفيلم للبحث عنه.\n\n"
        "اكتب /help لعرض الأوامر.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    register_update_user(update)

    await update.message.reply_text(
        "📚 <b>أوامر البوت</b>\n\n"
        "/start - تشغيل البوت\n"
        "/latest - أحدث الأفلام\n"
        "/top - أعلى الأفلام تقييمًا\n"
        "/favorites - أفلامك المفضلة\n"
        "/watchlist - قائمة المتابعة\n"
        "/stats - إحصائيات المستخدمين (للأدمن فقط)\n\n"
        "🔎 ابعت اسم أي فيلم للبحث عنه.",
        parse_mode=ParseMode.HTML,
    )


async def latest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    register_update_user(update)

    movies = await get_movies(page=1, sort_by="date_added")

    await send_movie_list(
        update,
        movies,
        "🆕 أحدث الأفلام",
        1,
        "latest",
    )


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    register_update_user(update)

    movies = await get_movies(page=1, sort_by="rating")

    await send_movie_list(
        update,
        movies,
        "🔥 أعلى الأفلام تقييمًا",
        1,
        "top",
    )


# =========================================================
# USER STATISTICS - ADMIN ONLY
# =========================================================

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    register_update_user(update)

    user = update.effective_user

    if not ADMIN_ID:
        await update.message.reply_text(
            "⚠️ لم يتم ضبط ADMIN_ID في إعدادات البوت."
        )
        return

    if not user or str(user.id) != ADMIN_ID:
        await update.message.reply_text(
            "⛔ الأمر ده متاح للأدمن فقط."
        )
        return

    total_users = db.get_user_count()

    await update.message.reply_text(
        "📊 <b>إحصائيات YTS Movies Pro Ultra</b>\n\n"
        f"👥 إجمالي المستخدمين: <b>{total_users}</b>\n"
        "💾 قاعدة البيانات: SQLite\n"
        f"🔔 فحص الأفلام كل: {CHECK_INTERVAL // 60} دقائق",
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# SEARCH
# =========================================================

async def text_search(update: Update, context: ContextTypes.DEFAULT_TYPE):

    register_update_user(update)

    query = (update.message.text or "").strip()

    if not query or query.startswith("/"):
        return

    if len(query) < 2:
        await update.message.reply_text(
            "اكتب اسم فيلم أطول شوية للبحث."
        )
        return

    await update.message.reply_text(
        f"🔎 جاري البحث عن: {safe_text(query)}",
        parse_mode=ParseMode.HTML,
    )

    movies = await get_movies(
        page=1,
        sort_by="year",
        query=query,
    )

    await send_movie_list(
        update,
        movies,
        f"نتائج البحث عن {query}",
        1,
        "latest",
    )


# =========================================================
# FAVORITES AND WATCHLIST
# =========================================================

async def favorites_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    register_update_user(update)

    rows = db.get_favorites(update.effective_user.id)

    if not rows:
        await update.message.reply_text(
            "⭐ قائمة المفضلة فارغة."
        )
        return

    keyboard = []

    for row in rows:
        keyboard.append([
            InlineKeyboardButton(
                f"🎬 {row['title'][:45]}",
                callback_data=f"movie:{row['movie_id']}",
            )
        ])

    await update.message.reply_text(
        f"⭐ أفلامك المفضلة: {len(rows)}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    register_update_user(update)

    rows = db.get_watchlist(update.effective_user.id)

    if not rows:
        await update.message.reply_text(
            "🔔 قائمة المتابعة فارغة."
        )
        return

    keyboard = []

    for row in rows:
        keyboard.append([
            InlineKeyboardButton(
                f"🎬 {row['title'][:45]}",
                callback_data=f"movie:{row['movie_id']}",
            )
        ])

    await update.message.reply_text(
        f"🔔 قائمة المتابعة: {len(rows)} فيلم",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================================================
# CATEGORIES
# =========================================================

GENRES = [
    "Action",
    "Adventure",
    "Animation",
    "Comedy",
    "Crime",
    "Drama",
    "Fantasy",
    "Horror",
    "Mystery",
    "Romance",
    "Sci-Fi",
    "Thriller",
    "Documentary",
]


async def categories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    register_update_user(update)

    keyboard = []

    for genre in GENRES:
        keyboard.append([
            InlineKeyboardButton(
                genre,
                callback_data=f"genre:{genre}",
            )
        ])

    await update.message.reply_text(
        "🎭 اختار تصنيف الأفلام:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================================================
# CALLBACK HANDLER
# =========================================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    if user:
        db.register_user(
            str(user.id),
            user.username or "",
        )

    data = query.data or ""

    # عرض أحدث الأفلام أو الأعلى تقييمًا
    if data.startswith("page:"):

        _, list_type, page = data.split(":")
        page = max(1, int(page))

        sort_by = "rating" if list_type == "top" else "date_added"

        movies = await get_movies(
            page=page,
            sort_by=sort_by,
        )

        if not movies:
            await query.message.reply_text(
                "❌ مفيش نتائج في الصفحة دي."
            )
            return

        await send_movie_list(
            update,
            movies,
            "🔥 الأعلى تقييمًا" if list_type == "top" else "🆕 أحدث الأفلام",
            page,
            list_type,
        )

        return

    # التصنيفات
    if data.startswith("genre:"):

        genre = data.split(":", 1)[1]

        movies = await get_movies(
            page=1,
            sort_by="rating",
            genre=genre,
        )

        await send_movie_list(
            update,
            movies,
            f"🎭 تصنيف {genre}",
        )

        return

    # عرض المفضلة
    if data == "myfavorites":

        rows = db.get_favorites(user.id)

        if not rows:
            await query.message.reply_text("⭐ المفضلة فارغة.")
            return

        keyboard = [
            [
                InlineKeyboardButton(
                    row["title"][:45],
                    callback_data=f"movie:{row['movie_id']}",
                )
            ]
            for row in rows
        ]

        await query.message.reply_text(
            "⭐ أفلامك المفضلة:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

        return

    # عرض قائمة المتابعة
    if data == "mywatchlist":

        rows = db.get_watchlist(user.id)

        if not rows:
            await query.message.reply_text("🔔 قائمة المتابعة فارغة.")
            return

        keyboard = [
            [
                InlineKeyboardButton(
                    row["title"][:45],
                    callback_data=f"movie:{row['movie_id']}",
                )
            ]
            for row in rows
        ]

        await query.message.reply_text(
            "🔔 قائمة المتابعة:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

        return

    # إضافة أو إزالة من المفضلة
    if data.startswith("fav:"):

        movie_id = int(data.split(":")[1])

        movie = await get_movie_details(movie_id)

        if not movie:
            await query.message.reply_text(
                "❌ لم أستطع تحميل بيانات الفيلم."
            )
            return

        added = db.toggle_favorite(
            user.id,
            movie_id,
            get_movie_title(movie),
        )

        message = (
            "⭐ تمت إضافة الفيلم للمفضلة."
            if added
            else "🗑 تم حذف الفيلم من المفضلة."
        )

        await query.message.reply_text(message)

        return

    # إضافة أو إزالة من قائمة المتابعة
    if data.startswith("watch:"):

        movie_id = int(data.split(":")[1])

        movie = await get_movie_details(movie_id)

        if not movie:
            await query.message.reply_text(
                "❌ لم أستطع تحميل بيانات الفيلم."
            )
            return

        added = db.toggle_watchlist(
            user.id,
            movie_id,
            get_movie_title(movie),
        )

        message = (
            "🔔 تمت إضافة الفيلم لقائمة المتابعة."
            if added
            else "🗑 تم حذف الفيلم من قائمة المتابعة."
        )

        await query.message.reply_text(message)

        return

    # تفاصيل الفيلم
    if data.startswith("movie:"):

        movie_id = int(data.split(":")[1])

        movie = await get_movie_details(movie_id)

        if not movie:
            await query.message.reply_text(
                "❌ لم أستطع تحميل تفاصيل الفيلم."
            )
            return

        torrents = movie.get("torrents") or []

        buttons = []

        for torrent in torrents:

            quality = torrent.get("quality", "Unknown")
            torrent_url = torrent.get("url")
            magnet = get_magnet(movie, torrent)

            row = []

            if torrent_url:
                row.append(
                    InlineKeyboardButton(
                        f"⬇️ Torrent {quality}",
                        url=torrent_url,
                    )
                )

            if magnet:
                row.append(
                    InlineKeyboardButton(
                        f"🧲 Magnet {quality}",
                        callback_data=f"magnet:{movie_id}:{quality}",
                    )
                )

            if row:
                buttons.append(row)

        buttons.append([
            InlineKeyboardButton(
                "⭐ إضافة للمفضلة",
                callback_data=f"fav:{movie_id}",
            ),
            InlineKeyboardButton(
                "🔔 متابعة",
                callback_data=f"watch:{movie_id}",
            ),
        ])

        poster = movie.get("large_cover_image") or movie.get("medium_cover_image")

        caption = movie_caption(movie)

        keyboard = InlineKeyboardMarkup(buttons)

        if poster:
            try:
                await query.message.reply_photo(
                    photo=poster,
                    caption=caption[:1024],
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
                return
            except Exception:
                logger.exception("Failed to send detail poster")

        await query.message.reply_text(
            caption[:4000],
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

        return

    # إرسال رابط Magnet كنص قابل للنسخ
    if data.startswith("magnet:"):

        _, movie_id, quality = data.split(":", 2)

        movie = await get_movie_details(int(movie_id))

        if not movie:
            await query.message.reply_text(
                "❌ الفيلم غير متاح حاليًا."
            )
            return

        torrent = next(
            (
                item for item in movie.get("torrents", [])
                if item.get("quality") == quality
            ),
            None,
        )

        if not torrent:
            await query.message.reply_text(
                "❌ رابط Magnet غير متاح للجودة دي."
            )
            return

        magnet = get_magnet(movie, torrent)

        if not magnet:
            await query.message.reply_text(
                "❌ لم أستطع إنشاء رابط Magnet."
            )
            return

        await query.message.reply_text(
            "🧲 <b>رابط Magnet:</b>\n\n"
            f"<code>{html.escape(magnet)}</code>\n\n"
            "انسخ الرابط وافتحه في برنامج Torrent.",
            parse_mode=ParseMode.HTML,
        )

        return


# =========================================================
# NEW MOVIES NOTIFICATIONS
# =========================================================

async def check_new_movies(context: ContextTypes.DEFAULT_TYPE):

    try:
        movies = await get_movies(
            page=1,
            sort_by="date_added",
        )

        if not movies:
            return

        current_ids = [
            movie.get("id")
            for movie in movies
            if movie.get("id")
        ]

        old_value = db.get_setting("last_movie_ids")

        # أول تشغيل: حفظ القائمة الحالية بدون إرسال إشعارات قديمة
        if not old_value:

            db.set_setting(
                "last_movie_ids",
                json.dumps(current_ids),
            )

            logger.info("Notification baseline initialized")
            return

        try:
            old_ids = set(json.loads(old_value))
        except Exception:
            old_ids = set()

        new_movies = [
            movie for movie in movies
            if movie.get("id") not in old_ids
        ]

        db.set_setting(
            "last_movie_ids",
            json.dumps(current_ids),
        )

        if not new_movies:
            return

        users = db.get_all_users()

        logger.info(
            "Found %s new movies for %s users",
            len(new_movies),
            len(users),
        )

        for movie in new_movies:

            title = safe_text(get_movie_title(movie))

            message = (
                "🆕 <b>فيلم جديد على YTS!</b>\n\n"
                f"🎬 {title}\n"
                f"📅 السنة: {movie.get('year', 'N/A')}\n"
                f"⭐ التقييم: {movie.get('rating', 'N/A')}/10"
            )

            keyboard = movie_keyboard(movie)

            for user_id in users:

                try:
                    await context.bot.send_message(
                        chat_id=int(user_id),
                        text=message,
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard,
                    )

                except Exception as error:
                    logger.warning(
                        "Could not notify user %s: %s",
                        user_id,
                        error,
                    )

    except Exception:
        logger.exception("Notification job failed")


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update, context):

    logger.error(
        "Exception while handling update:",
        exc_info=context.error,
    )


# =========================================================
# MAIN APPLICATION
# =========================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing. Add it to environment variables."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("latest", latest_command))
    application.add_handler(CommandHandler("top", top_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("favorites", favorites_command))
    application.add_handler(CommandHandler("watchlist", watchlist_command))
    application.add_handler(CommandHandler("categories", categories_command))

    # Callbacks
    application.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    # Text search
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_search,
        )
    )

    # Errors
    application.add_error_handler(error_handler)

    # Notifications
    if application.job_queue:
        application.job_queue.run_repeating(
            check_new_movies,
            interval=CHECK_INTERVAL,
            first=15,
            name="new-movies-check",
        )
    else:
        logger.warning(
            "JobQueue is unavailable. Install python-telegram-bot[job-queue]."
        )

    logger.info("YTS Movies Pro Ultra is starting...")

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
