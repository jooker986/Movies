
YTS Movies Pro Telegram Bot (yts.gg/api/v2)
===========================================
A high-performance, asynchronous Telegram bot powered by the YTS API (v2)
with fallback mirror support, rich inline keyboards, Magnet & .torrent links,
poster previews, IMDb ratings, and automated new-release alerts.

Features:
1. Instant Movie Search: Type any movie name (or IMDb ID like tt0133093) to get results.
2. Full Quality & Codec Breakdown: 720p, 1080p, 2160p (4K), 3D, BluRay/WEB, file sizes, seeds/peers.
3. One-Tap Magnet & Direct .torrent Links: Generates valid magnet URIs with top trackers + direct torrent download buttons.
4. Browse Categories: Latest Movies, Top Rated (IMDb), Most Downloaded, and Genre Filters.
5. Watchlist & Auto-Alerts: Save upcoming/wanted movies and get notified automatically when a high-quality rip drops.
6. GitHub Actions Ready: Supports both 24/7 interactive polling mode and stateless GitHub Actions cron checks.

Installation:
    pip install -r requirements.txt
"""

import os
import json
import asyncio
import logging
from typing import List, Dict, Optional, Any
from urllib.parse import quote

import aiohttp
from telegram import (
    Bot,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==============================================================================
# CONFIGURATION
# ==============================================================================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
ADMIN_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WATCHED_MOVIES_ENV = os.getenv("WATCHED_MOVIES", "")  # Comma-separated movie titles/IMDb IDs
RUN_ONCE_MODE = os.getenv("GITHUB_ACTIONS", "false").lower() == "true"

# Primary YTS API endpoint with automatic failover mirrors
YTS_API_MIRRORS = [
    "https://yts.gg/api/v2",
    "https://yts.mx/api/v2",
    "https://yts.lt/api/v2",
]

# Standard high-speed BitTorrent trackers for Magnet URI generation
TRACKERS = [
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:80",
    "udp://tracker.coppersurfer.tk:6969",
    "udp://glotorrents.pw:6969/announce",
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://torrent.gresille.org:80/announce",
    "udp://p4p.arenabg.com:1337",
    "udp://tracker.leechers-paradise.org:6969",
]

STATE_FILE = "yts_notified_state.json"
WATCHLIST_FILE = "yts_user_watchlists.json"

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("YTSProBot")


# ==============================================================================
# YTS API ASYNC CLIENT (WITH AUTOMATIC MIRROR FAILOVER)
# ==============================================================================
class YTSClient:
    """Asynchronous client for YTS API v2 with automatic mirror fallback."""

    def __init__(self, mirrors: List[str]):
        self.mirrors = mirrors
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) YTSProBot/2.0",
            "Accept": "application/json",
        }

    async def _request(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """Tries each YTS mirror in order until a valid JSON response is received."""
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(headers=self.headers, timeout=timeout) as session:
            for base_url in self.mirrors:
                url = f"{base_url}/{endpoint.lstrip('/')}"
                try:
                    async with session.get(url, params=params) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            if data and data.get("status") == "ok":
                                return data.get("data", {})
                except Exception as exc:
                    logger.warning(f"Mirror failed ({url}): {exc}")
                    continue
        return None

    async def list_movies(
        self,
        query_term: str = "",
        sort_by: str = "date_added",
        order_by: str = "desc",
        genre: str = "",
        minimum_rating: int = 0,
        limit: int = 10,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        """Searches or lists movies from YTS API."""
        params: Dict[str, Any] = {
            "limit": limit,
            "page": page,
            "sort_by": sort_by,
            "order_by": order_by,
        }
        if query_term:
            params["query_term"] = query_term
        if genre and genre.lower() != "all":
            params["genre"] = genre
        if minimum_rating > 0:
            params["minimum_rating"] = minimum_rating

        data = await self._request("list_movies.json", params=params)
        if not data:
            return []
        return data.get("movies", []) or []

    async def get_movie_details(self, movie_id: int) -> Optional[Dict[str, Any]]:
        """Fetches full metadata, cast, and torrent qualities for a specific movie ID."""
        params = {
            "movie_id": movie_id,
            "with_images": "true",
            "with_cast": "true",
        }
        data = await self._request("movie_details.json", params=params)
        if not data:
            return None
        return data.get("movie")

    async def get_suggestions(self, movie_id: int) -> List[Dict[str, Any]]:
        """Fetches 4 similar movie recommendations."""
        data = await self._request("movie_suggestions.json", params={"movie_id": movie_id})
        if not data:
            return []
        return data.get("movies", []) or []

    @staticmethod
    def build_magnet_link(torrent_hash: str, movie_title: str) -> str:
        """Constructs a standard BitTorrent magnet URI with trackers."""
        encoded_name = quote(movie_title)
        tracker_params = "".join(f"&tr={quote(tr)}" for tr in TRACKERS)
        return f"magnet:?xt=urn:btih:{torrent_hash}&dn={encoded_name}{tracker_params}"


yts = YTSClient(YTS_API_MIRRORS)


# ==============================================================================
# LOCAL PERSISTENCE HELPERS (WATCHLIST & NOTIFICATION STATE)
# ==============================================================================
def load_json_file(path: str, default: Any) -> Any:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json_file(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ==============================================================================
# UI FORMATTERS & KEYBOARD BUILDERS
# ==============================================================================
def format_movie_card(movie: Dict[str, Any]) -> str:
    """Formats rich movie details in clean Markdown."""
    title = movie.get("title_long") or f"{movie.get('title')} ({movie.get('year')})"
    rating = movie.get("rating", "N/A")
    runtime = movie.get("runtime", 0)
    genres = ", ".join(movie.get("genres", []) or ["General"])
    language = (movie.get("language") or "en").upper()
    imdb_code = movie.get("imdb_code", "")
    synopsis = (
        movie.get("description_full")
        or movie.get("summary")
        or "لا يوجد وصف متاح لهذا الفيلم حالياً."
    )
    if len(synopsis) > 420:
        synopsis = synopsis[:417] + "..."

    torrents = movie.get("torrents", []) or []
    torrents_lines = []
    for t in torrents:
        q = t.get("quality", "?")
        t_type = t.get("type", "").capitalize()
        size = t.get("size", "?")
        seeds = t.get("seeds", 0)
        peers = t.get("peers", 0)
        torrents_lines.append(f"• `{q}` ({t_type}) — **{size}** | 🟢 {seeds} / 🔵 {peers}")

    torrents_block = "\n".join(torrents_lines) if torrents_lines else "لا توجد نسخ تورنت متاحة حالياً."

    return (
        f"🎬 **{title}**\n"
        f"⭐ **IMDb:** `{rating}/10`  |  ⏱️ `{runtime} دقيقة`  |  🌐 `{language}`\n"
        f"🎭 **التصنيف:** {genres}\n"
        f"🔗 **IMDb ID:** `{imdb_code}`\n\n"
        f"📝 **القصة:**\n_{synopsis}_\n\n"
        f"📥 **الجودات المتاحة للتحميل:**\n{torrents_block}"
    )


def build_movie_keyboard(movie: Dict[str, Any]) -> InlineKeyboardMarkup:
    """Builds interactive download, magnet, trailer, and similar-movies buttons."""
    movie_id = movie.get("id")
    title = movie.get("title_long") or movie.get("title", "Movie")
    torrents = movie.get("torrents", []) or []
    rows: List[List[InlineKeyboardButton]] = []

    # Direct .torrent download buttons + Magnet trigger buttons per quality
    for idx, t in enumerate(torrents):
        q = t.get("quality", "HD")
        t_type = t.get("type", "").upper()
        size = t.get("size", "")
        torrent_url = t.get("url")
        row = []
        if torrent_url:
            row.append(
                InlineKeyboardButton(
                    text=f"⬇️ .torrent {q} {t_type} ({size})",
                    url=torrent_url,
                )
            )
        row.append(
            InlineKeyboardButton(
                text=f"🧲 Magnet {q}",
                callback_data=f"magnet:{movie_id}:{idx}",
            )
        )
        rows.append(row)

    # Extra action row: YouTube Trailer, Similar Movies, Watchlist
    extra_row = []
    yt_code = movie.get("yt_trailer_code")
    if yt_code:
        extra_row.append(
            InlineKeyboardButton("▶️ التريلر", url=f"https://www.youtube.com/watch?v={yt_code}")
        )
    extra_row.append(
        InlineKeyboardButton("🎯 أفلام مشابهة", callback_data=f"similar:{movie_id}")
    )
    rows.append(extra_row)

    rows.append([
        InlineKeyboardButton("🔔 أضف لقائمة المتابعة", callback_data=f"watch:{title[:35]}"),
        InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu:home"),
    ])

    return InlineKeyboardMarkup(rows)


def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Builds the main navigation keyboard."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🆕 أحدث الأفلام", callback_data="browse:date_added:1"),
            InlineKeyboardButton("⭐ الأعلى تقييماً", callback_data="browse:rating:1"),
        ],
        [
            InlineKeyboardButton("🔥 الأكثر تحميلاً", callback_data="browse:download_count:1"),
            InlineKeyboardButton("🎭 تصفح بالتصنيف", callback_data="menu:genres"),
        ],
        [
            InlineKeyboardButton("🔔 قائمة متابعتي (Alerts)", callback_data="menu:watchlist"),
        ],
    ])


# ==============================================================================
# TELEGRAM BOT HANDLERS
# ==============================================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    welcome_text = (
        "🎬 **أهلاً بك في بوت YTS Movies Pro!** 🍿\n\n"
        "البوت متصل مباشرة بواجهة **YTS API** لجلب أحدث الأفلام بأعلى جودة وأقل حجم.\n\n"
        "🔎 **ابحث فوراً:** ابعت اسم أي فيلم (بالإنجليزي) أو كود IMDb مثل `tt0133093`.\n"
        "⚡ **روابط مباشرة:** يدعم ملفات `.torrent` المباشرة وروابط `Magnet` لكل الجودات (720p / 1080p / 4K).\n"
        "🔔 **تنبيهات تلقائية:** ضيف أي فيلم منتظره في قائمة المتابعة وهنبعتلك إشعار أول ما ينزل!"
    )
    await update.message.reply_text(
        welcome_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_main_menu_keyboard(),
    )


async def send_movies_list(
    target_message,
    movies: List[Dict[str, Any]],
    header: str,
    back_callback: str = "menu:home",
    pagination_row: Optional[List[InlineKeyboardButton]] = None,
) -> None:
    """Renders a clean list of movies as clickable inline buttons."""
    if not movies:
        await target_message.reply_text(
            "❌ لم يتم العثور على نتائج مطابقة. جرب كتابة اسم الفيلم بالإنجليزية بشكل صحيح.",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    buttons: List[List[InlineKeyboardButton]] = []
    for m in movies:
        m_id = m.get("id")
        title = m.get("title", "Unknown")
        year = m.get("year", "")
        rating = m.get("rating", "-")
        qualities = "/".join(sorted({t.get("quality", "") for t in (m.get("torrents") or [])}))
        label = f"🎬 {title} ({year}) | ⭐{rating} [{qualities}]"
        buttons.append([InlineKeyboardButton(label[:64], callback_data=f"movie:{m_id}")])

    if pagination_row:
        buttons.append(pagination_row)
    buttons.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data=back_callback)])

    await target_message.reply_text(
        header,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def handle_text_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles any text sent by the user as a live search query on YTS."""
    query = (update.message.text or "").strip()
    if not query:
        return

    wait_msg = await update.message.reply_text(f"🔎 جاري البحث في YTS عن: **{query}** ...", parse_mode=ParseMode.MARKDOWN)
    movies = await yts.list_movies(query_term=query, sort_by="download_count", limit=10)
    await wait_msg.delete()

    await send_movies_list(
        update.message,
        movies,
        header=f"🎬 **نتائج البحث عن:** `{query}`\nاختر الفيلم لعرض التفاصيل وروابط التحميل:",
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Routes all inline button callbacks."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user_id = str(query.from_user.id)

    if data == "menu:home":
        await query.message.reply_text(
            "🏠 **القائمة الرئيسية — YTS Movies Pro**\nاختر القسم أو اكتب اسم أي فيلم للبحث:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_main_menu_keyboard(),
        )

    elif data == "menu:genres":
        genres = ["Action", "Adventure", "Animation", "Comedy", "Crime", "Drama", "Horror", "Sci-Fi", "Thriller", "Mystery"]
        rows = []
        for i in range(0, len(genres), 2):
            row = [InlineKeyboardButton(f"🎭 {genres[i]}", callback_data=f"genre:{genres[i]}:1")]
            if i + 1 < len(genres):
                row.append(InlineKeyboardButton(f"🎭 {genres[i+1]}", callback_data=f"genre:{genres[i+1]}:1"))
            rows.append(row)
        rows.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu:home")])
        await query.message.reply_text(
            "🎭 **اختر تصنيف الأفلام المفضل لديك:**",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(rows),
        )

    elif data.startswith("browse:"):
        _, sort_by, page_str = data.split(":")
        page = int(page_str)
        titles_map = {
            "date_added": "🆕 أحدث الأفلام المضافة",
            "rating": "⭐ الأفلام الأعلى تقييماً (IMDb)",
            "download_count": "🔥 الأفلام الأكثر تحميلاً",
        }
        movies = await yts.list_movies(sort_by=sort_by, limit=8, page=page)
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"browse:{sort_by}:{page-1}"))
        if len(movies) == 8:
            nav_row.append(InlineKeyboardButton("التالي ➡️", callback_data=f"browse:{sort_by}:{page+1}"))
        await send_movies_list(
            query.message,
            movies,
            header=f"**{titles_map.get(sort_by, 'قائمة الأفلام')}** (صفحة {page}):",
            pagination_row=nav_row or None,
        )

    elif data.startswith("genre:"):
        _, genre, page_str = data.split(":")
        page = int(page_str)
        movies = await yts.list_movies(genre=genre, sort_by="rating", limit=8, page=page)
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"genre:{genre}:{page-1}"))
        if len(movies) == 8:
            nav_row.append(InlineKeyboardButton("التالي ➡️", callback_data=f"genre:{genre}:{page+1}"))
        await send_movies_list(
            query.message,
            movies,
            header=f"🎭 **أفضل أفلام {genre}** (صفحة {page}):",
            pagination_row=nav_row or None,
        )

    elif data.startswith("movie:"):
        movie_id = int(data.split(":")[1])
        movie = await yts.get_movie_details(movie_id)
        if not movie:
            await query.message.reply_text("❌ تعذر جلب تفاصيل الفيلم حالياً، حاول مرة أخرى.")
            return

        caption = format_movie_card(movie)
        keyboard = build_movie_keyboard(movie)
        poster_url = movie.get("large_cover_image") or movie.get("medium_cover_image")

        if poster_url:
            try:
                await query.message.reply_photo(
                    photo=poster_url,
                    caption=caption[:1024],
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=keyboard,
                )
                return
            except Exception:
                pass

        await query.message.reply_text(
            caption,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )

    elif data.startswith("magnet:"):
        _, movie_id_str, idx_str = data.split(":")
        movie = await yts.get_movie_details(int(movie_id_str))
        if not movie:
            return
        torrents = movie.get("torrents", []) or []
        idx = int(idx_str)
        if 0 <= idx < len(torrents):
            t = torrents[idx]
            magnet_uri = yts.build_magnet_link(
                t.get("hash", ""),
                f"{movie.get('title_long')} [{t.get('quality')}] [YTS]",
            )
            await query.message.reply_text(
                f"🧲 **رابط Magnet المباشر ({t.get('quality')} - {t.get('size')}):**\n"
                f"اضغط مطولاً لنسخ الرابط أو افتحه في تطبيق التورنت:\n\n`{magnet_uri}`",
                parse_mode=ParseMode.MARKDOWN,
            )

    elif data.startswith("similar:"):
        movie_id = int(data.split(":")[1])
        suggestions = await yts.get_suggestions(movie_id)
        await send_movies_list(
            query.message,
            suggestions,
            header="🎯 **أفلام مشابهة مقترحة لك:**",
        )

    elif data.startswith("watch:"):
        movie_title = data.split(":", 1)[1].strip()
        watchlists = load_json_file(WATCHLIST_FILE, {})
        user_list: List[str] = watchlists.get(user_id, [])
        if movie_title not in user_list:
            user_list.append(movie_title)
            watchlists[user_id] = user_list
            save_json_file(WATCHLIST_FILE, watchlists)
            await query.message.reply_text(
                f"✅ تم إضافة **{movie_title}** إلى قائمة المتابعة!\n"
                "سيصلك تنبيه فوري عند توفر جودات جديدة أو نزول الفيلم.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await query.message.reply_text(f"ℹ️ فيلم **{movie_title}** موجود بالفعل في قائمة متابعتك.", parse_mode=ParseMode.MARKDOWN)

    elif data == "menu:watchlist":
        watchlists = load_json_file(WATCHLIST_FILE, {})
        user_list: List[str] = watchlists.get(user_id, [])
        if not user_list:
            await query.message.reply_text(
                "📭 قائمة المتابعة فارغة حالياً.\nابحث عن أي فيلم واضغط **🔔 أضف لقائمة المتابعة**.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_main_menu_keyboard(),
            )
            return

        buttons = [
            [InlineKeyboardButton(f"❌ حذف: {title}", callback_data=f"unwatch:{idx}")]
            for idx, title in enumerate(user_list)
        ]
        buttons.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu:home")])
        items_text = "\n".join(f"• **{t}**" for t in user_list)
        await query.message.reply_text(
            f"🔔 **قائمة الأفلام التي تتابعها:**\n\n{items_text}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("unwatch:"):
        idx = int(data.split(":")[1])
        watchlists = load_json_file(WATCHLIST_FILE, {})
        user_list: List[str] = watchlists.get(user_id, [])
        if 0 <= idx < len(user_list):
            removed = user_list.pop(idx)
            watchlists[user_id] = user_list
            save_json_file(WATCHLIST_FILE, watchlists)
            await query.message.reply_text(f"🗑️ تم حذف **{removed}** من قائمة المتابعة.", parse_mode=ParseMode.MARKDOWN)


# ==============================================================================
# BACKGROUND MONITOR (INTERACTIVE JOBQUEUE + GITHUB ACTIONS CRON)
# ==============================================================================
async def check_new_releases_and_notify(bot: Bot) -> None:
    """Checks YTS for watched movies or top new releases and sends Telegram alerts."""
    state: Dict[str, bool] = load_json_file(STATE_FILE, {})
    state_changed = False

    # 1. Check GitHub Secrets watched list (if configured)
    env_targets = [t.strip() for t in WATCHED_MOVIES_ENV.split(",") if t.strip()]
    if ADMIN_CHAT_ID and env_targets:
        for target in env_targets:
            movies = await yts.list_movies(query_term=target, limit=3)
            for m in movies:
                qualities = [t.get("quality", "") for t in (m.get("torrents") or [])]
                key = f"admin::{m.get('id')}::{','.join(sorted(qualities))}"
                if not state.get(key):
                    caption = "🚨 **تنبيه نزول فيلم جديد على YTS!** 🚨\n\n" + format_movie_card(m)
                    await bot.send_message(
                        chat_id=int(ADMIN_CHAT_ID),
                        text=caption,
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=build_movie_keyboard(m),
                    )
                    state[key] = True
                    state_changed = True

    # 2. Check interactive users' watchlists
    watchlists: Dict[str, List[str]] = load_json_file(WATCHLIST_FILE, {})
    for uid, titles in watchlists.items():
        for title in titles:
            movies = await yts.list_movies(query_term=title, limit=2)
            for m in movies:
                qualities = [t.get("quality", "") for t in (m.get("torrents") or [])]
                key = f"{uid}::{m.get('id')}::{','.join(sorted(qualities))}"
                if not state.get(key):
                    caption = f"🔔 **تحديث جديد لفيلم في قائمة متابعتك:**\n\n" + format_movie_card(m)
                    try:
                        await bot.send_message(
                            chat_id=int(uid),
                            text=caption,
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=build_movie_keyboard(m),
                        )
                        state[key] = True
                        state_changed = True
                    except Exception as exc:
                        logger.warning(f"Failed to notify user {uid}: {exc}")

    if state_changed:
        save_json_file(STATE_FILE, state)


async def scheduled_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await check_new_releases_and_notify(context.bot)


def main() -> None:
    if RUN_ONCE_MODE:
        if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
            logger.error("Missing TELEGRAM_BOT_TOKEN!")
            return
        asyncio.run(check_new_releases_and_notify(Bot(token=BOT_TOKEN)))
        return

    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("❌ ERROR: Please set TELEGRAM_BOT_TOKEN before running.")
        return

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_search))

    # Run background check every 10 minutes
    application.job_queue.run_repeating(scheduled_job, interval=600, first=20)

    logger.info("YTS Movies Pro Telegram Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
