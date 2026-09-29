# -*- coding: utf-8 -*-

import asyncio
import base64
import hashlib
import inspect
import logging
import os
import secrets
import sqlite3
from io import BytesIO
from urllib.parse import quote

import aiohttp
from PIL import Image

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

   token = os.environ.get("TOKEN")

).strip()

OWNER_ID = int(os.getenv("OWNER_ID", "7860500580"))

DB_FILE = os.getenv("DB_FILE", "bot.db")

IMAGE_MODEL = os.getenv("IMAGE_MODEL", "flux")

# طبق سرویس اصلی که از سورس استخراج شد
IMAGE_WIDTH = int(os.getenv("IMAGE_WIDTH", "768"))
IMAGE_HEIGHT = int(os.getenv("IMAGE_HEIGHT", "768"))

MAX_PROMPT = int(os.getenv("MAX_PROMPT", "1500"))

DEFAULT_LIMIT = int(os.getenv("DEFAULT_LIMIT", "5"))

# اگر API Key جدید Pollinations داری می‌توانی در محیط قرار بدهی.
# اجباری نیست برای endpoint قدیمی.
POLLINATIONS_API_KEY = os.getenv(
    "POLLINATIONS_API_KEY",
    ""
).strip()


if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    raise RuntimeError(
        "BOT_TOKEN را داخل کد یا متغیر محیطی BOT_TOKEN قرار بده."
    )


# ============================================================
# POLLINATIONS ENDPOINTS
# ============================================================

# Endpoint موجود در سورس اصلی
LEGACY_IMAGE_API = "https://image.pollinations.ai/prompt"

# Endpoint جدید Pollinations
NEW_IMAGE_API = "https://gen.pollinations.ai/image"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("ai-image-bot")


# ============================================================
# DATABASE
# ============================================================

