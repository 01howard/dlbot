from flask import Flask, request, jsonify
import subprocess
import os
import tempfile
from telegram import Bot
import asyncio
import threading

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
async def wake_handler():
    """被 Cloudflare Workers 唤醒的端点"""
    # 验证请求
    auth_token = request.headers.get('Authorization')
    if not auth_token or not auth_token.startswith('Bearer '):
        return jsonify({'error': 'Unauthorized'}), 401
    
    incoming_secret = auth_token[7:]  # 移除 'Bearer ' 前缀
    if incoming_secret != KOYEB_SECRET:
        return jsonify({'error': 'Invalid secret'}), 403
    
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No JSON data'}), 400
    
    youtube_url = data.get('url')
    chat_id = data.get('chatId')
    
    if not youtube_url or not chat_id:
        return jsonify({'error': 'Missing parameters'}), 400
    
    try:
        # 在后台线程中处理下载，避免阻塞
        thread = threading.Thread(
            target=download_and_send,
            args=(youtube_url, chat_id)
        )
        thread.start()
        
        return jsonify({'status': 'processing', 'message': 'Download started'})
    
    except Exception as e:
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
        print(f"Error in download_and_send: {e}")

def download_youtube_video(url):
    """使用 yt-dlp 下载视频"""
    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_file:
        temp_path = temp_file.name
    
    # 使用 yt-dlp 下载
    cmd = [
        'yt-dlp',
        '-f', 'best[filesize<50M]',  # 限制文件大小
        '-o', temp_path,
        '--no-warnings',
        url
    ]
    
    try:
        subprocess.run(cmd, check=True, timeout=300)
        return temp_path
    except subprocess.TimeoutExpired:
        os.remove(temp_path)
        raise Exception('Download timeout')
    except subprocess.CalledProcessError as e:
        os.remove(temp_path)
        raise Exception(f'Download failed: {e}')

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

if __name__ == '__main__':
    # 使用生产环境的 WSGI 服务器
    from waitress import serve
    serve(app, host='0.0.0.0', port=8080)
