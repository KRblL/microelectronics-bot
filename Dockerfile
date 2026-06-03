FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY bot/ ./bot/
COPY data/ ./data/

RUN mkdir -p /app/logs /app/data/datasheets

CMD ["python", "-m", "bot.bot"]