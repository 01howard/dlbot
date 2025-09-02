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
        # 创建线程但添加异常处理
        def thread_wrapper():
            try:
                download_and_send(youtube_url, chat_id)
            except Exception as e:
                logger.error(f"Error in download thread: {e}")
                # 发送错误消息到 Telegram
                asyncio.run(send_error_message(chat_id, str(e)))
        
        thread = threading.Thread(target=thread_wrapper)
        thread.daemon = True  # 设置为守护线程，主线程退出时自动结束
        thread.start()
        
        logger.info(f"Started download for URL: {youtube_url}")
        return jsonify({'status': 'processing', 'message': 'Download started'})
    
    except Exception as e:
        logger.error(f"Error starting download thread: {e}")
        return jsonify({'error': str(e)}), 500

def download_and_send(youtube_url, chat_id):
    """在后台线程中处理下载和发送"""
    try:
        # 下载视频
        video_path = download_youtube_video(youtube_url)
        
        # 检查文件大小，如果超过45MB则压缩（留一些余量）
        file_size = os.path.getsize(video_path)
        file_size_mb = file_size / (1024 * 1024)
        
        if file_size_mb > 45:  # 超过45MB就压缩
            logger.info(f"文件大小 {file_size_mb:.2f} MB 超过45MB，开始压缩...")
            compressed_path = compress_video(video_path)
            os.remove(video_path)  # 删除原始文件
            video_path = compressed_path  # 使用压缩后的文件
            
            # 检查压缩后的文件大小
            compressed_size = os.path.getsize(video_path)
            compressed_size_mb = compressed_size / (1024 * 1024)
            logger.info(f"压缩完成，压缩后大小: {compressed_size_mb:.2f} MB")
        
        # 发送到 Telegram
        asyncio.run(send_to_telegram(chat_id, video_path))
        
        # 清理临时文件
        os.remove(video_path)
        
    except Exception as e:
        logger.error(f"Error in download_and_send: {e}")
        # 重新抛出异常，让线程包装器处理
        raise

def download_youtube_video(url):
    """使用 yt-dlp 下载视频"""
    import shutil
    
    # 记录环境信息
    logger.info(f"Python PATH: {os.environ.get('PATH', '未设置')}")
    
    # 获取 yt-dlp 的绝对路径
    yt_dlp_path = shutil.which('yt-dlp') or '/usr/local/bin/yt-dlp'
    logger.info(f"yt-dlp 路径: {yt_dlp_path}")
    
    # 检查 FFmpeg
    ffmpeg_path = shutil.which('ffmpeg')
    logger.info(f"ffmpeg 路径: {ffmpeg_path}")
    
    # 创建临时文件
    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_file:
        temp_path = temp_file.name
    
    # 准备环境变量
    env = os.environ.copy()
    env['PATH'] = '/usr/local/bin:/usr/bin:/bin:' + env.get('PATH', '')
    
    # 构建命令
    cmd = [
        yt_dlp_path,
        '-f', 'bestvideo[height<=720]+bestaudio/best[height<=720]',
        '--output', temp_path,
        '--no-warnings',
        '--merge-output-format', 'mp4',
        '--socket-timeout', '30',
        '--retries', '3',
        url
    ]
    
    # 只有在cookies文件存在且不为空时添加cookies参数
    cookies_path = '/app/cookies.txt'
    if os.path.exists(cookies_path) and os.path.getsize(cookies_path) > 0:
        cmd.extend(['--cookies', cookies_path])
    
    logger.info(f"执行命令: {' '.join(cmd)}")
    
    # 记录当前工作目录
    original_cwd = os.getcwd()
    logger.info(f"当前工作目录: {original_cwd}")
    
    try:
        # 切换到临时目录执行
        os.chdir('/tmp')
        
        # 执行命令
        result = subprocess.run(cmd, check=True, timeout=300, 
                              capture_output=True, text=True, env=env)
        
        logger.info(f"yt-dlp 输出: {result.stdout}")
        if result.stderr:
            logger.warning(f"yt-dlp 错误输出: {result.stderr}")
        
        # 检查最终文件大小
        file_size = os.path.getsize(temp_path)
        file_size_mb = file_size / (1024 * 1024)
        logger.info(f"下载完成，文件大小: {file_size_mb:.2f} MB")
        
        if file_size_mb == 0:
            raise Exception('下载的文件大小为0')
            
        return temp_path
        
    except subprocess.TimeoutExpired:
        os.remove(temp_path)
        raise Exception('下载超时，请稍后再试')
    except subprocess.CalledProcessError as e:
        os.remove(temp_path)
        logger.error(f"yt-dlp 命令失败: {e.stderr if e.stderr else e.stdout}")
        raise Exception(f'下载失败: {e.stderr if e.stderr else "未知错误"}')
    except Exception as e:
        os.remove(temp_path)
        raise Exception(f'下载过程中出现错误: {e}')
    finally:
        # 恢复原始工作目录
        os.chdir(original_cwd)

