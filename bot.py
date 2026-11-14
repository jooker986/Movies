
import os
import json
import html
import logging
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from urllib.parse import quote

import aiohttp

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =====================================================
# SETTINGS
# =====================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()

# API الأساسي + عناوين احتياطية
API_BASES = [
    "https://movies-api.accel.li/api/v2",
    "https://yts.gg/api/v2",
    "https://yts.mx/api/v2",
]

DB_PATH = Path(os.getenv("DB_PATH", "yts_movies.db"))

MOVIES_PER_PAGE = 5
CHECK_INTERVAL = 600  # كل 10 دقائق

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("YTS-BOT")


# =====================================================
# DATABASE
# =====================================================

class Database:

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")

        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        with self.connect() as conn:
            conn.executescript("""
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

    def register_user(self, user_id, username=""):
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO users (user_id, username)
                VALUES (?, ?)
                ON CONFLICT(user_id)
                DO UPDATE SET username = excluded.username
            """, (str(user_id), username or ""))

    def get_user_count(self):
        with self.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]

    def get_all_users(self):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT user_id FROM users"
            ).fetchall()

            return [row["user_id"] for row in rows]

    def toggle_item(self, table, user_id, movie_id, title):
        if table not in ("favorites", "watchlist"):
            return False

        with self.connect() as conn:
            existing = conn.execute(
                f"SELECT 1 FROM {table} WHERE user_id=? AND movie_id=?",
                (str(user_id), int(movie_id)),
            ).fetchone()

            if existing:
                conn.execute(
                    f"DELETE FROM {table} WHERE user_id=? AND movie_id=?",
                    (str(user_id), int(movie_id)),
                )
                return False

            conn.execute(
                f"INSERT OR IGNORE INTO {table} (user_id, movie_id, title) VALUES (?, ?, ?)",
                (str(user_id), int(movie_id), title),
            )
            return True

    def get_items(self, table, user_id):
        if table not in ("favorites", "watchlist"):
            return []

        with self.connect() as conn:
            return conn.execute(
                f"SELECT movie_id, title FROM {table} WHERE user_id=? ORDER BY title",
                (str(user_id),),
            ).fetchall()

    def get_setting(self, key):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT setting_value FROM settings WHERE setting_key=?",
                (key,),
            ).fetchone()

            return row["setting_value"] if row else None

    def set_setting(self, key, value):
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO settings (setting_key, setting_value)
                VALUES (?, ?)
                ON CONFLICT(setting_key)
                DO UPDATE SET setting_value=excluded.setting_value
            """, (key, value))


db = Database(DB_PATH)


# =====================================================
# USER REGISTRATION
# =====================================================

def register_user(update):

    user = update.effective_user

    if user:
        db.register_user(
            str(user.id),
            user.username or "",
        )


# =====================================================
# YTS API
# =====================================================

async def yts_request(endpoint, params=None):

    timeout = aiohttp.ClientTimeout(total=35)

    headers = {
        "User-Agent": "Mozilla/5.0 YTSMoviesTelegramBot/3.0",
        "Accept": "application/json",
    }

    last_error = "Unknown API error"

    try:
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
        ) as session:

            for base in API_BASES:

                url = f"{base}/{endpoint}.json"

                try:
                    logger.info("API REQUEST: %s", url)

                    async with session.get(
                        url,
                        params=params or {},
                    ) as response:

                        body = await response.text()

                        logger.info(
                            "API RESPONSE: HTTP %s",
                            response.status,
                        )

                        if response.status != 200:
                            last_error = (
                                f"HTTP {response.status}: {body[:250]}"
                            )
                            logger.warning(
                                "API failed on %s: %s",
                                base,
                                last_error,
                            )
                            continue

                        try:
                            result = json.loads(body)
                        except json.JSONDecodeError:
                            last_error = "Invalid JSON response"
                            logger.warning(
                                "Invalid JSON from %s",
                                base,
                            )
                            continue

                        if result.get("status") != "ok":
                            last_error = result.get(
                                "status_message",
                                "API returned an error",
                            )
                            logger.warning(
                                "API message from %s: %s",
                                base,
                                last_error,
                            )
                            continue

                        logger.info("API SUCCESS: %s", base)
                        return result.get("data") or {}

                except Exception as error:
                    last_error = str(error)
                    logger.exception(
                        "Connection failed: %s",
                        base,
                    )

    except Exception as error:
        last_error = str(error)
        logger.exception("API session failed")

    logger.error(
        "ALL YTS API SOURCES FAILED. Last error: %s",
        last_error,
    )

    return None


async def get_movies(
    page=1,
    sort_by="date_added",
    genre=None,
    query=None,
    limit=MOVIES_PER_PAGE,
):

    params = {
        "limit": max(1, min(int(limit), 50)),
        "page": max(1, int(page)),
        "sort_by": sort_by,
        "order_by": "desc",
    }

    if genre:
        params["genre"] = genre

    if query:
        params["query_term"] = query

    data = await yts_request("list_movies", params)

    if data is None:
        return None

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


# =====================================================
# HELPERS
# =====================================================

def safe(value):
    return html.escape(str(value or ""))


def movie_title(movie):
    return movie.get("title_long") or movie.get("title") or "Unknown"


def magnet_link(movie, torrent):

    info_hash = torrent.get("hash")

    if not info_hash:
        return None

    title = quote(movie_title(movie))

    trackers = [
        "udp://tracker.opentrackr.org:1337/announce",
        "udp://tracker.torrent.eu.org:451/announce",
        "udp://open.demonii.com:1337/announce",
        "udp://tracker.openbittorrent.com:80",
    ]

    magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={title}"

    for tracker in trackers:
        magnet += f"&tr={quote(tracker, safe='')}"

    return magnet


def movie_caption(movie):

    title = safe(movie_title(movie))
    year = movie.get("year", "N/A")
    rating = movie.get("rating", "N/A")
    runtime = movie.get("runtime", "N/A")

    genres = movie.get("genres") or []
    genres_text = ", ".join(genres[:4]) if genres else "Unknown"

    summary = (
        movie.get("summary")
        or movie.get("description_full")
        or "لا توجد قصة متاحة."
    )

    summary = safe(summary[:350])

    return (
        f"🎬 <b>{title}</b>\n\n"
        f"📅 السنة: {year}\n"
        f"⭐ التقييم: {rating}/10\n"
        f"⏱ المدة: {runtime} دقيقة\n"
        f"🎭 التصنيف: {safe(genres_text)}\n\n"
        f"📝 <b>القصة:</b>\n{summary}"
    )


def movie_keyboard(movie):

    movie_id = movie.get("id")

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🎬 التفاصيل والجودات",
                callback_data=f"movie:{movie_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "⭐ المفضلة",
                callback_data=f"fav:{movie_id}",
            ),
            InlineKeyboardButton(
                "🔔 متابعة",
                callback_data=f"watch:{movie_id}",
            ),
        ],
    ])


# =====================================================
# SEND MOVIE RESULTS
# =====================================================

async def send_movie_list(
    update,
    movies,
    title="🎬 الأفلام",
    page=1,
    list_type="latest",
):

    message = update.effective_message

    if movies is None:
        await message.reply_text(
            "⚠️ حصل خطأ أثناء الاتصال بمصدر الأفلام.\n"
            "راجع سجل Actions لمعرفة تفاصيل الخطأ."
        )
        return

    if not movies:
        await message.reply_text(
            "🔎 مفيش نتائج أفلام للطلب ده."
        )
        return

    await message.reply_text(
        f"<b>{safe(title)}</b>\n"
        f"الصفحة: {page}",
        parse_mode=ParseMode.HTML,
    )

    for movie in movies:

        caption = movie_caption(movie)
        poster = (
            movie.get("medium_cover_image")
            or movie.get("large_cover_image")
        )

        keyboard = movie_keyboard(movie)

        if poster:
            try:
                await message.reply_photo(
                    photo=poster,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
                continue
            except Exception:
                logger.exception(
                    "Could not send poster for movie %s",
                    movie.get("id"),
                )

        await message.reply_text(
            caption,
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

    await message.reply_text(
        "تصفح صفحات الأفلام:",
        reply_markup=navigation,
    )


# =====================================================
# COMMANDS
# =====================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    register_user(update)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🆕 أحدث الأفلام",
                callback_data="page:latest:1",
            ),
            InlineKeyboardButton(
                "🔥 الأعلى تقييمًا",
                callback_data="page:top:1",
            ),
        ],
        [
            InlineKeyboardButton(
                "⭐ المفضلة",
                callback_data="myfavorites",
            ),
            InlineKeyboardButton(
                "🔔 المتابعة",
                callback_data="mywatchlist",
            ),
        ],
        [
            InlineKeyboardButton(
                "🎭 التصنيفات",
                callback_data="categories",
            ),
        ],
    ])

    await update.message.reply_text(
        "🎬 <b>YTS Movies Pro Ultra</b>\n\n"
        "أهلاً بيك!\n"
        "ابعت اسم الفيلم للبحث عنه.\n\n"
        "استخدم الأزرار للتصفح أو اكتب /help.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def help_command(update, context):

    register_user(update)

    await update.message.reply_text(
        "📚 <b>أوامر البوت</b>\n\n"
        "/start - تشغيل البوت\n"
        "/latest - أحدث الأفلام\n"
        "/top - الأعلى تقييمًا\n"
        "/favorites - المفضلة\n"
        "/watchlist - قائمة المتابعة\n"
        "/categories - التصنيفات\n"
        "/stats - إحصائيات الأدمن\n"
        "/testapi - اختبار الاتصال بالـ API للأدمن\n\n"
        "🔎 ابعت اسم الفيلم للبحث.",
        parse_mode=ParseMode.HTML,
    )


async def latest_command(update, context):

    register_user(update)

    movies = await get_movies(sort_by="date_added")

    await send_movie_list(
        update,
        movies,
        "🆕 أحدث الأفلام",
        1,
        "latest",
    )


async def top_command(update, context):

    register_user(update)

    movies = await get_movies(sort_by="rating")

    await send_movie_list(
        update,
        movies,
        "🔥 الأعلى تقييمًا",
        1,
        "top",
    )


async def stats_command(update, context):

    register_user(update)

    user = update.effective_user

    if not ADMIN_ID:
        await update.message.reply_text(
            "⚠️ لم يتم ضبط ADMIN_ID في GitHub Secrets."
        )
        return

    if not user or str(user.id) != ADMIN_ID:
        await update.message.reply_text(
            "⛔ الأمر متاح للأدمن فقط."
        )
        return

    total = db.get_user_count()

    await update.message.reply_text(
        "📊 <b>إحصائيات البوت</b>\n\n"
        f"👥 إجمالي المستخدمين: <b>{total}</b>\n"
        "💾 قاعدة البيانات: SQLite\n"
        f"🔔 الفحص كل {CHECK_INTERVAL // 60} دقائق",
        parse_mode=ParseMode.HTML,
    )


async def testapi_command(update, context):

    register_user(update)

    user = update.effective_user

    if not ADMIN_ID or not user or str(user.id) != ADMIN_ID:
        await update.message.reply_text(
            "⛔ الأمر متاح للأدمن فقط."
        )
        return

    await update.message.reply_text(
        "🔄 جاري اختبار الاتصال بـ YTS API..."
    )

    movies = await get_movies(limit=3)

    if movies is None:
        await update.message.reply_text(
            "❌ فشل الاتصال بجميع مصادر API.\n"
            "افتح Actions وشوف سجل الأخطاء."
        )
        return

    await update.message.reply_text(
        f"✅ الاتصال ناجح!\n"
        f"🎬 عدد الأفلام المستلمة في الاختبار: {len(movies)}"
    )


async def favorites_command(update, context):

    register_user(update)

    rows = db.get_items("favorites", update.effective_user.id)

    if not rows:
        await update.message.reply_text("⭐ المفضلة فارغة.")
        return

    keyboard = [
        [
            InlineKeyboardButton(
                row["title"][:50],
                callback_data=f"movie:{row['movie_id']}",
            )
        ]
        for row in rows
    ]

    await update.message.reply_text(
        f"⭐ المفضلة: {len(rows)} فيلم",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def watchlist_command(update, context):

    register_user(update)

    rows = db.get_items("watchlist", update.effective_user.id)

    if not rows:
        await update.message.reply_text("🔔 قائمة المتابعة فارغة.")
        return

    keyboard = [
        [
            InlineKeyboardButton(
                row["title"][:50],
                callback_data=f"movie:{row['movie_id']}",
            )
        ]
        for row in rows
    ]

    await update.message.reply_text(
        f"🔔 قائمة المتابعة: {len(rows)} فيلم",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =====================================================
# CATEGORIES
# =====================================================

GENRES = [
    "Action",
    "Adventure",
    "Animation",
    "Comedy",
    "Crime",
    "Documentary",
    "Drama",
    "Family",
    "Fantasy",
    "Horror",
    "Mystery",
    "Romance",
    "Sci-Fi",
    "Thriller",
    "War",
]


async def categories_command(update, context):

    register_user(update)

    keyboard = [
        [
            InlineKeyboardButton(
                genre,
                callback_data=f"genre:{genre}",
            )
        ]
        for genre in GENRES
    ]

    await update.message.reply_text(
        "🎭 اختار تصنيف الأفلام:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =====================================================
# TEXT SEARCH
# =====================================================

async def text_search(update, context):

    register_user(update)

    search_text = (update.message.text or "").strip()

    if len(search_text) < 2:
        await update.message.reply_text(
            "اكتب اسم فيلم صحيح للبحث."
        )
        return

    status = await update.message.reply_text(
        f"🔎 جاري البحث عن: {safe(search_text)}",
        parse_mode=ParseMode.HTML,
    )

    movies = await get_movies(
        page=1,
        sort_by="date_added",
        query=search_text,
    )

    try:
        await status.delete()
    except Exception:
        pass

    await send_movie_list(
        update,
        movies,
        f"نتائج البحث: {search_text}",
        1,
        "latest",
    )


# =====================================================
# CALLBACK HANDLER
# =====================================================

async def callback_handler(update, context):

    query = update.callback_query
    await query.answer()

    user = query.from_user

    if user:
        db.register_user(
            str(user.id),
            user.username or "",
        )

    data = query.data or ""

    # Pagination
    if data.startswith("page:"):

        try:
            _, list_type, page = data.split(":")
            page = max(1, int(page))
        except ValueError:
            return

        sort_by = (
            "rating"
            if list_type == "top"
            else "date_added"
        )

        movies = await get_movies(
            page=page,
            sort_by=sort_by,
        )

        await send_movie_list(
            update,
            movies,
            "🔥 الأعلى تقييمًا" if list_type == "top" else "🆕 أحدث الأفلام",
            page,
            list_type,
        )
        return

    # Categories menu
    if data == "categories":

        keyboard = [
            [
                InlineKeyboardButton(
                    genre,
                    callback_data=f"genre:{genre}",
                )
            ]
            for genre in GENRES
        ]

        await query.message.reply_text(
            "🎭 اختار تصنيف الأفلام:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # Genre
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

    # Favorites list
    if data == "myfavorites":

        rows = db.get_items("favorites", user.id)

        if not rows:
            await query.message.reply_text("⭐ المفضلة فارغة.")
            return

        keyboard = [
            [
                InlineKeyboardButton(
                    row["title"][:50],
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

    # Watchlist
    if data == "mywatchlist":

        rows = db.get_items("watchlist", user.id)

        if not rows:
            await query.message.reply_text("🔔 قائمة المتابعة فارغة.")
            return

        keyboard = [
            [
                InlineKeyboardButton(
                    row["title"][:50],
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

    # Favorite toggle
    if data.startswith("fav:"):

        movie_id = int(data.split(":")[1])
        movie = await get_movie_details(movie_id)

        if not movie:
            await query.message.reply_text(
                "❌ لم أستطع تحميل بيانات الفيلم."
            )
            return

        added = db.toggle_item(
            "favorites",
            user.id,
            movie_id,
            movie_title(movie),
        )

        await query.message.reply_text(
            "⭐ تمت الإضافة للمفضلة."
            if added
            else "🗑 تم الحذف من المفضلة."
        )
        return

    # Watchlist toggle
    if data.startswith("watch:"):

        movie_id = int(data.split(":")[1])
        movie = await get_movie_details(movie_id)

        if not movie:
            await query.message.reply_text(
                "❌ لم أستطع تحميل بيانات الفيلم."
            )
            return

        added = db.toggle_item(
            "watchlist",
            user.id,
            movie_id,
            movie_title(movie),
        )

        await query.message.reply_text(
            "🔔 تمت الإضافة لقائمة المتابعة."
            if added
            else "🗑 تم الحذف من قائمة المتابعة."
        )
        return

    # Movie details
    if data.startswith("movie:"):

        movie_id = int(data.split(":")[1])
        movie = await get_movie_details(movie_id)

        if not movie:
            await query.message.reply_text(
                "❌ لم أستطع تحميل تفاصيل الفيلم."
            )
            return

        buttons = []

        for torrent in (movie.get("torrents") or [])[:8]:

            quality = torrent.get("quality", "Unknown")
            torrent_url = torrent.get("url")
            magnet = magnet_link(movie, torrent)

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
                "⭐ المفضلة",
                callback_data=f"fav:{movie_id}",
            ),
            InlineKeyboardButton(
                "🔔 متابعة",
                callback_data=f"watch:{movie_id}",
            ),
        ])

        caption = movie_caption(movie)
        poster = (
            movie.get("large_cover_image")
            or movie.get("medium_cover_image")
        )

        if poster:
            try:
                await query.message.reply_photo(
                    photo=poster,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
                return
            except Exception:
                logger.exception("Failed to send movie details poster")

        await query.message.reply_text(
            caption,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # Magnet link
    if data.startswith("magnet:"):

        try:
            _, movie_id, quality = data.split(":", 2)
        except ValueError:
            return

        movie = await get_movie_details(int(movie_id))

        if not movie:
            await query.message.reply_text(
                "❌ الفيلم غير متاح."
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
                "❌ الجودة دي غير متاحة."
            )
            return

        magnet = magnet_link(movie, torrent)

        if not magnet:
            await query.message.reply_text(
                "❌ لم أستطع إنشاء رابط Magnet."
            )
            return

        await query.message.reply_text(
            "🧲 <b>رابط Magnet:</b>\n\n"
            f"<code>{safe(magnet)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return


# =====================================================
# NEW MOVIES NOTIFICATIONS
# =====================================================

async def check_new_movies(context):

    try:
        movies = await get_movies(
            page=1,
            sort_by="date_added",
            limit=50,
        )

        if movies is None:
            logger.error("Notification check failed: API unavailable")
            return

        current_ids = [
            movie["id"]
            for movie in movies
            if movie.get("id")
        ]

        old_value = db.get_setting("last_movie_ids")

        # أول تشغيل: حفظ الأفلام الحالية بدون إرسال تنبيهات قديمة
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
            logger.info("No new movies")
            return

        users = db.get_all_users()

        logger.info(
            "New movies: %s | Users: %s",
            len(new_movies),
            len(users),
        )

        for movie in new_movies:

            message = (
                "🆕 <b>فيلم جديد على YTS!</b>\n\n"
                f"🎬 {safe(movie_title(movie))}\n"
                f"📅 السنة: {movie.get('year', 'N/A')}\n"
                f"⭐ التقييم: {movie.get('rating', 'N/A')}/10"
            )

            for user_id in users:

                try:
                    await context.bot.send_message(
                        chat_id=int(user_id),
                        text=message,
                        parse_mode=ParseMode.HTML,
                        reply_markup=movie_keyboard(movie),
                    )

                except Exception:
                    logger.exception(
                        "Could not notify user %s",
                        user_id,
                    )

    except Exception:
        logger.exception("Notification task failed")


# =====================================================
# ERROR HANDLER
# =====================================================

async def error_handler(update, context):

    logger.error(
        "Unhandled error:",
        exc_info=context.error,
    )


# =====================================================
# MAIN
# =====================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing in GitHub Secrets."
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
    application.add_handler(CommandHandler("testapi", testapi_command))
    application.add_handler(CommandHandler("favorites", favorites_command))
    application.add_handler(CommandHandler("watchlist", watchlist_command))
    application.add_handler(CommandHandler("categories", categories_command))

    # Buttons
    application.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    # Search
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_search,
        )
    )

    # Error handling
    application.add_error_handler(error_handler)

    # Periodic notifications
    if application.job_queue:
        application.job_queue.run_repeating(
            check_new_movies,
            interval=CHECK_INTERVAL,
            first=20,
            name="new-movies-check",
        )
    else:
        logger.error(
            "JobQueue unavailable. Install python-telegram-bot[job-queue]."
        )

    logger.info("YTS Movies Pro Ultra is starting...")

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
