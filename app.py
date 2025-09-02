import logging
import os
import subprocess
import tempfile
import threading
import asyncio
import uuid 
from flask import Flask, request, jsonify
from telegram import Bot
from waitress import serve

# --- 日誌設定 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Flask App 初始化 ---
app = Flask(__name__)

# --- 從環境變數獲取配置 ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
KOYEB_SECRET = os.getenv('KOYEB_SECRET')
COOKIES_PATH = os.getenv('COOKIES_PATH', '/app/cookies.txt') # 讓 cookies 路徑可配置

# --- Flask 路由 ---
@app.route('/')
def home():
    """根路徑，用於服務健康檢查或基本資訊。"""
    return jsonify({
        'status': 'ok',
        'service': 'youtube-downloader',
        'version': '1.2-optimized'
    })

@app.route('/health')
def health_check():
    """健康檢查端點。"""
    return jsonify({'status': 'healthy'}), 200

@app.route('/wake', methods=['POST'])
def wake_handler():
    """
    主工作端點，接收請求並在背景執行緒中處理下載任務。
    """
    # 驗證授權
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        logger.warning("未經授權的請求：缺少 Bearer Token。")
        return jsonify({'error': 'Unauthorized'}), 401

    incoming_secret = auth_header[7:]
    if incoming_secret != KOYEB_SECRET:
        logger.warning("無效的 Secret。")
        return jsonify({'error': 'Invalid secret'}), 403

    # 解析請求資料
    data = request.get_json()
    if not data:
        logger.warning("請求中未提供 JSON 資料。")
        return jsonify({'error': 'No JSON data'}), 400

    youtube_url = data.get('url')
    chat_id = data.get('chatId')

    if not youtube_url or not chat_id:
        logger.warning(f"缺少必要參數: url={youtube_url}, chatId={chat_id}")
        return jsonify({'error': 'Missing parameters'}), 400

    # --- 關鍵：背景執行緒 ---
    try:
        thread = threading.Thread(
            target=run_download_and_send,
            args=(youtube_url, chat_id)
        )
        thread.daemon = True
        thread.start()
        logger.info(f"已為 URL 啟動背景下載任務: {youtube_url}")
        return jsonify({'status': 'processing', 'message': 'Download started in background'})
    except Exception as e:
        logger.error(f"啟動下載執行緒時出錯: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500

# --- 核心邏輯函式 ---
def run_download_and_send(youtube_url, chat_id):
    """
    執行緒的目標函式，包裹了完整的下載、壓縮、發送和錯誤處理流程。
    """
    final_path = None # 初始化 final_path
    try:
        # 1. 下載影片
        video_path = download_youtube_video(youtube_url)
        if not video_path:
            raise Exception("下載失敗，未返回有效的檔案路徑。")

        # 2. 檢查檔案大小並視情況壓縮
        file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
        logger.info(f"原始檔案大小: {file_size_mb:.2f} MB")

        final_path = video_path
        if file_size_mb > 48:  # 留一點緩衝空間給 50MB 限制
            logger.info("檔案過大，開始快速壓縮...")
            compressed_path = compress_video(video_path)
            os.remove(video_path)  # 刪除原始大檔案
            final_path = compressed_path
            compressed_size_mb = os.path.getsize(final_path) / (1024 * 1024)
            logger.info(f"壓縮完成，新檔案大小: {compressed_size_mb:.2f} MB")

        # 3. 發送到 Telegram
        asyncio.run(send_to_telegram(chat_id, final_path, "您的影片已準備好！"))

    except Exception as e:
        logger.error(f"處理 URL {youtube_url} 時發生錯誤: {e}", exc_info=True)
        asyncio.run(send_to_telegram(chat_id, None, f"處理影片時出錯了😭\n錯誤訊息: {e}"))
    finally:
        # 4. 清理臨時檔案
        if final_path and os.path.exists(final_path):
            try:
                os.remove(final_path)
                logger.info(f"已清理臨時檔案: {final_path}")
            except OSError as e:
                logger.error(f"清理檔案 {final_path} 失敗: {e}")


def download_youtube_video(url):
    """使用 yt-dlp 下載影片，並返回臨時檔案的路徑。"""
    temp_filename = f"{uuid.uuid4()}.mp4"
    temp_path = os.path.join(tempfile.gettempdir(), temp_filename)
    
    cmd = [
        'yt-dlp',
        '-f', 'bestvideo[height<=720]+bestaudio/best[height<=720]',
        '--merge-output-format', 'mp4',
        '--max-filesize', '750M',
        '--output', temp_path,
        '--no-warnings',
        '--socket-timeout', '30',
        '--retries', '3',
    ]

    if os.path.exists(COOKIES_PATH) and os.path.getsize(COOKIES_PATH) > 0:
        cmd.extend(['--cookies', COOKIES_PATH])

    cmd.append(url)

    logger.info(f"執行 yt-dlp 命令: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, check=True, timeout=300, capture_output=True, text=True, encoding='utf-8'
        )
        logger.info(f"yt-dlp 輸出: {result.stdout}")
        if result.stderr:
            logger.warning(f"yt-dlp 錯誤輸出: {result.stderr}")

        if os.path.getsize(temp_path) > 0:
            return temp_path
        else:
            # 如果檔案大小為 0，也視為失敗並清理
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise Exception("下載的檔案大小為 0。")

    except subprocess.TimeoutExpired:
        if os.path.exists(temp_path): os.remove(temp_path)
        raise Exception("下載超時 (超過 300 秒)。")
    except subprocess.CalledProcessError as e:
        if os.path.exists(temp_path): os.remove(temp_path)
        error_message = e.stderr or e.stdout
        raise Exception(f"yt-dlp 下載失敗: {error_message.strip()}")

def compress_video(input_path):
    """
    使用 FFmpeg 快速壓縮影片 (單階段 CRF)。
    """
    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_file:
        output_path = temp_file.name
    
    try:
        # 使用單階段 CRF 編碼，速度遠快於兩階段
        # -crf 24: 一個很好的品質與檔案大小的平衡點 (數值越高品質越低)
        # -preset superfast: 比 'fast' 更快，犧牲少量壓縮率換取大量時間
        ffmpeg_cmd = [
            'ffmpeg',
            '-y',
            '-i', input_path,
            '-c:v', 'libx264',
            '-crf', '24',
            '-preset', 'superfast',
            '-c:a', 'aac',
            '-b:a', '128k',
            output_path
        ]
        
        logger.info(f"執行 FFmpeg 單階段壓縮命令: {' '.join(ffmpeg_cmd)}")
        
        # 執行壓縮，設定 10 分鐘超時
        subprocess.run(ffmpeg_cmd, check=True, timeout=600, capture_output=True, text=True, encoding='utf-8')

        return output_path

    except Exception as e:
        # 如果壓縮出錯，清理輸出的臨時檔
        if os.path.exists(output_path):
            os.remove(output_path)
        
        # 提取更詳細的 FFmpeg 錯誤
        error_details = ""
        if isinstance(e, subprocess.CalledProcessError):
            error_details = e.stderr or e.stdout
        raise Exception(f"FFmpeg 壓縮失敗: {e} - {error_details}")

async def send_to_telegram(chat_id, file_path, caption):
    """發送檔案或文字訊息到 Telegram。"""
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN 未設定，無法發送訊息。")
        return
    bot = Bot(token=TELEGRAM_TOKEN)
    
    if file_path and os.path.exists(file_path):
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if file_size_mb > 50:
            error_msg = f"檔案太大 ({file_size_mb:.2f} MB)，Telegram 拒絕傳送。"
            logger.error(error_msg)
            await bot.send_message(chat_id=chat_id, text=error_msg)
            return

        with open(file_path, 'rb') as video_file:
            await bot.send_video(
                chat_id=chat_id,
                video=video_file,
                caption=caption,
                read_timeout=60, # 增加超時時間
                write_timeout=60
            )
    else:
        # 如果沒有檔案路徑 (例如發生錯誤時)，只發送文字訊息
        await bot.send_message(
            chat_id=chat_id,
            text=caption,
            read_timeout=20,
            write_timeout=20
        )

# --- 主程式入口 ---
if __name__ == '__main__':
    # 在生產環境中，建議使用 Waitress 或 Gunicorn
    logger.info("服務啟動於 http://0.0.0.0:8080")
    serve(app, host='0.0.0.0', port=8080)
