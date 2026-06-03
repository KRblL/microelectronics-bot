import asyncio
import logging
import re

from aiogram import F, Router
from aiogram.types import BufferedInputFile, Message

from backend.datasheet_downloader import download_pdf, search_datasheet
from backend.grok_api import GrokComponentFinder
from backend.report_builder import build_excel, parse_analogs_blocks, parse_component_block, split_grok_answer
from backend.text_extractor import extract_text_from_pdf

datasheet_router = Router()
logger = logging.getLogger(__name__)
finder = GrokComponentFinder()
busy_users: set[int] = set()


def is_part_number(text: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./+#-]{1,60}", text.strip()))


async def send_long_text(message: Message, text: str) -> None:
    limit = 3800
    chunks = [text[index : index + limit] for index in range(0, len(text), limit)]
    for chunk in chunks:
        await message.answer(chunk)


@datasheet_router.message(F.text)
async def datasheet_handler(message: Message) -> None:
    text = (message.text or "").strip()
    if not is_part_number(text):
        await message.answer("Введите корректный парт-номер компонента.")
        return

    user_id = message.from_user.id if message.from_user else message.chat.id
    if user_id in busy_users:
        await message.answer("Ваш предыдущий запрос еще обрабатывается. Дождитесь результата.")
        return

    busy_users.add(user_id)
    try:
        await message.answer("Принял запрос ✅\nИщу данные по компоненту…")

        result = await search_datasheet(text)
        if result["status"] != "ok" or not result["items"]:
            await message.answer(f"Техническая документация для {text} не найдена.")
            return

        item = result["items"][0]
        pdf_path = await download_pdf(item["link"], item["part"], item["manufacturer"])
        if not pdf_path:
            await message.answer("Не удалось загрузить PDF-документацию.")
            return

        # await message.answer("Документация найдена. Извлекаю текст и анализирую характеристики…")
        loop = asyncio.get_running_loop()
        documentation_text = await loop.run_in_executor(None, extract_text_from_pdf, pdf_path)
        answer_text, stats = await loop.run_in_executor(None, finder.find_component, text, documentation_text)

        await send_long_text(message, answer_text)

        if not stats.get("success"):
            return

        main_text, analogs_text = split_grok_answer(answer_text)
        base = parse_component_block(main_text)
        analogs = parse_analogs_blocks(analogs_text)
        if base and analogs:
            excel_bytes, filename = build_excel(base, analogs)
            file = BufferedInputFile(excel_bytes, filename=filename)
            await message.answer_document(file, caption="Сравнительная таблица с выделением отличий")
    except Exception:
        logger.exception("Ошибка обработки запроса пользователя")
        await message.answer("Произошла ошибка при обработке запроса. Попробуйте позже.")
    finally:
        busy_users.discard(user_id)
