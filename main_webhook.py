
"""
Webhook-ready Telegram classifieds bot for "Лыжный Базар"
- Same features as main.py, but runs as a WEB SERVER suitable for Render Free plan.
- Uses python-telegram-bot's built-in webhook runner (aiohttp under the hood).

Env vars (in addition to .env.example):
- MODE=WEBHOOK
- WEBHOOK_URL=https://<your-service>.onrender.com/telegram
- PORT (Render will provide automatically)

Run locally for testing (optional):
PORT=8080 MODE=WEBHOOK WEBHOOK_URL=http://localhost:8080/telegram python main_webhook.py
"""

import asyncio
import logging
import os
import json
from datetime import datetime
from typing import Dict, Any, List, Optional

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaVideo
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, CallbackQueryHandler, ContextTypes, filters
)

# ----- Logging -----
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ----- Conversation States -----
(
    CAT, ITEM, SIZE, CONDITION, PRICE, CITY, DELIVERY, CONTACT, DESCRIPTION, MEDIA, CONFIRM
) = range(11)

# ----- Simple persistence -----
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
SUBMISSIONS_FILE = os.path.join(DATA_DIR, "submissions.json")

def load_db() -> Dict[str, Any]:
    if not os.path.exists(SUBMISSIONS_FILE):
        return {}
    with open(SUBMISSIONS_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return {}

def save_db(db: Dict[str, Any]) -> None:
    with open(SUBMISSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

# ----- Helpers -----
def env(name: str, default: Optional[str]=None) -> str:
    val = os.getenv(name, default)
    if val is None:
        raise RuntimeError(f"Missing required env var: {name}")
    return val

def get_admin_ids() -> List[int]:
    raw = env("ADMIN_IDS", "")
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                ids.append(int(part))
            except ValueError:
                logger.warning("ADMIN_IDS contains a non-integer: %s", part)
    return ids

def get_channel_id() -> str:
    return env("CHANNEL_ID")  # e.g. @lyzhny_bazar or -1001234567890

def format_listing(data: Dict[str, Any]) -> str:
    # Build hashtags from key fields
    tags = []
    if data.get("category"):
        tags.append(f"#{data['category'].replace(' ', '_')}")
    if data.get("size"):
        tags.append(f"#{data['size'].replace(' ', '_')}")
    if data.get("condition"):
        tags.append(f"#{data['condition'].replace(' ', '_')}")
    if data.get("city"):
        tags.append(f"#{data['city'].replace(' ', '_')}")

    tags_line = " ".join(tags) if tags else ""

    text = (
        f"{tags_line}\n\n"
        f"<b>Товар:</b> {data.get('item','—')}\n"
        f"<b>Размер:</b> {data.get('size','—')}\n"
        f"<b>Состояние:</b> {data.get('condition','—')}\n"
        f"<b>Цена:</b> {data.get('price','—')}\n"
        f"<b>Город:</b> {data.get('city','—')}\n"
        f"<b>Доставка:</b> {data.get('delivery','—')}\n"
        f"<b>Контакт:</b> {data.get('contact','—')}\n\n"
        f"<b>Описание:</b> {data.get('description','—')}"
    )
    return text

def is_admin(user_id: int) -> bool:
    return user_id in get_admin_ids()

# ----- Commands -----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Привет! Это бот для подачи объявлений «Лыжный Базар».\n"
        "Отвечай на вопросы по очереди. В любой момент можно /cancel.\n\n"
        "Сначала выбери категорию (можно просто написать):\n"
        "Примеры: Лыжи, Ботинки, Палки, Крепления, Одежда, Лыжероллеры"
    )
    context.user_data.clear()
    context.user_data["media"] = []  # collect photos/videos
    return CAT

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено. Возвращайся, когда будешь готов. /start")
    context.user_data.clear()
    return ConversationHandler.END

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rules_text = (
        "Правила коротко:\n"
        "• 1 объявление = 1 товар\n"
        "• Обязательно: цена, город, размер, состояние, 3+ фото\n"
        "• Торг и вопросы — в ЛС\n"
        "• «Подъём» — не чаще, чем раз в 72 часа\n"
        "• Запрещены оффтоп/мошенничество/чужие контакты без согласия"
    )
    await update.message.reply_text(rules_text)

# ----- Conversation Steps -----
async def set_cat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["category"] = update.message.text.strip()
    await update.message.reply_text("Название товара (например: Salomon S/Race Skate BOA):")
    return ITEM

async def set_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["item"] = update.message.text.strip()
    await update.message.reply_text("Размер (например: 38 / 24.0 / 187 см):")
    return SIZE

async def set_size(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["size"] = update.message.text.strip()
    await update.message.reply_text("Состояние (новые / как новые / б/у, пробег):")
    return CONDITION

async def set_condition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["condition"] = update.message.text.strip()
    await update.message.reply_text("Цена (например: 29 000 ₽):")
    return PRICE

async def set_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["price"] = update.message.text.strip()
    await update.message.reply_text("Город (например: Москва / Дмитров):")
    return CITY

async def set_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["city"] = update.message.text.strip()
    await update.message.reply_text("Доставка (например: СДЭК/личная встреча):")
    return DELIVERY

async def set_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["delivery"] = update.message.text.strip()
    await update.message.reply_text("Контакт (например: @username):")
    return CONTACT

async def set_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["contact"] = update.message.text.strip()
    await update.message.reply_text(
        "Короткое описание (1–2 предложения: сезон, уровень, нюансы):"
    )
    return DESCRIPTION

async def set_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["description"] = update.message.text.strip()
    await update.message.reply_text(
        "Пришли 3–8 фото/видео (можно альбомом). Когда закончишь — отправь «готово»."
    )
    return MEDIA

async def collect_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Accept photos and videos
    media_list = context.user_data.get("media", [])
    if update.message.photo:
        # take best resolution
        file_id = update.message.photo[-1].file_id
        media_list.append(("photo", file_id))
    elif update.message.video:
        file_id = update.message.video.file_id
        media_list.append(("video", file_id))
    context.user_data["media"] = media_list
    return MEDIA

async def media_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    media_list = context.user_data.get("media", [])
    if len(media_list) == 0:
        await update.message.reply_text("Не вижу медиа. Пришли хотя бы 1 фото, пожалуйста.")
        return MEDIA

    # Compose preview
    text = format_listing(context.user_data)

    # Send preview to user
    if len(media_list) == 1:
        kind, fid = media_list[0]
        if kind == "photo":
            await update.message.reply_photo(fid, caption=text, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_video(fid, caption=text, parse_mode=ParseMode.HTML)
    else:
        # prepare album
        album = []
        for i, (kind, fid) in enumerate(media_list):
            if i == 0:
                caption = text
            else:
                caption = None
            if kind == "photo":
                album.append(InputMediaPhoto(media=fid, caption=caption, parse_mode=ParseMode.HTML if caption else None))
            else:
                album.append(InputMediaVideo(media=fid, caption=caption, parse_mode=ParseMode.HTML if caption else None))
        await update.message.reply_media_group(album)

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Отправить на модерацию", callback_data="submit")],
         [InlineKeyboardButton("✏️ Исправить", callback_data="edit")]]
    )
    await update.message.reply_text("Проверь объявление. Всё верно?", reply_markup=keyboard)
    return CONFIRM

async def confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "edit":
        await query.edit_message_text("Окей, начнём заново. /start")
        context.user_data.clear()
        return ConversationHandler.END

    # submit
    db = load_db()
    sub_id = f"{query.from_user.id}_{int(datetime.utcnow().timestamp())}"
    db[sub_id] = {
        "user_id": query.from_user.id,
        "username": query.from_user.username,
        "data": context.user_data,
        "created_at": datetime.utcnow().isoformat()
    }
    save_db(db)

    # notify admins
    text = format_listing(context.user_data)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Одобрить и опубликовать", callback_data=f"approve:{sub_id}")],
         [InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{sub_id}")]]
    )

    media_list = context.user_data.get("media", [])
    admin_ids = get_admin_ids()
    for aid in admin_ids:
        try:
            if len(media_list) == 1:
                kind, fid = media_list[0]
                if kind == "photo":
                    await context.bot.send_photo(chat_id=aid, photo=fid, caption=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
                else:
                    await context.bot.send_video(chat_id=aid, video=fid, caption=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            else:
                album = []
                for i, (kind, fid) in enumerate(media_list):
                    caption = text if i == 0 else None
                    if kind == "photo":
                        album.append(InputMediaPhoto(media=fid, caption=caption, parse_mode=ParseMode.HTML if caption else None))
                    else:
                        album.append(InputMediaVideo(media=fid, caption=caption, parse_mode=ParseMode.HTML if caption else None))
                msgs = await context.bot.send_media_group(chat_id=aid, media=album)
                # add buttons under a separate msg
                await context.bot.send_message(chat_id=aid, text="Публикация:", reply_markup=keyboard)
        except Exception as e:
            logger.error("Failed to notify admin %s: %s", aid, e)

    await query.edit_message_text("Отправлено на модерацию. Спасибо!")
    context.user_data.clear()
    return ConversationHandler.END

async def moderation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if not is_admin(query.from_user.id):
        await query.edit_message_text("Недостаточно прав.")
        return

    if data.startswith("approve:"):
        sub_id = data.split(":", 1)[1]
        db = load_db()
        sub = db.get(sub_id)
        if not sub:
            await query.edit_message_text("Заявка не найдена или уже обработана.")
            return
        listing = sub["data"]
        text = format_listing(listing)
        channel_id = get_channel_id()

        # post to channel
        media_list = listing.get("media", [])
        try:
            if len(media_list) == 1:
                kind, fid = media_list[0]
                if kind == "photo":
                    await context.bot.send_photo(chat_id=channel_id, photo=fid, caption=text, parse_mode=ParseMode.HTML)
                else:
                    await context.bot.send_video(chat_id=channel_id, video=fid, caption=text, parse_mode=ParseMode.HTML)
            else:
                album = []
                for i, (kind, fid) in enumerate(media_list):
                    caption = text if i == 0 else None
                    if kind == "photo":
                        album.append(InputMediaPhoto(media=fid, caption=caption, parse_mode=ParseMode.HTML if caption else None))
                    else:
                        album.append(InputMediaVideo(media=fid, caption=caption, parse_mode=ParseMode.HTML if caption else None))
                await context.bot.send_media_group(chat_id=channel_id, media=album)
            await query.edit_message_text(f"✅ Опубликовано в {channel_id}")
            # remove from db
            db.pop(sub_id, None)
            save_db(db)
        except Exception as e:
            logger.exception("Publish error: %s", e)
            await query.edit_message_text(f"Ошибка публикации: {e}")

    elif data.startswith("reject:"):
        sub_id = data.split(":", 1)[1]
        db = load_db()
        if sub_id in db:
            db.pop(sub_id, None)
            save_db(db)
        await query.edit_message_text("❌ Отклонено и удалено.")

# ----- Build Application -----
def build_app() -> Application:
    token = env("BOT_TOKEN")
    app: Application = ApplicationBuilder().token(token).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_cat)],
            ITEM: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item)],
            SIZE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_size)],
            CONDITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_condition)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_price)],
            CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_city)],
            DELIVERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_delivery)],
            CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_contact)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_description)],
            MEDIA: [
                MessageHandler((filters.PHOTO | filters.VIDEO) & ~filters.COMMAND, collect_media),
                MessageHandler(filters.Regex("^(готово|Готово|done|Done)$"), media_done),
            ],
            CONFIRM: [CallbackQueryHandler(confirm_handler, pattern="^(submit|edit)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CallbackQueryHandler(moderation_handler, pattern="^(approve:|reject:)"))

    return app

if __name__ == "__main__":
    # SWITCH TO WEBHOOK MODE
    MODE = os.getenv("MODE", "WEBHOOK").upper()
    app = build_app()

    if MODE == "WEBHOOK":
        # Render sets PORT automatically
        port = int(os.getenv("PORT", "10000"))
        webhook_url = env("WEBHOOK_URL")
        # Run webhook server; PTB will set webhook for you
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            webhook_url=webhook_url,
            webhook_path="/telegram",
            drop_pending_updates=True,
        )
    else:
        # Fallback: polling (not ideal for free web hosting)
        app.run_polling(drop_pending_updates=True)
