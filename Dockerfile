FROM python:3.11-slim

WORKDIR /app

# deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# default DB location (override with BOT_DB_PATH; mount a volume here to persist)
ENV BOT_DB_PATH=/app/data/bot.db \
    PORT=8000 \
    SCAN_INTERVAL_SEC=60

EXPOSE 8000

# shell form so ${PORT} injected by the host (Render/Railway/Fly) is expanded
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"]
