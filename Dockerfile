FROM python:3.10-slim

# 安装必要的依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# 安装 yt-dlp
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp && \
    chmod a+rx /usr/local/bin/yt-dlp

# 创建非root用户
RUN useradd -m -u 1000 appuser
USER appuser

WORKDIR /app

# 复制并安装Python依赖
COPY --chown=appuser:appuser requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY --chown=appuser:appuser app.py .
COPY --chown=appuser:appuser cookies.txt .

# 设置环境变量
ENV PYTHONUNBUFFERED=1

CMD ["python", "app.py"]
