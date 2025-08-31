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
    
    # 首先获取可用格式列表
    format_cmd = [
        'yt-dlp',
        '--list-formats',
        '--cookies', '/app/cookies.txt',
        url
    ]
    
    try:
        format_result = subprocess.run(format_cmd, check=True, capture_output=True, text=True, timeout=60)
        logger.info(f"可用格式:\n{format_result.stdout}")
    except Exception as e:
        logger.warning(f"无法获取格式列表: {e}")
    
    # 使用灵活的格式选择策略
    cmd = [
        'yt-dlp',
        '-f', 'best[filesize<50M]/best[height<=720][filesize<100M]/best[height<=480]/best',
        '--cookies', '/app/cookies.txt',
        '-o', temp_path,
        '--no-warnings',
        '--merge-output-format', 'mp4',
        url
    ]
    
    try:
        subprocess.run(cmd, check=True, timeout=300)
        
        # 检查最终文件大小
        file_size = os.path.getsize(temp_path)
        file_size_mb = file_size / (1024 * 1024)
        logger.info(f"下载完成，文件大小: {file_size_mb:.2f} MB")
        
        if file_size_mb > 50:
            logger.warning(f"文件大小 ({file_size_mb:.2f} MB) 超过 50MB 限制")
            # 可以选择删除文件并抛出异常，或者继续处理
            # os.remove(temp_path)
            # raise Exception(f'视频太大 ({file_size_mb:.2f} MB)，超过 50MB 限制')
        
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
    
    # 提供更友好的错误消息
    friendly_msg = error_msg
    if "Requested format is not available" in error_msg:
        friendly_msg = "找不到合适的视频格式。视频可能太大或没有可用的格式。"
    elif "filesize" in error_msg.lower():
        friendly_msg = "视频太大，超过大小限制。请尝试较短的视频。"
    elif "Sign in to confirm" in error_msg:
        friendly_msg = "需要验证身份，请稍后再试或联系管理员。"
    
    await bot.send_message(
        chat_id=chat_id,
        text=f"处理视频时出现错误: {friendly_msg}"
    )

if __name__ == '__main__':
    # 使用生产环境的 WSGI 服务器
    from waitress import serve
    serve(app, host='0.0.0.0', port=8080)