class DB:

    def __init__(self, path):
        self.path = path

        with self.c() as c:

            c.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS users(
                    user_id INTEGER PRIMARY KEY,
                    username TEXT DEFAULT '',
                    first_name TEXT DEFAULT '',
                    used INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS admins(
                    user_id INTEGER PRIMARY KEY
                );

                CREATE TABLE IF NOT EXISTS channels(
                    username TEXT PRIMARY KEY
                );

                CREATE TABLE IF NOT EXISTS groups(
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT DEFAULT '',
                    username TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS settings(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

            c.execute(
                """
                INSERT OR IGNORE INTO settings(key,value)
                VALUES('limit',?)
                """,
                (str(DEFAULT_LIMIT),)
            )

    def c(self):
        connection = sqlite3.connect(
            self.path,
            timeout=30
        )

        connection.row_factory = sqlite3.Row

        return connection

    def run(self, query, args=()):
        with self.c() as c:
            c.execute(query, args)

    def rows(self, query, args=()):
        with self.c() as c:
            return c.execute(
                query,
                args
            ).fetchall()

    def add_user(self, user):

        self.run(
            """
            INSERT INTO users(
                user_id,
                username,
                first_name
            )
            VALUES(?,?,?)

            ON CONFLICT(user_id)
            DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
            """,
            (
                user.id,
                user.username or "",
                user.first_name or ""
            )
        )

    def add_group(self, chat):

        self.run(
            """
            INSERT INTO groups(
                chat_id,
                title,
                username
            )
            VALUES(?,?,?)

            ON CONFLICT(chat_id)
            DO UPDATE SET
                title=excluded.title,
                username=excluded.username
            """,
            (
                chat.id,
                chat.title or "",
                chat.username or ""
            )
        )

    def users(self):
        return [
            r["user_id"]
            for r in self.rows(
                "SELECT user_id FROM users"
            )
        ]

    def groups(self):
        return [
            r["chat_id"]
            for r in self.rows(
                "SELECT chat_id FROM groups"
            )
        ]

    def usage(self, user_id):

        rows = self.rows(
            """
            SELECT used
            FROM users
            WHERE user_id=?
            """,
            (user_id,)
        )

        if not rows:
            return 0

        return int(rows[0]["used"])

    def inc(self, user_id):

        self.run(
            """
            UPDATE users
            SET used=used+1
            WHERE user_id=?
            """,
            (user_id,)
        )

    def admins(self):

        return {
            r["user_id"]
            for r in self.rows(
                "SELECT user_id FROM admins"
            )
        }

    def admin(self, user_id):

        return (
            user_id == OWNER_ID
            or user_id in self.admins()
        )

    def add_admin(self, user_id):

        self.run(
            """
            INSERT OR IGNORE INTO admins(user_id)
            VALUES(?)
            """,
            (user_id,)
        )

    def del_admin(self, user_id):

        self.run(
            """
            DELETE FROM admins
            WHERE user_id=?
            """,
            (user_id,)
        )

    def channels(self):

        return [
            r["username"]
            for r in self.rows(
                "SELECT username FROM channels"
            )
        ]

    def add_channel(self, username):

        self.run(
            """
            INSERT OR IGNORE INTO channels(username)
            VALUES(?)
            """,
            (username,)
        )

    def del_channel(self, username):

        self.run(
            """
            DELETE FROM channels
            WHERE username=?
            """,
            (username,)
        )

    def setting(self, key, default=None):

        rows = self.rows(
            """
            SELECT value
            FROM settings
            WHERE key=?
            """,
            (key,)
        )

        if not rows:
            return default

        return rows[0]["value"]

    def set_setting(self, key, value):

        self.run(
            """
            INSERT INTO settings(key,value)
            VALUES(?,?)

            ON CONFLICT(key)
            DO UPDATE SET
                value=excluded.value
            """,
            (
                key,
                str(value)
            )
        )


db = DB(DB_FILE)


# ============================================================
# USER STATES
# ============================================================

STATE_ADD_CHANNEL = "add_channel"
STATE_DELETE_CHANNEL = "delete_channel"
STATE_ADD_ADMIN = "add_admin"
STATE_DELETE_ADMIN = "delete_admin"
STATE_BROADCAST = "broadcast"
STATE_ADVERTISING = "advertising"
STATE_LIMIT = "limit"


# ============================================================
# BUTTONS
# ============================================================

# Telegram Bot API 9.6+ supports native button styles:
# primary = blue, success = green, danger = red.
# python-telegram-bot 22.7+ exposes this as InlineKeyboardButton(style=...).
# Compatibility fallback prevents crashes on older PTB versions.
_PT_BUTTON_STYLE_SUPPORTED = (
    "style" in inspect.signature(InlineKeyboardButton).parameters
)


def B(text, callback_data=None, url=None, style="primary"):
    """Create a Telegram inline button with native color styling."""
    kwargs = {}

    if url is not None:
        kwargs["url"] = url
    else:
        kwargs["callback_data"] = callback_data

    if _PT_BUTTON_STYLE_SUPPORTED and style in (
        "primary", "success", "danger"
    ):
        kwargs["style"] = style

    return InlineKeyboardButton(text=text, **kwargs)


def user_kb():
    return InlineKeyboardMarkup(
        [
            [
                B("🎨 ساخت تصویر", "make", style="primary"),
                B("📊 سهمیه", "quota", style="success"),
            ],
            [
                B("ℹ️ راهنما", "help", style="primary"),
            ],
        ]
    )


def admin_kb():
    return InlineKeyboardMarkup(
        [
            [
                B("➕ عضویت اجباری", "a:addch", style="success"),
                B("➖ حذف عضویت", "a:delch", style="danger"),
            ],
            [
                B("👤 افزودن ادمین", "a:addad", style="success"),
                B("🗑 حذف ادمین", "a:delad", style="danger"),
            ],
            [
                B("📨 پیام همگانی", "a:broadcast", style="primary"),
            ],
            [
                B("📢 تبلیغات", "a:advertising", style="primary"),
            ],
            [
                B("📊 آمار", "a:stats", style="primary"),
                B("🔢 محدودیت", "a:limit", style="success"),
            ],
        ]
    )


# ============================================================
# MEMBERSHIP
# ============================================================

async def subscribed(bot, user_id):

    channels = db.channels()

    if not channels:
        return True

    for channel in channels:

        try:

            member = await bot.get_chat_member(
                chat_id=channel,
                user_id=user_id
            )

            if member.status in (
                "left",
                "kicked"
            ):
                return False

        except Exception as e:

            log.error(
                "Membership check failed for %s: %s",
                channel,
                e
            )

            return False

    return True


async def gate(update, context):

    message = update.effective_message

    if not message:
        return False

    if message.chat.type != ChatType.PRIVATE:
        return True

    user_id = update.effective_user.id

    if await subscribed(
        context.bot,
        user_id
    ):
        return True

    rows = []

    for channel in db.channels():

        username = channel.lstrip("@")

        rows.append(
            [
                B(
                    "📢 " + channel,
                    url=f"https://t.me/{username}",
                    style="primary"
                )
            ]
        )

    rows.append(
        [
            B(
                "✅ بررسی عضویت",
                "check",
                style="success"
            )
        ]
    )

    await message.reply_text(
        "🔒 ابتدا در کانال‌های زیر عضو شوید:",
        reply_markup=InlineKeyboardMarkup(rows)
    )

    return False


# ============================================================
# PROMPT
# ============================================================

def prompt_of(text):

    prefixes = (
        "عکس ",
        "عکس:",
        "/عکس ",
        "/photo ",
        "/image "
    )

    stripped = text.strip()

    for prefix in prefixes:

        if stripped.lower().startswith(
            prefix.lower()
        ):

            return stripped[
                len(prefix):
            ].strip()

    return None


# ============================================================
# POLLINATIONS REQUEST
# ============================================================

async def request_image(
    session,
    endpoint,
    prompt,
    use_auth=False
):

    encoded_prompt = quote(
        prompt,
        safe=""
    )

    url = (
        f"{endpoint}/"
        f"{encoded_prompt}"
    )

    params = {
        "width": IMAGE_WIDTH,
        "height": IMAGE_HEIGHT,
        "seed": secrets.randbelow(2_000_000),
        "nologo": "true",
        "model": IMAGE_MODEL,
    }

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Linux; Android 12) "
            "AI-Image-Bot/2.0"
        ),
        "Accept": (
            "image/png,image/jpeg,"
            "image/webp,image/*,*/*"
        ),
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    if use_auth and POLLINATIONS_API_KEY:

        headers["Authorization"] = (
            f"Bearer {POLLINATIONS_API_KEY}"
        )

    log.info(
        "Requesting image from %s",
        endpoint
    )

    async with session.get(
        url,
        params=params,
        headers=headers
    ) as response:

        status = response.status

        content_type = (
            response.headers.get(
                "Content-Type",
                ""
            )
        )

        raw = await response.read()

        log.info(
            "Pollinations response: "
            "HTTP=%s Content-Type=%s Size=%s",
            status,
            content_type,
            len(raw)
        )

        if status != 200:

            try:
                error_text = raw.decode(
                    "utf-8",
                    errors="replace"
                )

            except Exception:
                error_text = repr(raw[:500])

            raise RuntimeError(
                f"Pollinations HTTP {status}: "
                f"{error_text[:500]}"
            )

        if not raw:

            raise RuntimeError(
                "Pollinations returned an empty response."
            )

        # بررسی واقعی بودن خروجی تصویر
        try:

            with Image.open(
                BytesIO(raw)
            ) as test_image:

                test_image.verify()

        except Exception as e:

            preview = raw[:300]

            raise RuntimeError(
                "Provider returned non-image data: "
                f"{preview!r}"
            ) from e

        return raw


async def get_image(prompt):

    timeout = aiohttp.ClientTimeout(
        total=300,
        connect=30,
        sock_connect=30,
        sock_read=300
    )

    connector = aiohttp.TCPConnector(
        ssl=True,
        limit=10
    )

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector
    ) as session:

        errors = []

        # ====================================================
        # 1. سرویس دقیق سورس اصلی
        # ====================================================

        try:

            raw = await request_image(
                session,
                LEGACY_IMAGE_API,
                prompt,
                use_auth=False
            )

            log.info(
                "Image generated successfully "
                "using legacy Pollinations endpoint."
            )

            return raw

        except Exception as e:

            errors.append(
                f"legacy: {e}"
            )

            log.exception(
                "Legacy Pollinations request failed"
            )

        # ====================================================
        # 2. endpoint جدید Pollinations
        # ====================================================

        try:

            raw = await request_image(
                session,
                NEW_IMAGE_API,
                prompt,
                use_auth=bool(
                    POLLINATIONS_API_KEY
                )
            )

            log.info(
                "Image generated successfully "
                "using new Pollinations endpoint."
            )

            return raw

        except Exception as e:

            errors.append(
                f"new: {e}"
            )

            log.exception(
                "New Pollinations request failed"
            )

        raise RuntimeError(
            "All Pollinations endpoints failed:\n"
            + "\n".join(errors)
        )


