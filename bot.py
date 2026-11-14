
import os
import re
import time
import html
import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import aiohttp

from telegram import (
    Bot,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    CopyTextButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIGURATION
# =========================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

WATCHED_MOVIES_ENV = os.getenv("WATCHED_MOVIES", "")

IS_GITHUB_ACTIONS = (
    os.getenv("GITHUB_ACTIONS", "false").lower() == "true"
)

SESSION_SECONDS = int(
    os.getenv("GITHUB_SESSION_SECONDS", "20700")
)

CHECK_INTERVAL = int(
    os.getenv("CHECK_INTERVAL", "600")
)

DATA_DIR = Path(os.getenv("YTS_DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_FILE = DATA_DIR / "yts_ultra.db"

API_MIRRORS = [
    "https://yts.mx/api/v2",
    "https://yts.gg/api/v2",
    "https://yts.lt/api/v2",
]

TRACKERS = [
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:80/announce",
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://p4p.arenabg.com:1337/announce",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 YTSMoviesProUltra/3.0",
    "Accept": "application/json",
}

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("YTS_ULTRA")


# =========================================================
# DATABASE
# =========================================================

class Database:

    def __init__(self, path: Path):
        self.path = path
        self.initialize()

    def connect(self):
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def initialize(self):

        with self.connect() as db:

            db.execute("""
                CREATE TABLE IF NOT EXISTS favorites (
                    user_id TEXT NOT NULL,
                    movie_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    added_at INTEGER NOT NULL,
                    PRIMARY KEY (user_id, movie_id)
                )
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS watchlist (
                    user_id TEXT NOT NULL,
                    movie_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    added_at INTEGER NOT NULL,
                    PRIMARY KEY (user_id, movie_id)
                )
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    user_id TEXT NOT NULL,
                    movie_id INTEGER NOT NULL,
                    qualities TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (user_id, movie_id)
                )
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT,
                    first_seen INTEGER NOT NULL
                )
            """)

            db.commit()

    def register_user(self, user_id: str, username: str = ""):

        with self.connect() as db:
            db.execute("""
                INSERT INTO users (user_id, username, first_seen)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET username=excluded.username
            """, (user_id, username, int(time.time())))
            db.commit()

    def add_item(self, table: str, user_id: str, movie_id: int, title: str):

        if table not in ("favorites", "watchlist"):
            return False

        with self.connect() as db:
            cursor = db.execute(
                f"""
                INSERT OR IGNORE INTO {table}
                (user_id, movie_id, title, added_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, movie_id, title, int(time.time()))
            )
            db.commit()
            return cursor.rowcount > 0

    def remove_item(self, table: str, user_id: str, movie_id: int):

        if table not in ("favorites", "watchlist"):
            return

        with self.connect() as db:
            db.execute(
                f"DELETE FROM {table} WHERE user_id=? AND movie_id=?",
                (user_id, movie_id)
            )
            db.commit()

    def get_items(self, table: str, user_id: str):

        if table not in ("favorites", "watchlist"):
            return []

        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT movie_id, title
                FROM {table}
                WHERE user_id=?
                ORDER BY added_at DESC
                """,
                (user_id,)
            ).fetchall()

            return [dict(row) for row in rows]

    def get_all_watchlists(self):

        with self.connect() as db:
            rows = db.execute("""
                SELECT user_id, movie_id, title
                FROM watchlist
            """).fetchall()

            return [dict(row) for row in rows]

    def get_notification(self, user_id: str, movie_id: int):

        with self.connect() as db:
            row = db.execute("""
                SELECT qualities FROM notifications
                WHERE user_id=? AND movie_id=?
            """, (user_id, movie_id)).fetchone()

            return row["qualities"] if row else ""

    def set_notification(
        self,
        user_id: str,
        movie_id: int,
        qualities: str
    ):

        with self.connect() as db:
            db.execute("""
                INSERT INTO notifications
                (user_id, movie_id, qualities, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, movie_id)
                DO UPDATE SET
                    qualities=excluded.qualities,
                    updated_at=excluded.updated_at
            """, (
                user_id,
                movie_id,
                qualities,
                int(time.time())
            ))
            db.commit()

    def get_user_count(self):

        with self.connect() as db:
            return db.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]


db = Database(DATABASE_FILE)


# =========================================================
# YTS API CLIENT
# =========================================================

class YTSClient:

    def __init__(self):
        self.session = None

    async def start(self):

        if self.session is None or self.session.closed:

            timeout = aiohttp.ClientTimeout(
                total=25,
                connect=10
            )

            connector = aiohttp.TCPConnector(
                limit=30,
                ttl_dns_cache=300
            )

            self.session = aiohttp.ClientSession(
                headers=HEADERS,
                timeout=timeout,
                connector=connector
            )

    async def close(self):

        if self.session and not self.session.closed:
            await self.session.close()

    async def request(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None
    ):

        await self.start()

        for mirror in API_MIRRORS:

            url = f"{mirror}/{endpoint}"

            try:

                async with self.session.get(
                    url,
                    params=params
                ) as response:

                    if response.status != 200:
                        logger.warning(
                            "API %s returned HTTP %s",
                            mirror,
                            response.status
                        )
                        continue

                    data = await response.json(
                        content_type=None
                    )

                    if data.get("status") == "ok":
                        return data.get("data", {})

                    logger.warning(
                        "API response was not OK: %s",
                        mirror
                    )

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                logger.warning(
                    "Mirror failed %s: %s",
                    mirror,
                    exc
                )

        return None

    async def list_movies(
        self,
        query_term: str = "",
        sort_by: str = "date_added",
        order_by: str = "desc",
        genre: str = "",
        minimum_rating: int = 0,
        limit: int = 10,
        page: int = 1
    ):

        params = {
            "limit": limit,
            "page": page,
            "sort_by": sort_by,
            "order_by": order_by,
        }

        if query_term:
            params["query_term"] = query_term

        if genre and genre.lower() != "all":
            params["genre"] = genre

        if minimum_rating:
            params["minimum_rating"] = minimum_rating

        data = await self.request(
            "list_movies.json",
            params=params
        )

        if not data:
            return []

        return data.get("movies") or []

    async def smart_search(self, query: str, limit: int = 12):

        query = " ".join(query.strip().split())

        if not query:
            return []

        first, second = await asyncio.gather(
            self.list_movies(
                query_term=query,
                sort_by="seeds",
                limit=limit
            ),
            self.list_movies(
                query_term=query,
                sort_by="download_count",
                limit=limit
            )
        )

        combined = {}

        for movie in first + second:

            movie_id = movie.get("id")

            if movie_id:
                combined[movie_id] = movie

        if len(combined) < 3:

            fallback = re.sub(
                r"[^\w\s]",
                " ",
                query
            )

            fallback = " ".join(
                word for word in fallback.split()
                if not (len(word) == 4 and word.isdigit())
            )

            if fallback and fallback.lower() != query.lower():

                extra = await self.list_movies(
                    query_term=fallback,
                    limit=limit
                )

                for movie in extra:

                    if movie.get("id"):
                        combined[movie["id"]] = movie

        q = query.lower()

        results = sorted(
            combined.values(),
            key=lambda movie: (
                0 if movie.get("title", "").lower() == q
                else 1 if movie.get("title", "").lower().startswith(q)
                else 2,
                -(movie.get("rating") or 0)
            )
        )

        return results[:limit]

    async def movie_details(self, movie_id: int):

        data = await self.request(
            "movie_details.json",
            params={
                "movie_id": movie_id,
                "with_images": "true",
                "with_cast": "true"
            }
        )

        return data.get("movie") if data else None

    async def suggestions(self, movie_id: int):

        data = await self.request(
            "movie_suggestions.json",
            params={"movie_id": movie_id}
        )

        return data.get("movies", []) if data else []

    @staticmethod
    def magnet_link(torrent_hash: str, title: str):

        encoded_title = quote(title[:45])

        link = (
            f"magnet:?xt=urn:btih:{torrent_hash}"
            f"&dn={encoded_title}"
        )

        for tracker in TRACKERS:
            link += f"&tr={quote(tracker, safe='')}"

        return link

    @staticmethod
    def short_magnet(torrent_hash: str, title: str):

        return (
            f"magnet:?xt=urn:btih:{torrent_hash}"
            f"&dn={quote(title[:35])}"
        )


yts = YTSClient()


# =========================================================
# FORMATTING
# =========================================================

def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def movie_title(movie: Dict[str, Any]):

    return (
        movie.get("title_long")
        or f"{movie.get('title', 'Unknown')} ({movie.get('year', '')})"
    )


def quality_signature(movie: Dict[str, Any]):

    qualities = sorted({
        f"{torrent.get('quality', '?')}-{torrent.get('type', '?')}"
        for torrent in movie.get("torrents", [])
    })

    return ",".join(qualities)


def format_movie(movie: Dict[str, Any]):

    title = esc(movie_title(movie))
    rating = esc(movie.get("rating", "N/A"))
    runtime = esc(movie.get("runtime", 0))
    language = esc(movie.get("language", "English"))
    imdb = esc(movie.get("imdb_code", "N/A"))

    genres = movie.get("genres") or ["General"]
    genres_text = ", ".join(esc(g) for g in genres)

    synopsis = (
        movie.get("description_full")
        or movie.get("summary")
        or "لا يوجد وصف متاح حالياً."
    )

    synopsis = str(synopsis)[:800]

    torrents = movie.get("torrents") or []

    torrent_lines = []

    for torrent in torrents:

        torrent_lines.append(
            "• <b>{}</b> | {} | {} | 🟢 {} 🔵 {}".format(
                esc(torrent.get("quality", "?")),
                esc(torrent.get("type", "?")),
                esc(torrent.get("size", "?")),
                esc(torrent.get("seeds", 0)),
                esc(torrent.get("peers", 0))
            )
        )

    torrents_text = (
        "\n".join(torrent_lines)
        if torrent_lines
        else "لا توجد جودات متاحة حالياً."
    )

    return (
        f"🎬 <b>{title}</b>\n\n"
        f"⭐ IMDb: <code>{rating}/10</code>\n"
        f"⏱️ المدة: <code>{runtime} دقيقة</code>\n"
        f"🌐 اللغة: {language}\n"
        f"🎭 التصنيف: {genres_text}\n"
        f"🔗 IMDb ID: <code>{imdb}</code>\n\n"
        f"📝 <b>القصة:</b>\n"
        f"{esc(synopsis)}\n\n"
        f"📥 <b>الجودات المتاحة:</b>\n"
        f"{torrents_text}"
    )


# =========================================================
# KEYBOARDS
# =========================================================

def main_keyboard():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "🆕 أحدث الأفلام",
                callback_data="browse:date_added:1"
            ),
            InlineKeyboardButton(
                "⭐ الأعلى تقييماً",
                callback_data="browse:rating:1"
            )
        ],

        [
            InlineKeyboardButton(
                "🔥 الأكثر تحميلاً",
                callback_data="browse:download_count:1"
            ),
            InlineKeyboardButton(
                "🎭 التصنيفات",
                callback_data="menu:genres"
            )
        ],

        [
            InlineKeyboardButton(
                "❤️ المفضلة",
                callback_data="menu:favorites"
            ),
            InlineKeyboardButton(
                "🔔 المتابعة",
                callback_data="menu:watchlist"
            )
        ],

        [
            InlineKeyboardButton(
                "ℹ️ المساعدة",
                callback_data="menu:help"
            )
        ]

    ])


