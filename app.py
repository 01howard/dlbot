# app.py
from flask import Flask, request, jsonify
import subprocess
import os
import tempfile
from telegram import Bot
import asyncio

app = Flask(__name__)

# 從環境變量獲取配置
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
KOYEB_SECRET = os.getenv('KOYEB_SECRET')

@app.route('/wake', methods=['POST'])
async def wake_handler():
    """被 Cloudflare Workers 喚醒的端點"""
    # 驗證請求
    auth_token = request.headers.get('Authorization')
    if auth_token != f'Bearer {KOYEB_SECRET}':
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.json
    youtube_url = data.get('url')
    chat_id = data.get('chatId')
    
    if not youtube_url or not chat_id:
        return jsonify({'error': 'Missing parameters'}), 400
    
    try:
        # 下載視頻
        video_path = await download_youtube_video(youtube_url)
        
        # 發送到 Telegram
        await send_to_telegram(chat_id, video_path)
        
        # 清理臨時文件
        os.remove(video_path)
        
        return jsonify({'status': 'success'})
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

async def download_youtube_video(url):
    """使用 yt-dlp 下載視頻"""
    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_file:
        temp_path = temp_file.name
    
    # 使用 yt-dlp 下載
    cmd = [
        'yt-dlp',
        '-f', 'best[filesize<50M]',  # 限制文件大小
        '-o', temp_path,
        url
    ]
    
    try:
        subprocess.run(cmd, check=True, timeout=300)  # 5分鐘超時
        return temp_path
    except subprocess.TimeoutExpired:
        os.remove(temp_path)
        raise Exception('Download timeout')
    except subprocess.CalledProcessError:
        os.remove(temp_path)
        raise Exception('Download failed')

async def send_to_telegram(chat_id, file_path):
    """發送文件到 Telegram"""
    bot = Bot(token=TELEGRAM_TOKEN)
    
    with open(file_path, 'rb') as video_file:
        await bot.send_video(
            chat_id=chat_id,
            video=video_file,
            caption='您的視頻已準備好！',
            timeout=120
        )

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