# ============================================================
# IMAGE GENERATION
# ============================================================

async def generate(
    update,
    context,
    prompt
):

    message = update.effective_message

    user_id = update.effective_user.id

    limit = int(
        db.setting(
            "limit",
            DEFAULT_LIMIT
        )
    )

    used = db.usage(
        user_id
    )

    if limit > 0 and used >= limit:

        await message.reply_text(
            "⛔ سهمیه شما تمام شده است.\n\n"
            f"📊 مصرف: {used}/{limit}"
        )

        return

    if not prompt:

        await message.reply_text(
            "❌ پرامپت خالی است."
        )

        return

    if len(prompt) > MAX_PROMPT:

        await message.reply_text(
            f"❌ حداکثر طول پرامپت "
            f"{MAX_PROMPT} کاراکتر است."
        )

        return

    status_message = await message.reply_text(
        "🎨 در حال ساخت تصویر با هوش مصنوعی...\n"
        "⏳ لطفاً صبر کنید."
    )

    try:

        raw = await get_image(
            prompt
        )

        # تبدیل به PNG واقعی
        with Image.open(
            BytesIO(raw)
        ) as image:

            image.load()

            image = image.convert(
                "RGB"
            )

            output = BytesIO()

            image.save(
                output,
                format="PNG",
                optimize=True
            )

            output.seek(0)

            png_data = output.getvalue()

        await message.reply_document(
            document=png_data,
            filename="AI_Image.png",
            caption=(
                "✨ تصویر با موفقیت ساخته شد.\n\n"
                f"🎨 مدل: {IMAGE_MODEL}\n"
                f"📐 اندازه: "
                f"{IMAGE_WIDTH}×{IMAGE_HEIGHT}"
            )
        )

        db.inc(
            user_id
        )

        try:
            await status_message.delete()
        except Exception:
            pass

    except Exception as e:

        log.exception(
            "IMAGE GENERATION FAILED"
        )

        await status_message.edit_text(
            "❌ ساخت تصویر انجام نشد.\n\n"
            "🔧 خطای سرویس در لاگ برنامه ثبت شد.\n"
            "چند لحظه بعد دوباره امتحان کنید."
        )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    db.add_user(
        user
    )

    if not await gate(
        update,
        context
    ):
        return

    await update.effective_message.reply_text(
        "🤖 <b>ربات ساخت تصویر هوش مصنوعی</b>\n\n"
        "برای ساخت تصویر بنویس:\n\n"
        "<code>عکس English prompt</code>\n\n"
        "مثال:\n"
        "<code>عکس a futuristic city "
        "at sunset, cinematic</code>",
        parse_mode="HTML",
        reply_markup=user_kb()
    )


