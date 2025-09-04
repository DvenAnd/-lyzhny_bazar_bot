"""
Telegram бот модерации объявлений для канала (Render + webhooks, aiogram v3).

Устанавливаем зависимости:
    pip install -r requirements.txt

Локальный запуск (для отладки без вебхука не рекомендуется на Render):
    python main_webhook.py

Настройка вебхука (Render):
1) В Render создайте Web Service из репозитория с этими файлами.
2) Переменные окружения (Environment):
   - BOT_TOKEN   = <ваш_токен_бота>
   - ADMIN_ID    = 919732599                 # либо ADMIN_IDS = 919732599,111222333
   - CHANNEL_ID  = @lyzhny_bazar             # можно @username или -100xxxxxxxxxx
   - WEBHOOK_URL = https://lyzhny-bazar-bot.onrender.com/telegram
   - (необязательно) WEBHOOK_SECRET = <случайная_строка> для проверки подписи Telegram
3) Build Command:   pip install -r requirements.txt
   Start Command:   python main_webhook.py
4) Убедитесь, что бот — админ канала.
5) Вебхук выставляется автоматически на старте (setWebhook).

Функционал:
- Принимает текст, фото, видео (включая медиа-альбом).
- Пересылает на модерацию администратору(ам).
- Кнопки модерации (Inline): ✅ Одобрить / ❌ Отклонить.
- После одобрения публикует в канал.
- Без БД — временные данные в памяти процесса.

Образец для команды /start (высылается пользователю):

📝 Образец подачи объявления
Заголовок: Продажа лыж Fischer Speedmax 3D Skate
Описание:
Состояние: Б/у, отличное состояние
Размер: 186 см
Год выпуска: 2022
Дополнительно: Установлены крепления Rottefella Xcelerator
Цена: 25 000 ₽
Адрес: Дмитров
Контакты: @username или +7 912 345-67-89
Фотографии: [Прикрепите до 3-х качественных фото]
"""

import asyncio
import os
import uuid
from typing import Dict, List, Optional, Tuple

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InputMediaPhoto,
    InputMediaVideo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# ---------------------------- Конфигурация из окружения ----------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в переменных окружения.")

# Поддержка одного или нескольких админов: ADMIN_ID или ADMIN_IDS="1,2,3"
_admin_ids_env = os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "")).strip()
ADMIN_IDS: List[int] = []
if _admin_ids_env:
    for part in _admin_ids_env.split(","):
        part = part.strip()
        if part:
            try:
                ADMIN_IDS.append(int(part))
            except ValueError:
                pass
if not ADMIN_IDS:
    raise RuntimeError("Не задан ADMIN_ID/ADMIN_IDS в переменных окружения.")

CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "").strip()
if not CHANNEL_ID_RAW:
    raise RuntimeError("Не задан CHANNEL_ID в переменных окружения.")
CHANNEL_ID = CHANNEL_ID_RAW  # может быть @username или -100XXXXXXXXXX

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
if not WEBHOOK_URL:
    raise RuntimeError("Не задан WEBHOOK_URL (например, https://<service>.onrender.com/telegram)")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip() or None

PORT = int(os.getenv("PORT", "10000"))
HOST = "0.0.0.0"

# ---------------------------- Инициализация бота/диспетчера ----------------------------

# aiogram >= 3.7: parse_mode передаётся через DefaultBotProperties
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ---------------------------- Память процесса (без БД) ----------------------------

# Буфер для медиа-альбомов: media_group_id -> (список Message, task)
_album_buffers: Dict[str, Tuple[List[Message], Optional[asyncio.Task]]] = {}

# Отложенные наборы медиа для публикации: token -> данные альбома
# Храним только до модерации
_pending_albums: Dict[str, Dict] = {}

# ---------------------------- Вспомогательные функции ----------------------------

SAMPLE_TEXT = (
    "📝 <b>Образец подачи объявления</b>\\n"
    "Заголовок: Продажа лыж Fischer Speedmax 3D Skate\\n"
    "Описание:\\n"
    "Состояние: Б/у, отличное состояние\\n"
    "Размер: 186 см\\n"
    "Год выпуска: 2022\\n"
    "Дополнительно: Установлены крепления Rottefella Xcelerator\\n"
    "Цена: 25 000 ₽\\n"
    "Адрес: Дмитров\\n"
    "Контакты: @username или +7 912 345-67-89\\n"
    "Фотографии: [Прикрепите до 3-х качественных фото]"
)