def movie_keyboard(movie):

    movie_id = int(movie.get("id", 0))
    torrents = movie.get("torrents") or []

    rows = []

    for index, torrent in enumerate(torrents):

        quality = torrent.get("quality", "HD")
        kind = torrent.get("type", "web")
        size = torrent.get("size", "")

        torrent_url = torrent.get("url")

        buttons = []

        if torrent_url:

            buttons.append(
                InlineKeyboardButton(
                    f"⬇️ Torrent {quality} ({size})",
                    url=torrent_url
                )
            )

        buttons.append(
            InlineKeyboardButton(
                f"🧲 Magnet {quality} {kind}",
                callback_data=f"magnet:{movie_id}:{index}"
            )
        )

        rows.append(buttons)

    trailer = movie.get("yt_trailer_code")

    if trailer:

        rows.append([
            InlineKeyboardButton(
                "▶️ مشاهدة التريلر",
                url=f"https://www.youtube.com/watch?v={quote(trailer)}"
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "❤️ أضف للمفضلة",
            callback_data=f"favorite:{movie_id}"
        ),
        InlineKeyboardButton(
            "🔔 تابع الفيلم",
            callback_data=f"watch:{movie_id}"
        )
    ])

    rows.append([
        InlineKeyboardButton(
            "🎯 أفلام مشابهة",
            callback_data=f"similar:{movie_id}"
        ),
        InlineKeyboardButton(
            "🏠 الرئيسية",
            callback_data="menu:home"
        )
    ])

    return InlineKeyboardMarkup(rows)


