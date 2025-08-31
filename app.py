from flask import Flask, request, jsonify
import subprocess
import os
import tempfile
from telegram import Bot
import asyncio
import threading
import logging

# 设置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 从环境变量获取配置
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
KOYEB_SECRET = os.getenv('KOYEB_SECRET')

# 添加根路径路由
@app.route('/')
def home():
    return jsonify({
        'status': 'ok',
        'service': 'youtube-downloader',
        'version': '1.0'
    })

# 添加健康检查端点
@app.route('/health')
def health_check():
    return jsonify({'status': 'healthy'}), 200

@app.route('/wake', methods=['POST'])
def wake_handler():
    """被 Cloudflare Workers 唤醒的端点"""
    # 验证请求
    auth_token = request.headers.get('Authorization')
    if not auth_token or not auth_token.startswith('Bearer '):
        logger.warning("Unauthorized: No Bearer token provided")
        return jsonify({'error': 'Unauthorized'}), 401
    
    incoming_secret = auth_token[7:]  # 移除 'Bearer ' 前缀
    
    # 记录接收到的密钥和期望的密钥（只记录部分字符以避免安全风险）
    logger.info(f"Received secret: {incoming_secret[:4]}... (truncated)")
    logger.info(f"Expected secret: {KOYEB_SECRET[:4]}... (truncated)")
    
    if incoming_secret != KOYEB_SECRET:
        logger.warning(f"Invalid secret provided: {incoming_secret[:4]}...")
        return jsonify({'error': 'Invalid secret'}), 403
    
    data = request.get_json()
    if not data:
        logger.warning("No JSON data provided")
        return jsonify({'error': 'No JSON data'}), 400
    
    youtube_url = data.get('url')
    chat_id = data.get('chatId')
    
    if not youtube_url or not chat_id:
        logger.warning(f"Missing parameters: url={youtube_url}, chatId={chat_id}")
        return jsonify({'error': 'Missing parameters'}), 400
    
    try:
        # 在后台线程中处理下载，避免阻塞
        thread = threading.Thread(
            target=download_and_send,
            args=(youtube_url, chat_id)
        )
        thread.start()
        
        logger.info(f"Started download for URL: {youtube_url}")
        return jsonify({'status': 'processing', 'message': 'Download started'})
    
    except Exception as e:
        logger.error(f"Error starting download: {e}")
        return jsonify({'error': str(e)}), 500

def download_and_send(youtube_url, chat_id):
    """在后台线程中处理下载和发送"""
    try:
        # 下载视频
        video_path = download_youtube_video(youtube_url)
        
        # 发送到 Telegram
        asyncio.run(send_to_telegram(chat_id, video_path))
        
        # 清理临时文件
        os.remove(video_path)
        
    except Exception as e:
        logger.error(f"Error in download_and_send: {e}")
        # 发送错误消息到用户
        asyncio.run(send_error_message(chat_id, str(e)))

def download_youtube_video(url):
    """使用 yt-dlp 下载视频"""
    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_file:
        temp_path = temp_file.name
    
    # 使用 yt-dlp 下载，添加 cookies 参数
    cmd = [
        'yt-dlp',
        '-f', 'best[filesize<50M]',  # 限制文件大小
        '--cookies', '/app/cookies.txt',  # 添加 cookies 文件路径
        '-o', temp_path,
        '--no-warnings',
        url
    ]
    
    try:
        subprocess.run(cmd, check=True, timeout=300)
        return temp_path
    except subprocess.TimeoutExpired:
        os.remove(temp_path)
        raise Exception('下载超时，请稍后再试')
    except subprocess.CalledProcessError as e:
        os.remove(temp_path)
        raise Exception(f'下载失败: {e}')
    except Exception as e:
        os.remove(temp_path)
        raise Exception(f'下载过程中出现错误: {e}')

async def send_to_telegram(chat_id, file_path):
    """发送文件到 Telegram"""
    bot = Bot(token=TELEGRAM_TOKEN)
    
    with open(file_path, 'rb') as video_file:
        await bot.send_video(
            chat_id=chat_id,
            video=video_file,
            caption='您的视频已准备好！',
            timeout=120
        )

async def send_error_message(chat_id, error_msg):
    """发送错误消息到 Telegram"""
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(
        chat_id=chat_id,
        text=f"处理视频时出现错误: {error_msg}"
    )

if __name__ == '__main__':
    # 使用生产环境的 WSGI 服务器
    from waitress import serve
    serve(app, host='0.0.0.0', port=8080)