def moderation_keyboard(data: str) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"approve:{data}")
    kb.button(text="❌ Отклонить", callback_data=f"reject:{data}")
    kb.adjust(2)
    return kb

async def notify_admins(text: str):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            # Игнорируем ошибки доставки админам
            pass

async def send_single_for_moderation(msg: Message):
    """
    Для одиночного сообщения (текст/фото/видео) — копируем админу и отправляем кнопки.
    Для публикации после одобрения используем copy_message из оригинального чата.
    """
    from_chat_id = msg.chat.id
    message_id = msg.message_id

    # Копируем само объявление админу(ам)
    for admin_id in ADMIN_IDS:
        try:
            await bot.copy_message(chat_id=admin_id, from_chat_id=from_chat_id, message_id=message_id)
        except Exception:
            pass

        # Отдельным сообщением — кнопки модерации
        kb = moderation_keyboard(f"single:{from_chat_id}:{message_id}")
        preview = (
            f"🧾 <b>Новая заявка на модерацию</b>\\n"
            f"От: <code>{from_chat_id}</code> • msg_id: <code>{message_id}</code>\\n\\n"
            f"Нажмите кнопку ниже, чтобы одобрить или отклонить публикацию."
        )
        try:
            await bot.send_message(admin_id, preview, reply_markup=kb.as_markup())
        except Exception:
            pass

async def flush_album(media_group_id: str):
    """
    Отправка накопленных медиа-альбомов админу(ам) и постановка в очередь модерации.
    """
    buffer_entry = _album_buffers.pop(media_group_id, None)
    if not buffer_entry:
        return
    items, _ = buffer_entry
    if not items:
        return

    # Подготовим медиа для пересылки админу и для дальнейшей публикации
    medias_for_admin = []
    medias_for_channel = []
    caption_used = False
    album_type = None  # "photo" / "video" / "mixed"
    for m in items:
        caption = m.caption if (m.caption and not caption_used) else None
        if m.photo:
            file_id = m.photo[-1].file_id
            medias_for_admin.append(InputMediaPhoto(media=file_id, caption=caption))
            medias_for_channel.append(("photo", file_id, caption))
            album_type = "photo" if album_type in (None, "photo") else "mixed"
        elif m.video:
            file_id = m.video.file_id
            medias_for_admin.append(InputMediaVideo(media=file_id, caption=caption))
            medias_for_channel.append(("video", file_id, caption))
            album_type = "video" if album_type in (None, "video") else "mixed"
        caption_used = caption_used or bool(caption)

    token = uuid.uuid4().hex[:16]
    _pending_albums[token] = {
        "from_chat_id": items[0].chat.id,
        "media": medias_for_channel,  # список кортежей ("photo"/"video", file_id, caption|None)
        "album_type": album_type,
        "used": False,
    }

    # Отправляем медиагруппу админу(ам)
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_media_group(chat_id=admin_id, media=medias_for_admin)
        except Exception:
            pass

        # Отдельным сообщением — кнопки модерации
        kb = moderation_keyboard(f"album:{token}")
        preview = (
            f"🧾 <b>Новая заявка (альбом) на модерацию</b>\\n"
            f"От: <code>{items[0].chat.id}</code> • media_group: <code>{media_group_id}</code>\\n\\n"
            f"Нажмите кнопку ниже, чтобы одобрить или отклонить публикацию."
        )
        try:
            await bot.send_message(admin_id, preview, reply_markup=kb.as_markup())
        except Exception:
            pass

# ---------------------------- Хэндлеры ----------------------------

@router.message(CommandStart())
async def on_start(message: Message):
    await message.answer(
        "Привет! Пришлите текст объявления, фото или видео — я отправлю на модерацию админу. "
        "После одобрения публикация попадёт в канал.\\n\\n" + SAMPLE_TEXT
    )

# Альбомы (media_group)
@router.message(F.media_group_id)
async def on_album_piece(message: Message):
    mgid = str(message.media_group_id)
    bucket, task = _album_buffers.get(mgid, ([], None))
    bucket.append(message)
    if task is None:
        # Небольшая задержка, чтобы собрать весь альбом
        task = asyncio.create_task(_schedule_album_flush(mgid))
    _album_buffers[mgid] = (bucket, task)

async def _schedule_album_flush(media_group_id: str, delay: float = 1.2):
    await asyncio.sleep(delay)
    await flush_album(media_group_id)

# Фото/видео одиночные
@router.message(F.photo & ~F.media_group_id)
async def on_single_photo(message: Message):
    await send_single_for_moderation(message)

@router.message(F.video & ~F.media_group_id)
async def on_single_video(message: Message):
    await send_single_for_moderation(message)