# ============================================================
# ADMIN
# ============================================================

async def admin_cmd(
    update,
    context
):

    if db.admin(
        update.effective_user.id
    ):

        await update.effective_message.reply_text(
            "🛠 <b>پنل مدیریت</b>",
            parse_mode="HTML",
            reply_markup=admin_kb()
        )


# ============================================================
# CALLBACKS
# ============================================================

async def callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    data = query.data

    # --------------------------------------------------------
    # USER BUTTONS
    # --------------------------------------------------------

    if data == "make":

        await query.message.reply_text(
            "🎨 مثال:\n\n"
            "<code>عکس futuristic city "
            "at sunset, cinematic</code>",
            parse_mode="HTML"
        )

        return

    if data == "quota":

        limit = int(
            db.setting(
                "limit",
                DEFAULT_LIMIT
            )
        )

        used = db.usage(
            query.from_user.id
        )

        limit_text = (
            "نامحدود"
            if limit == 0
            else str(limit)
        )

        await query.message.reply_text(
            f"📊 مصرف شما: {used}\n"
            f"🔢 محدودیت: {limit_text}"
        )

        return

    if data == "help":

        await query.message.reply_text(
            "📚 <b>راهنما</b>\n\n"
            "برای ساخت تصویر بنویس:\n"
            "<code>عکس English prompt</code>\n\n"
            "🖼️ خروجی به صورت PNG ارسال می‌شود.",
            parse_mode="HTML"
        )

        return

    if data == "check":

        if await subscribed(
            context.bot,
            query.from_user.id
        ):

            await query.message.reply_text(
                "✅ عضویت شما تأیید شد.",
                reply_markup=user_kb()
            )

        else:

            await query.answer(
                "❌ هنوز در همه کانال‌ها عضو نشده‌اید.",
                show_alert=True
            )

        return

    # --------------------------------------------------------
    # ADMIN BUTTONS
    # --------------------------------------------------------

    if not data.startswith("a:"):
        return

    if not db.admin(
        query.from_user.id
    ):

        await query.answer(
            "⛔ دسترسی ندارید.",
            show_alert=True
        )

        return

    action = data[2:]

    context.user_data[
        "admin_state"
    ] = action

    if action == "addch":

        await query.message.reply_text(
            "➕ یوزرنیم کانال را بفرست.\n"
            "مثال:\n"
            "@example"
        )

    elif action == "delch":

        await query.message.reply_text(
            "➖ یوزرنیم کانال را برای حذف بفرست."
        )

    elif action == "addad":

        await query.message.reply_text(
            "👤 آیدی عددی ادمین را بفرست."
        )

    elif action == "delad":

        await query.message.reply_text(
            "🗑 آیدی عددی ادمین را برای حذف بفرست."
        )

    elif action == "broadcast":

        await query.message.reply_text(
            "📨 متن پیام همگانی را بفرست."
        )

    elif action == "advertising":

        await query.message.reply_text(
            "📢 متن تبلیغ را بفرست."
        )

    elif action == "limit":

        await query.message.reply_text(
            "🔢 تعداد مجاز ساخت تصویر را بفرست.\n\n"
            "0 = نامحدود"
        )

    elif action == "stats":

        await query.message.reply_text(
            "📊 <b>آمار ربات</b>\n\n"
            f"👤 کاربران: {len(db.users())}\n"
            f"👥 گروه‌ها: {len(db.groups())}\n"
            f"🛡 ادمین‌ها: {len(db.admins())}\n"
            f"📢 کانال‌ها: {len(db.channels())}",
            parse_mode="HTML"
        )

        context.user_data.pop(
            "admin_state",
            None
        )


