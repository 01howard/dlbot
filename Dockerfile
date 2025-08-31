FROM python:3.9-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 先安装依赖，利用Docker缓存
COPY requirements.txt .
RUN pip install -r requirements.txt

WORKDIR /app
COPY . .

CMD ["python", "app.py"]