# Текст
@router.message(F.text & ~F.via_bot)
async def on_text(message: Message):
    # Игнорируем команды (кроме /start)
    if message.text.startswith("/"):
        return
    await send_single_for_moderation(message)

# Кнопки модерации — одобрение
@router.callback_query(F.data.startswith("approve:"))
async def on_approve(callback: CallbackQuery):
    """
    Форматы данных:
      approve:single:<from_chat_id>:<message_id>
      approve:album:<token>
    """
    try:
        payload = callback.data.split(":", 1)[1]
        kind, rest = payload.split(":", 1)
        if kind == "single":
            # Публикуем копией оригинального сообщения
            from_chat_id_str, msg_id_str = rest.split(":")
            from_chat_id = int(from_chat_id_str)
            msg_id = int(msg_id_str)
            await bot.copy_message(chat_id=CHANNEL_ID, from_chat_id=from_chat_id, message_id=msg_id)
            # Уведомления
            await callback.answer("Опубликовано ✅")
            try:
                await bot.send_message(from_chat_id, "✅ Ваше объявление опубликовано в канале.")
            except Exception:
                pass
        elif kind == "album":
            token = rest
            data = _pending_albums.get(token)
            if not data or data.get("used"):
                await callback.answer("Устарело или уже обработано.", show_alert=True)
                return
            # Собираем медиагруппу для канала
            medias = []
            caption_used = False
            for media_type, file_id, caption in data["media"]:
                caption_to_use = caption if (caption and not caption_used) else None
                if media_type == "photo":
                    medias.append(InputMediaPhoto(media=file_id, caption=caption_to_use))
                else:
                    medias.append(InputMediaVideo(media=file_id, caption=caption_to_use))
                caption_used = caption_used or bool(caption_to_use)
            # Публикация
            await bot.send_media_group(chat_id=CHANNEL_ID, media=medias)
            data["used"] = True
            # Уведомления
            await callback.answer("Опубликовано ✅")
            try:
                await bot.send_message(data["from_chat_id"], "✅ Ваше объявление (альбом) опубликовано в канале.")
            except Exception:
                pass
        else:
            await callback.answer("Неизвестный тип.", show_alert=True)
            return
    except Exception as e:
        await callback.answer("Ошибка публикации.", show_alert=True)

# Кнопки модерации — отклонение
@router.callback_query(F.data.startswith("reject:"))
async def on_reject(callback: CallbackQuery):
    """
    Форматы:
      reject:single:<from_chat_id>:<message_id>
      reject:album:<token>
    """
    try:
        payload = callback.data.split(":", 1)[1]
        kind, rest = payload.split(":", 1)
        if kind == "single":
            from_chat_id = int(rest.split(":")[0])
            try:
                await bot.send_message(from_chat_id, "❌ Ваше объявление отклонено модератором.")
            except Exception:
                pass
        elif kind == "album":
            token = rest
            data = _pending_albums.get(token)
            if data and not data.get("used"):
                try:
                    await bot.send_message(data["from_chat_id"], "❌ Ваше объявление (альбом) отклонено модератором.")
                except Exception:
                    pass
                data["used"] = True
        await callback.answer("Отклонено.")
    except Exception:
        await callback.answer("Ошибка обработки.", show_alert=True)

# ---------------------------- AIOHTTP (Webhook) ----------------------------

async def on_startup_app(_: web.Application):
    # Выставляем вебхук
    try:
        await bot.set_webhook(
            url=WEBHOOK_URL,
            drop_pending_updates=True,
            secret_token=WEBHOOK_SECRET,
        )
        # По желанию можно уведомить админов о запуске:
        await notify_admins("🔔 Бот запущен, вебхук установлен.")
    except Exception:
        pass

async def on_shutdown_app(_: web.Application):
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception:
        pass

async def healthcheck(_: web.Request):
    return web.Response(text="OK")

def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup_app)
    app.on_shutdown.append(on_shutdown_app)

    # Регистрируем хэндлер Telegram webhook
    req_handler = SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET)
    # Путь должен совпадать с WEBHOOK_URL (окончание)
    # Например: WEBHOOK_URL = https://<service>.onrender.com/telegram
    req_handler.register(app, path="/telegram")

    # Healthcheck
    app.router.add_get("/", healthcheck)
    app.router.add_get("/health", healthcheck)

    # Обязательная интеграция с aiogram (graceful shutdown)
    setup_application(app, dp, bot=bot)
    return app

if __name__ == "__main__":
    web.run_app(build_app(), host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
