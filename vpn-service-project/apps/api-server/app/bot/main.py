"""
Точка входа бота.

Два режима:
  polling  — для локальной разработки (сервер и домен не нужны);
  webhook  — для прода (Telegram сам шлёт апдейты на наш API).

Запуск:  python -m app.bot.main
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Dispatcher
from aiogram.fsm.storage.redis import RedisStorage

from app.bot.handlers import router
from app.bot.notifications import create_bot
from app.config import get_settings
from app.db import close_connections
from app.utils.logging_setup import setup_logging

log = logging.getLogger(__name__)
settings = get_settings()


def build_dispatcher() -> Dispatcher:
    """
    Диспетчер с хранилищем состояний в Redis.

    Redis вместо памяти нужен, чтобы диалоги не терялись при перезапуске
    контейнера и работали при нескольких копиях бота.
    """
    storage = RedisStorage.from_url(settings.redis_url)
    dispatcher = Dispatcher(storage=storage)
    dispatcher.include_router(router)
    return dispatcher


async def main() -> None:
    setup_logging("bot")
    bot = create_bot()
    dispatcher = build_dispatcher()

    me = await bot.get_me()
    log.info("Бот запущен: @%s (режим %s)", me.username, settings.bot_mode)

    try:
        if settings.bot_mode == "webhook":
            # В режиме webhook апдейты принимает FastAPI (app/main.py),
            # здесь только регистрируем адрес в Telegram.
            webhook_url = f"{settings.public_base_url.rstrip('/')}/webhook/telegram"
            await bot.set_webhook(
                webhook_url,
                secret_token=settings.telegram_webhook_secret,
                drop_pending_updates=True,
            )
            log.info("Webhook установлен: %s", webhook_url)
            # Процесс остаётся живым: он обслуживает воркер-задачи бота
            # Держим процесс живым до сигнала остановки
            await asyncio.Event().wait()
        else:
            await bot.delete_webhook(drop_pending_updates=True)
            await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()
        await close_connections()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен")