# ============================================================
# ADMIN STATE HANDLER
# ============================================================

async def admin_state_message(
    update,
    context
):

    if not update.message:
        return

    if not db.admin(
        update.effective_user.id
    ):
        return

    state = context.user_data.get(
        "admin_state"
    )

    if not state:
        return False

    text = update.message.text.strip()

    # --------------------------------------------------------
    # ADD CHANNEL
    # --------------------------------------------------------

    if state == "addch":

        if not text.startswith("@"):

            await update.message.reply_text(
                "❌ یوزرنیم باید با @ شروع شود."
            )

            return True

        db.add_channel(
            text
        )

        await update.message.reply_text(
            "✅ کانال با موفقیت اضافه شد."
        )

        context.user_data.pop(
            "admin_state",
            None
        )

        return True

    # --------------------------------------------------------
    # DELETE CHANNEL
    # --------------------------------------------------------

    if state == "delch":

        db.del_channel(
            text
        )

        await update.message.reply_text(
            "✅ کانال حذف شد."
        )

        context.user_data.pop(
            "admin_state",
            None
        )

        return True

    # --------------------------------------------------------
    # ADD ADMIN
    # --------------------------------------------------------

    if state == "addad":

        try:

            user_id = int(
                text
            )

            db.add_admin(
                user_id
            )

            await update.message.reply_text(
                "✅ ادمین اضافه شد."
            )

            context.user_data.pop(
                "admin_state",
                None
            )

        except ValueError:

            await update.message.reply_text(
                "❌ آیدی عددی نامعتبر است."
            )

        return True

    # --------------------------------------------------------
    # DELETE ADMIN
    # --------------------------------------------------------

    if state == "delad":

        try:

            user_id = int(
                text
            )

            if user_id == OWNER_ID:

                await update.message.reply_text(
                    "❌ مالک اصلی قابل حذف نیست."
                )

            else:

                db.del_admin(
                    user_id
                )

                await update.message.reply_text(
                    "✅ ادمین حذف شد."
                )

                context.user_data.pop(
                    "admin_state",
                    None
                )

        except ValueError:

            await update.message.reply_text(
                "❌ آیدی عددی نامعتبر است."
            )

        return True

    # --------------------------------------------------------
    # BROADCAST
    # --------------------------------------------------------

    if state == "broadcast":

        ok, fail = await send_many(
            context.bot,
            db.users(),
            text
        )

        await update.message.reply_text(
            f"📨 پیام همگانی ارسال شد.\n\n"
            f"✅ موفق: {ok}\n"
            f"❌ ناموفق: {fail}"
        )

        context.user_data.pop(
            "admin_state",
            None
        )

        return True

    # --------------------------------------------------------
    # ADVERTISING
    # --------------------------------------------------------

    if state == "advertising":

        ids = list(
            dict.fromkeys(
                db.users()
                + db.groups()
            )
        )

        ok, fail = await send_many(
            context.bot,
            ids,
            text
        )

        await update.message.reply_text(
            f"📢 تبلیغ ارسال شد.\n\n"
            f"✅ موفق: {ok}\n"
            f"❌ ناموفق: {fail}"
        )

        context.user_data.pop(
            "admin_state",
            None
        )

        return True

    # --------------------------------------------------------
    # LIMIT
    # --------------------------------------------------------

    if state == "limit":

        try:

            number = int(
                text
            )

            if number < 0:
                raise ValueError

            db.set_setting(
                "limit",
                number
            )

            await update.message.reply_text(
                "✅ محدودیت با موفقیت ذخیره شد."
            )

            context.user_data.pop(
                "admin_state",
                None
            )

        except ValueError:

            await update.message.reply_text(
                "❌ عدد نامعتبر است."
            )

        return True

    return False


