from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

start_router = Router()

@start_router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(
        "Приветствую! 👋\n\n"
        "Введите парт-номер или описание компонента — найду характеристики и аналоги."
    )


@start_router.message(Command("help"))
async def help_handler(message: Message) -> None:
    await message.answer(
        "Отправьте парт-номер компонента одним сообщением.\n\n"
        "Бот найдет техническую документацию, извлечет характеристики,"
        "подберет аналоги и сформирует сравнительную XLSX-таблицу."
    )