# =========================================================
# MOVIE LISTS
# =========================================================

async def send_movies_list(
    message,
    movies,
    header,
    pagination=None
):

    if not movies:

        await message.reply_text(
            "❌ لم يتم العثور على أفلام.\n"
            "جرب البحث باسم الفيلم بالإنجليزية.",
            reply_markup=main_keyboard()
        )

        return

    rows = []

    for movie in movies:

        movie_id = movie.get("id")

        if not movie_id:
            continue

        title = movie.get("title", "Unknown")
        year = movie.get("year", "")
        rating = movie.get("rating", "-")

        qualities = "/".join(sorted({
            t.get("quality", "")
            for t in movie.get("torrents", [])
        }))

        label = (
            f"🎬 {title} ({year}) "
            f"| ⭐ {rating} "
            f"| {qualities}"
        )

        rows.append([
            InlineKeyboardButton(
                label[:64],
                callback_data=f"movie:{movie_id}"
            )
        ])

    if pagination:
        rows.append(pagination)

    rows.append([
        InlineKeyboardButton(
            "🏠 القائمة الرئيسية",
            callback_data="menu:home"
        )
    ])

    await message.reply_text(
        header,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows)
    )


# =========================================================
# START / HELP
# =========================================================

WELCOME_TEXT = (
    "🎬 <b>YTS Movies Pro Ultra 3.0</b>\n\n"
    "أهلاً بك في مكتبة الأفلام الذكية 🍿\n\n"
    "🔎 ابحث عن أي فيلم بالإنجليزية.\n"
    "🎞️ استعرض أحدث الأفلام والأعلى تقييماً.\n"
    "❤️ احفظ أفلامك المفضلة.\n"
    "🔔 تابع الأفلام واستقبل التحديثات.\n"
    "📥 استعرض الجودات المتاحة.\n\n"
    "اختر من القائمة أو اكتب اسم الفيلم."
)