def compress_video(input_path, target_size_mb=45):
    """使用 FFmpeg 压缩视频到指定大小"""
    # 获取视频信息
    cmd = [
        'ffprobe',
        '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=duration,width,height',
        '-of', 'csv=p=0',
        input_path
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        duration, width, height = map(float, result.stdout.strip().split(','))
    except Exception as e:
        logger.warning(f"无法获取视频信息: {e}")
        duration, width, height = 60, 1280, 720  # 默认值
    
    # 计算目标比特率 (kbps)
    target_bitrate = int((target_size_mb * 8192) / duration)  # 8 * 1024 = 8192
    
    # 根据原始分辨率调整比特率
    if width * height > 1280 * 720:  # 如果分辨率高于720p
        target_bitrate = min(target_bitrate, 2000)  # 限制最大比特率
    elif width * height > 854 * 480:  # 如果分辨率高于480p
        target_bitrate = min(target_bitrate, 1500)
    else:
        target_bitrate = min(target_bitrate, 1000)
    
    # 创建输出文件路径
    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_file:
        output_path = temp_file.name
    
    # 使用 FFmpeg 压缩视频
    cmd = [
        'ffmpeg',
        '-i', input_path,
        '-c:v', 'libx264',
        '-b:v', f'{target_bitrate}k',
        '-maxrate', f'{target_bitrate}k',
        '-bufsize', f'{target_bitrate * 2}k',
        '-preset', 'fast',
        '-c:a', 'aac',
        '-b:a', '128k',
        '-y',  # 覆盖输出文件
        output_path
    ]
    
    try:
        subprocess.run(cmd, check=True, timeout=600)  # 压缩可能需要更长时间
        return output_path
    except subprocess.TimeoutExpired:
        os.remove(output_path)
        raise Exception('压缩超时，请稍后再试')
    except subprocess.CalledProcessError as e:
        os.remove(output_path)
        raise Exception(f'压缩失败: {e}')
    except Exception as e:
        os.remove(output_path)
        raise Exception(f'压缩过程中出现错误: {e}')

async def send_to_telegram(chat_id, file_path):
    """发送文件到 Telegram"""
    bot = Bot(token=TELEGRAM_TOKEN)
    
    # 检查文件大小
    file_size = os.path.getsize(file_path)
    file_size_mb = file_size / (1024 * 1024)
    
    if file_size_mb > 50:
        logger.warning(f"文件大小 {file_size_mb:.2f} MB 仍然超过50MB限制")
        raise Exception(f'文件太大 ({file_size_mb:.2f} MB)，超过50MB限制')
    
    with open(file_path, 'rb') as video_file:
        await bot.send_video(
            chat_id=chat_id,
            video=video_file,
            caption='您的视频已准备好！'
        )

async def send_error_message(chat_id, error_msg):
    """发送错误消息到 Telegram"""
    bot = Bot(token=TELEGRAM_TOKEN)
    
    # 提供更友好的错误消息
    friendly_msg = error_msg
    if "Requested format is not available" in error_msg:
        friendly_msg = "找不到合适的视频格式。视频可能太大或没有可用的格式。"
    elif "filesize" in error_msg.lower():
        friendly_msg = "视频太大，超过大小限制。已尝试压缩但仍然过大。"
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