# ============================================================
# SEND MANY
# ============================================================

async def send_many(
    bot,
    ids,
    text
):

    success = 0
    failed = 0

    for chat_id in ids:

        try:

            await bot.send_message(
                chat_id=chat_id,
                text=text
            )

            success += 1

        except Exception as e:

            failed += 1

            log.warning(
                "Broadcast failed for %s: %s",
                chat_id,
                e
            )

        await asyncio.sleep(
            0.05
        )

    return success, failed


# ============================================================
# PRIVATE MESSAGE
# ============================================================

async def private_message(
    update,
    context
):

    message = update.message

    if not message:
        return

    user = update.effective_user

    db.add_user(
        user
    )

    # Admin state اولویت دارد
    if await admin_state_message(
        update,
        context
    ):
        return

    if not await gate(
        update,
        context
    ):
        return

    text = message.text.strip()

    if (
        text == "پنل مدیریت"
        and db.admin(user.id)
    ):

        await message.reply_text(
            "🛠 <b>پنل مدیریت</b>",
            parse_mode="HTML",
            reply_markup=admin_kb()
        )

        return

    prompt = prompt_of(
        text
    )

    if prompt:

        await generate(
            update,
            context,
            prompt
        )

    else:

        await message.reply_text(
            "🎨 برای ساخت تصویر بنویس:\n\n"
            "<code>عکس English prompt</code>",
            parse_mode="HTML",
            reply_markup=user_kb()
        )


# ============================================================
# GROUP MESSAGE
# ============================================================

async def group_message(
    update,
    context
):

    message = update.message

    if not message:
        return

    db.add_group(
        message.chat
    )

    prompt = prompt_of(
        message.text
    )

    if prompt:

        await generate(
            update,
            context,
            prompt
        )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context
):

    log.exception(
        "Unhandled bot exception:",
        exc_info=context.error
    )


# ============================================================
# MAIN
# ============================================================

def main():

    log.info(
        "Starting AI Image Bot..."
    )

    log.info(
        "Image service: %s",
        LEGACY_IMAGE_API
    )

    log.info(
        "Fallback service: %s",
        NEW_IMAGE_API
    )

    log.info(
        "Image model: %s",
        IMAGE_MODEL
    )

    log.info(
        "Image size: %sx%s",
        IMAGE_WIDTH,
        IMAGE_HEIGHT
    )

    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin_cmd
        )
    )

    # Callback buttons
    application.add_handler(
        CallbackQueryHandler(
            callback
        )
    )

    # Group messages
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & filters.TEXT
            & ~filters.COMMAND,
            group_message
        )
    )

    # Private messages
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & filters.TEXT
            & ~filters.COMMAND,
            private_message
        )
    )

    application.add_error_handler(
        error_handler
    )

    log.info(
        "Bot is running..."
    )

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