async def start_command(update, context):

    user = update.effective_user

    db.register_user(
        str(user.id),
        user.username or ""
    )

    await update.message.reply_text(
        WELCOME_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard()
    )


async def help_command(update, context):

    text = (
        "ℹ️ <b>طريقة استخدام البوت</b>\n\n"
        "/start - تشغيل البوت\n"
        "/help - المساعدة\n"
        "/favorites - المفضلة\n"
        "/watchlist - قائمة المتابعة\n"
        "/stats - الإحصائيات\n\n"
        "اكتب اسم الفيلم بالإنجليزية للبحث.\n"
        "يمكنك أيضاً البحث باستخدام IMDb ID."
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML
    )


# =========================================================
# SEARCH
# =========================================================

async def text_search(update, context):

    query = (update.message.text or "").strip()

    if not query:
        return

    wait = await update.message.reply_text(
        f"🔎 جاري البحث عن: <b>{esc(query)}</b>...",
        parse_mode=ParseMode.HTML
    )

    try:

        movies = await yts.smart_search(
            query,
            limit=12
        )

        await wait.delete()

        await send_movies_list(
            update.message,
            movies,
            (
                f"🎬 <b>نتائج البحث:</b> {esc(query)}\n"
                f"عدد النتائج: {len(movies)}\n\n"
                "اختر الفيلم لعرض التفاصيل."
            )
        )

    except Exception as exc:

        logger.exception("Search error: %s", exc)

        await wait.edit_text(
            "❌ حدث خطأ أثناء البحث. حاول مرة أخرى."
        )


