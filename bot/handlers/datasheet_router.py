import asyncio
import logging
import re
import time

from aiogram import F, Router
from aiogram.types import BufferedInputFile, Message

from backend.datasheet_downloader import download_pdf, search_datasheet
from backend.grok_api import GrokComponentFinder
from backend.report_builder import build_excel, parse_analogs_blocks, parse_component_block, split_grok_answer
from backend.text_extractor import extract_text_from_pdf

datasheet_router = Router()
logger = logging.getLogger(__name__)
performance_logger = logging.getLogger("backend.grok_api")
finder = GrokComponentFinder()
busy_users: set[int] = set()


async def send_long_text(message: Message, text: str) -> None:
    limit = 3800
    chunks = [text[index:index + limit] for index in range(0, len(text), limit)]
    for chunk in chunks:
        await message.answer(chunk)


@datasheet_router.message(F.text)
async def datasheet_handler(message: Message) -> None:
    text = (message.text or "").strip()

    user_id = message.from_user.id if message.from_user else message.chat.id
    if user_id in busy_users:
        await message.answer("Ваш предыдущий запрос еще обрабатывается. Дождитесь результата.")
        return

    request_start_time = time.perf_counter()
    busy_users.add(user_id)
    stats = {"main_component_attempts": 0, "analogs_attempts": 0, "success": 0}

    try:
        await message.answer("Принял запрос ✅\nИщу данные по компоненту…")

        result = await search_datasheet(text)
        if result["status"] != "ok" or not result["items"]:
            elapsed = time.perf_counter() - request_start_time
            logger.warning("Техническая документация для %s не найдена за %.2f секунд", text, elapsed)
            await message.answer(f"Техническая документация для {text} не найдена.")
            return

        item = result["items"][0]
        pdf_path = await download_pdf(item["link"], item["part"], item["manufacturer"])
        if not pdf_path:
            elapsed = time.perf_counter() - request_start_time
            logger.warning("PDF-документация для %s не загружена за %.2f секунд", text, elapsed)
            await message.answer("Не удалось загрузить PDF-документацию.")
            return

        loop = asyncio.get_running_loop()
        documentation_text = await loop.run_in_executor(None, extract_text_from_pdf, pdf_path)

        answer_text, stats = await loop.run_in_executor(
            None,
            finder.find_component,
            text,
            documentation_text,
        )

        await send_long_text(message, answer_text)

        if not stats.get("success"):
            elapsed = time.perf_counter() - request_start_time
            logger.warning("Запрос %s завершён без успешного результата за %.2f секунд", text, elapsed)
            return

        main_text, analogs_text = split_grok_answer(answer_text)
        base = parse_component_block(main_text)
        analogs = parse_analogs_blocks(analogs_text)

        if base and analogs:
            excel_bytes, filename = build_excel(base, analogs)
            file = BufferedInputFile(excel_bytes, filename=filename)
            await message.answer_document(file, caption="Сравнительная таблица с выделением отличий")

        total_time = time.perf_counter() - request_start_time

        performance_logger.info(
            "Запрос выполнен успешно за %.2f секунд (шаг 1: %d попыток, шаг 2: %d попыток)",
            total_time,
            stats.get("main_component_attempts", 0),
            stats.get("analogs_attempts", 0),
        )

    except Exception:
        elapsed = time.perf_counter() - request_start_time
        logger.exception("Ошибка обработки запроса пользователя за %.2f секунд", elapsed)
        await message.answer("Произошла ошибка при обработке запроса. Попробуйте позже.")
    finally:
        busy_users.discard(user_id)
