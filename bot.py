import os
import asyncio
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart

bot_token = os.getenv("bot_token")

dp = Dispatcher()

@dp.message(CommandStart())
async def start_handler(message: types.Message):
    # если ты используешь привязку с кодом:
    args = message.text.split(maxsplit=1)
    code = args[1].strip() if len(args) == 2 else None

    if code:
        await message.answer(
            "✅ Steam Track n Buy подключён.\n"
            "Теперь уведомления будут приходить сюда."
        )
        # тут можешь сохранить code -> chat_id в файл/базу, если нужно
    else:
        await message.answer("Привет! Нажми Start через ссылку из расширения 🙂")

async def healthz(request):
    return web.Response(text="ok")

async def main():
    bot = Bot(token=bot_token)
    app = web.Application()
    app.router.add_get("/healthz", healthz)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    # polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