# =========================================================
# INLINE SEARCH
# =========================================================

async def inline_search(update, context):

    query = (update.inline_query.query or "").strip()

    if len(query) < 2:

        await update.inline_query.answer(
            [],
            cache_time=5
        )

        return

    try:

        movies = await yts.smart_search(
            query,
            limit=10
        )

        results = []

        for movie in movies:

            movie_id = movie.get("id")

            if not movie_id:
                continue

            title = movie_title(movie)
            rating = movie.get("rating", "N/A")

            qualities = " / ".join(sorted({
                t.get("quality", "")
                for t in movie.get("torrents", [])
            }))

            thumb = (
                movie.get("medium_cover_image")
                or movie.get("small_cover_image")
            )

            results.append(
                InlineQueryResultArticle(
                    id=str(movie_id),
                    title=f"🎬 {title}"[:100],
                    description=(
                        f"⭐ {rating}/10 | {qualities}"
                    )[:255],
                    thumbnail_url=thumb,
                    input_message_content=InputTextMessageContent(
                        format_movie(movie),
                        parse_mode=ParseMode.HTML
                    ),
                    reply_markup=movie_keyboard(movie)
                )
            )

        await update.inline_query.answer(
            results,
            cache_time=60
        )

    except Exception:

        logger.exception("Inline search failed")

        await update.inline_query.answer(
            [],
            cache_time=5
        )


# =========================================================
# FAVORITES / WATCHLIST COMMANDS
# =========================================================

async def show_user_list(
    message,
    user_id,
    table,
    heading
):

    items = db.get_items(table, user_id)

    if not items:

        await message.reply_text(
            f"📭 {heading} فارغة حالياً.",
            reply_markup=main_keyboard()
        )

        return

    rows = []

    for item in items[:40]:

        movie_id = item["movie_id"]
        title = item["title"]

        if table == "favorites":
            callback = f"favdel:{movie_id}"
        else:
            callback = f"watchdel:{movie_id}"

        rows.append([
            InlineKeyboardButton(
                f"🎬 {title[:38]}",
                callback_data=f"movie:{movie_id}"
            ),
            InlineKeyboardButton(
                "❌",
                callback_data=callback
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "🏠 الرئيسية",
            callback_data="menu:home"
        )
    ])

    await message.reply_text(
        f"<b>{heading}</b>\n\n"
        f"عدد الأفلام: {len(items)}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows)
    )


async def favorites_command(update, context):

    await show_user_list(
        update.message,
        str(update.effective_user.id),
        "favorites",
        "❤️ قائمة المفضلة"
    )


async def watchlist_command(update, context):

    await show_user_list(
        update.message,
        str(update.effective_user.id),
        "watchlist",
        "🔔 قائمة المتابعة"
    )


async def stats_command(update, context):

    if not ADMIN_CHAT_ID:
        return

    if str(update.effective_user.id) != ADMIN_CHAT_ID:
        return

    await update.message.reply_text(
        f"📊 <b>إحصائيات YTS Ultra</b>\n\n"
        f"👥 المستخدمون: {db.get_user_count()}\n"
        f"💾 قاعدة البيانات: SQLite\n"
        f"🔔 الفحص كل: {CHECK_INTERVAL // 60} دقائق",
        parse_mode=ParseMode.HTML
    )


# =========================================================
# CALLBACK HANDLER
# =========================================================

