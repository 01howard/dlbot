FROM python:3.9-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install yt-dlp python-telegram-bot flask

WORKDIR /app
COPY . .

CMD ["python", "app.py"]
