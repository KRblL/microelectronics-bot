import logging
import re
from pathlib import Path

import pdfplumber
from PyPDF2 import PdfReader

logger = logging.getLogger(__name__)


def normalize_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w\sа-яА-ЯёЁ.,;:!?()\-–+/%°µμΩΩ@#=<>\[\]±≤≥×]", " ", text)
    return text.strip()


def extract_with_pdfplumber(path: str) -> str:
    chunks = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                chunks.append(page_text)
    return "\n".join(chunks)


def extract_with_pypdf2(path: str) -> str:
    chunks = []
    with open(path, "rb") as file:
        reader = PdfReader(file)
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                chunks.append(page_text)
    return "\n".join(chunks)


def extract_text_from_pdf(path: str, max_chars: int = 300000) -> str:
    pdf_path = Path(path)
    if not pdf_path.exists():
        raise FileNotFoundError(path)

    text = ""

    try:
        text = extract_with_pdfplumber(str(pdf_path))
        if text.strip():
            logger.info("Текст извлечён через pdfplumber: %s", pdf_path.name)
    except Exception as exc:
        logger.warning("pdfplumber не смог извлечь текст из %s: %s", pdf_path.name, exc)

    if len(text.strip()) < 2000:
        try:
            text = extract_with_pypdf2(str(pdf_path))
            if text.strip():
                logger.info("Текст извлечён через PyPDF2: %s", pdf_path.name)
        except Exception as exc:
            logger.warning("PyPDF2 не смог извлечь текст из %s: %s", pdf_path.name, exc)

    text = normalize_text(text)

    if not text:
        raise RuntimeError(f"Не удалось извлечь текст из PDF: {pdf_path.name}")

    if len(text) > max_chars:
        text = text[:max_chars]

    logger.info("Извлечено %d символов из PDF: %s", len(text), pdf_path.name)
    return text