async def callback_handler(update, context):

    query = update.callback_query

    await query.answer()

    data = query.data or ""
    user_id = str(query.from_user.id)

    db.register_user(
        user_id,
        query.from_user.username or ""
    )

    try:

        # HOME

        if data == "menu:home":

            await query.message.reply_text(
                WELCOME_TEXT,
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard()
            )

        # HELP

        elif data == "menu:help":

            await query.message.reply_text(
                "اكتب اسم الفيلم بالإنجليزية للبحث.\n"
                "استخدم الأزرار لحفظ الأفلام ومتابعتها."
            )

        # GENRES

        elif data == "menu:genres":

            genres = [
                "Action", "Adventure",
                "Animation", "Comedy",
                "Crime", "Documentary",
                "Drama", "Fantasy",
                "Horror", "Mystery",
                "Romance", "Sci-Fi",
                "Thriller", "War"
            ]

            rows = []

            for i in range(0, len(genres), 2):

                row = [
                    InlineKeyboardButton(
                        f"🎭 {genres[i]}",
                        callback_data=f"genre:{genres[i]}:1"
                    )
                ]

                if i + 1 < len(genres):

                    row.append(
                        InlineKeyboardButton(
                            f"🎭 {genres[i+1]}",
                            callback_data=f"genre:{genres[i+1]}:1"
                        )
                    )

                rows.append(row)

            rows.append([
                InlineKeyboardButton(
                    "🏠 الرئيسية",
                    callback_data="menu:home"
                )
            ])

            await query.message.reply_text(
                "🎭 <b>اختر التصنيف:</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(rows)
            )

        # FAVORITES

        elif data == "menu:favorites":

            await show_user_list(
                query.message,
                user_id,
                "favorites",
                "❤️ المفضلة"
            )

        # WATCHLIST

        elif data == "menu:watchlist":

            await show_user_list(
                query.message,
                user_id,
                "watchlist",
                "🔔 قائمة المتابعة"
            )

        # BROWSE

        elif data.startswith("browse:"):

            _, sort_by, page_text = data.split(":")

            page = int(page_text)

            titles = {
                "date_added": "🆕 أحدث الأفلام",
                "rating": "⭐ الأعلى تقييماً",
                "download_count": "🔥 الأكثر تحميلاً"
            }

            movies = await yts.list_movies(
                sort_by=sort_by,
                limit=8,
                page=page
            )

            navigation = []

            if page > 1:

                navigation.append(
                    InlineKeyboardButton(
                        "⬅️ السابق",
                        callback_data=f"browse:{sort_by}:{page-1}"
                    )
                )

            if len(movies) == 8:

                navigation.append(
                    InlineKeyboardButton(
                        "التالي ➡️",
                        callback_data=f"browse:{sort_by}:{page+1}"
                    )
                )

            await send_movies_list(
                query.message,
                movies,
                (
                    f"<b>{titles.get(sort_by, 'الأفلام')}</b>\n"
                    f"الصفحة: {page}"
                ),
                navigation or None
            )

        # GENRE

        elif data.startswith("genre:"):

            _, genre, page_text = data.split(":")

            page = int(page_text)

            movies = await yts.list_movies(
                genre=genre,
                sort_by="rating",
                limit=8,
                page=page
            )

            navigation = []

            if page > 1:

                navigation.append(
                    InlineKeyboardButton(
                        "⬅️ السابق",
                        callback_data=f"genre:{genre}:{page-1}"
                    )
                )

            if len(movies) == 8:

                navigation.append(
                    InlineKeyboardButton(
                        "التالي ➡️",
                        callback_data=f"genre:{genre}:{page+1}"
                    )
                )

            await send_movies_list(
                query.message,
                movies,
                f"🎭 <b>أفلام {esc(genre)}</b> | الصفحة {page}",
                navigation or None
            )

        # MOVIE DETAILS

        elif data.startswith("movie:"):

            movie_id = int(data.split(":")[1])

            movie = await yts.movie_details(movie_id)

            if not movie:

                await query.message.reply_text(
                    "❌ تعذر جلب تفاصيل الفيلم."
                )

                return

            caption = format_movie(movie)
            keyboard = movie_keyboard(movie)

            poster = (
                movie.get("large_cover_image")
                or movie.get("medium_cover_image")
            )

            if poster:

                try:

                    await query.message.reply_photo(
                        photo=poster,
                        caption=caption[:1024],
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard
                    )

                    return

                except Exception as exc:

                    logger.warning(
                        "Poster failed: %s",
                        exc
                    )

            await query.message.reply_text(
                caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard
            )

        # MAGNET

        elif data.startswith("magnet:"):

            _, movie_id_text, index_text = data.split(":")

            movie = await yts.movie_details(
                int(movie_id_text)
            )

            if not movie:
                return

            torrents = movie.get("torrents") or []
            index = int(index_text)

            if index < 0 or index >= len(torrents):
                return

            torrent = torrents[index]

            torrent_hash = torrent.get("hash", "")

            if not re.fullmatch(
                r"[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64}",
                torrent_hash
            ):
                await query.message.reply_text(
                    "❌ رابط Magnet غير صالح."
                )
                return

            title = movie_title(movie)

            magnet = yts.magnet_link(
                torrent_hash,
                title
            )

            short_magnet = yts.short_magnet(
                torrent_hash,
                title
            )

            buttons = [
                [
                    InlineKeyboardButton(
                        "📋 نسخ Magnet",
                        copy_text=CopyTextButton(
                            text=short_magnet[:256]
                        )
                    )
                ]
            ]

            await query.message.reply_text(
                f"🧲 <b>Magnet Link</b>\n\n"
                f"🎬 {esc(title)}\n"
                f"📺 الجودة: {esc(torrent.get('quality'))}\n"
                f"💾 الحجم: {esc(torrent.get('size'))}\n\n"
                f"<code>{esc(magnet)}</code>\n\n"
                "استخدم زر النسخ لنسخ الرابط المختصر.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(buttons)
            )

        # FAVORITE ADD

        elif data.startswith("favorite:"):

            movie_id = int(data.split(":")[1])

            movie = await yts.movie_details(movie_id)

            if not movie:
                return

            added = db.add_item(
                "favorites",
                user_id,
                movie_id,
                movie_title(movie)
            )

            await query.message.reply_text(
                "❤️ تمت إضافة الفيلم للمفضلة."
                if added
                else "ℹ️ الفيلم موجود بالفعل في المفضلة."
            )

        # WATCH ADD

        elif data.startswith("watch:"):

            movie_id = int(data.split(":")[1])

            movie = await yts.movie_details(movie_id)

            if not movie:
                return

            added = db.add_item(
                "watchlist",
                user_id,
                movie_id,
                movie_title(movie)
            )

            await query.message.reply_text(
                "🔔 تمت إضافة الفيلم لقائمة المتابعة."
                if added
                else "ℹ️ الفيلم موجود بالفعل في قائمة المتابعة."
            )

        # FAVORITE DELETE

        elif data.startswith("favdel:"):

            movie_id = int(data.split(":")[1])

            db.remove_item(
                "favorites",
                user_id,
                movie_id
            )

            await query.message.reply_text(
                "🗑️ تم حذف الفيلم من المفضلة."
            )

        # WATCH DELETE

        elif data.startswith("watchdel:"):

            movie_id = int(data.split(":")[1])

            db.remove_item(
                "watchlist",
                user_id,
                movie_id
            )

            await query.message.reply_text(
                "🗑️ تم حذف الفيلم من قائمة المتابعة."
            )

        # SIMILAR MOVIES

        elif data.startswith("similar:"):

            movie_id = int(data.split(":")[1])

            movies = await yts.suggestions(movie_id)

            await send_movies_list(
                query.message,
                movies,
                "🎯 <b>أفلام مشابهة:</b>"
            )

    except Exception as exc:

        logger.exception(
            "Callback error: %s",
            exc
        )

        await query.message.reply_text(
            "❌ حصل خطأ أثناء تنفيذ الطلب. حاول مرة أخرى."
        )


# =========================================================
# AUTOMATIC NOTIFICATIONS
# =========================================================

async def check_movie_updates(bot: Bot):

    logger.info("Checking YTS movie updates...")

    # Admin watched titles

    admin_targets = [
        item.strip()
        for item in WATCHED_MOVIES_ENV.split(",")
        if item.strip()
    ]

    targets = []

    if ADMIN_CHAT_ID:

        for title in admin_targets:

            targets.append({
                "user_id": ADMIN_CHAT_ID,
                "movie_id": None,
                "title": title,
                "admin": True
            })

    # User watchlists

    targets.extend(
        db.get_all_watchlists()
    )

    checked = set()

    for target in targets:

        user_id = str(target["user_id"])
        title = target["title"]

        movies = await yts.list_movies(
            query_term=title,
            limit=5
        )

        for movie in movies:

            movie_id = movie.get("id")

            if not movie_id:
                continue

            # Reduce false matches by comparing normalized titles.

            expected = re.sub(
                r"[^a-z0-9]+",
                "",
                title.lower()
            )

            actual = re.sub(
                r"[^a-z0-9]+",
                "",
                movie.get("title", "").lower()
            )

            if expected and expected != actual:
                continue

            signature = quality_signature(movie)

            if not signature:
                continue

            # Avoid duplicate notifications during the same check.

            key = (user_id, movie_id)

            if key in checked:
                continue

            checked.add(key)

            previous = db.get_notification(
                user_id,
                movie_id
            )

            if previous == signature:
                continue

            new_qualities = [
                quality for quality in signature.split(",")
                if quality and quality not in previous.split(",")
            ]

            if previous:
                if not new_qualities:
                    db.set_notification(
                        user_id,
                        movie_id,
                        signature
                    )
                    continue

                alert_title = "🆕 جودة جديدة متاحة!"
            else:
                alert_title = "🔔 تم العثور على فيلم تتابعه!"

            try:

                caption = (
                    f"<b>{alert_title}</b>\n\n"
                    f"{format_movie(movie)}"
                )

                await bot.send_message(
                    chat_id=int(user_id),
                    text=caption[:4000],
                    parse_mode=ParseMode.HTML,
                    reply_markup=movie_keyboard(movie)
                )

                db.set_notification(
                    user_id,
                    movie_id,
                    signature
                )

                logger.info(
                    "Notification sent to %s for %s",
                    user_id,
                    movie.get("title")
                )

            except asyncio.CancelledError:
                raise

            except Exception as exc:

                logger.warning(
                    "Notification failed for %s: %s",
                    user_id,
                    exc
                )

        await asyncio.sleep(0.3)

    logger.info("Notification check completed.")


async def scheduled_notifications(context):

    try:

        await check_movie_updates(context.bot)

    except asyncio.CancelledError:
        raise

    except Exception:

        logger.exception(
            "Scheduled notification error"
        )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update, context):

    logger.error(
        "Telegram update error",
        exc_info=context.error
    )


# =========================================================
# APPLICATION
# =========================================================

def build_application():

    if not BOT_TOKEN:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN is missing!"
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("favorites", favorites_command)
    )

    application.add_handler(
        CommandHandler("watchlist", watchlist_command)
    )

    application.add_handler(
        CommandHandler("stats", stats_command)
    )

    application.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    application.add_handler(
        InlineQueryHandler(inline_search)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_search
        )
    )

    application.add_error_handler(
        error_handler
    )

    application.job_queue.run_repeating(
        scheduled_notifications,
        interval=CHECK_INTERVAL,
        first=20,
        name="yts_notifications"
    )

    return application


# =========================================================
# GITHUB ACTIONS RUNNER
# =========================================================

async def run_github():

    application = build_application()

    logger.info(
        "Starting YTS Movies Pro Ultra on GitHub Actions"
    )

    await application.initialize()
    await application.start()

    await application.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False
    )

    start_time = time.monotonic()

    try:

        while (
            time.monotonic() - start_time
        ) < SESSION_SECONDS:

            await asyncio.sleep(20)

    finally:

        logger.info(
            "Stopping GitHub Actions session..."
        )

        await application.updater.stop()
        await application.stop()
        await application.shutdown()


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:

        print(
            "ERROR: Set TELEGRAM_BOT_TOKEN first."
        )

        return

    if IS_GITHUB_ACTIONS:

        asyncio.run(run_github())

    else:

        application = build_application()

        logger.info(
            "YTS Movies Pro Ultra is running..."
        )

        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False
        )


if __name__ == "__main__":
    main()
